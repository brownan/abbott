import glob
from collections import defaultdict
import time
import os.path

from twisted.python import log
from twisted.internet import defer

from ..transport import Event
from ..pluginbase import BotPlugin, EventWatcher

"""
IRC OP-related plugins. This is meant to replace the old admin.* plugins with a
new, cleaned-up architecture.

Outline of this module:
* Connector plugins that interface with a method of performing IRC operations.
  They expose one method per operation (ban, quiet, op, etc)
  - Chanserv connector: sends requests to chanserv
  - Weechat connector: sends requests to chanserv via weechat

  These plugins should be relatively lightweight. These plugins don't have any
  return value, and never error. The chanserv connector has no way of detecting
  if it didn't work, so detection of whether the request worked must happen at
  a higher level.

* The OpProvider plugin provides a unified interface to OP functions. It takes
  care of choosing which connector to use (or sending a mode request directly
  to the irc plugin itself) depending on the channel, the command, and the
  current OP status of the bot.

  Specific features of this plugin:
  - exposes one function per operation to other plugins: op, deop, voice,
    devoice, quiet, unquiet, ban, unban, kick, and set topic
  - keeps track of what it can do in which channels and how. For example, some
    channels it may can do everything through chanserv, but others it may can
    only OP itself with chanserv and must do everything else itself.
  - if it has a method of acquiring OP, it will automatically de-op itself when
    it's not needed.
  - provides a become_op() function for other plugins to force it to hold OP
    for a while

"""

class OpFailed(Exception):
    """Raised when an operator function was requested but it could not be
    fulfilled

    """
    pass

class WeechatConnector(BotPlugin):
    """Listens for requests of the form connector.weechat.X where X is one of:
    op, deop, quiet, unquiet, voice, devoice. Each request takes a channel name
    and nick as parameters. Sends the request to Chanserv via a local weechat
    instance.

    """
    DEFAULT_CONFIG = {"weechat_server": "irc.server.freenode"}
    def start(self):
        super(WeechatConnector, self).start()

        for operation in ("op", "deop", "quiet", "unquiet", "voice", "devoice", "topic"):
            self.provides_request("connector.weechat.{0}".format(operation))

    def incoming_request(self, reqname, channel, nick):
        operation = reqname.split(".")[-1].upper()

        path = glob.glob(os.path.expanduser("~/.weechat/weechat_fifo_*"))[0]

        log.msg("Weechat connector sending command {0} {1} {2}".format(operation, channel, nick))
        with open(path, 'w') as out:
            out.write("{weechat_server} */msg ChanServ {op} {channel} {nick}\n".format(
                weechat_server=self.config['weechat_server'],
                op=operation,
                channel=channel,
                nick=nick,
                ))

class ChanservConnector(BotPlugin):
    """Listens for requests of the form connector.chanserv.X where X is one of:
    op, deop, quiet, unquiet, voice, devoice. Each request takes a channel name
    and a nick as parameters. Sends the request to Chanserv directly. Assumes
    the bot is logged in and identified and has permission to perform the
    request.

    """
    def start(self):
        super(ChanservConnector, self).start()

        for operation in ("op", "deop", "quiet", "unquiet", "voice", "devoice", "topic"):
            self.provides_request("connector.chanserv.{0}".format(operation))

    def incoming_request(self, reqname, channel, nick):
        operation = reqname.split(".")[-1].upper()

        self.transport.send_event(Event("irc.do_msg",
            user="ChanServ",
            message="{op} {channel} {nick}".format(
                op=operation,
                channel=channel,
                nick=nick),
            ))

class OpProvider(EventWatcher, BotPlugin):
    """Provides a unified interface to other plugins to various operator tasks.
    Provides the following requests in the form of ircop.X where X is one of:
    op, deop, voice, devoice, quiet, unquiet, ban, unban, kick, topic. All take
    a first parameter: channel. All except topic take a second parameter: a
    hostmask or nick.

    Also provides ircop.become_op which takes two parameters: a channel name
    and a duration, in seconds. When called, the bot will attempt to acquire op
    status in the given channel and  hold it for (at least) the given duration.

    """
    REQUIRES = ["ircutil.HasOp", "ircutil.ChanMode"]
    DEFAULT_CONFIG = {"opmethod": dict()}

    ### Definition of various requests provided by this plugin
    # Connector requests are requests that can be performed by a connector
    # plugin such as chanserv. They all have the same signature: (channel,
    # param) with no return. All of these operations are implemented by a
    # single method: _do_connector_operation
    CONNECTOR_REQS = frozenset(['op', 'deop', 'voice', 'devoice', 'quiet',
            'unquiet', 'topic'])
    # These are requests that have unique signatures and unique
    # implementations. Op must be acquired for these and we must do them
    # ourself; they don't have a connector implementation. These are all
    # implemented by a method of the form _do_{name}
    # ban and unban are missing because it's just a shorthand for setting a +b
    # mode and doing a kick.
    OTHER_REQS = frozenset(['kick', 'become_op', 'mode'])

    def start(self):
        super(OpProvider, self).start()

        # A timestamp that we should hold op until, tracked per-channel. Used
        # to keep track of op requests from the become_op request call.
        self.op_until = defaultdict(float)

        # A per-channel buffer of mode requests that we will fulfill shortly.
        # This is held so that we may de-duplicate mode requests. Each item is
        # a tuple: (mode, argument), where argument may be None.
        self.mode_buffer = defaultdict(set)

        # Maps channel names to a boolean var indicating whether we have an
        # outstanding OP request pending on this channel or not. If an
        # operation needs OP, it should check this var. If it's false, put in
        # an op request and then wait. If it's true, just wait. Waiters should
        # also set an error timeout (20-30 seconds) in case the op request
        # errors and is not fulfilled.
        self.op_pending = defaultdict(bool)

        for operation in self.CONNECTOR_REQS | self.OTHER_REQS:
            self.provides_request("ircop.{0}".format(operation))

        self.listen_for_event("ircutil.hasop.acquired")
        self.listen_for_event("ircop.mode_buffer_emptied")

    def reload(self):
        super(OpProvider, self).reload()
        
        # Keeps a mapping of channels to a dict mapping operations to connectors.
        self.config["opmethod"] = defaultdict(dict, self.config["opmethod"])

    def incoming_request(self, reqname, *args):
        # Choose the appropriate handler here.
        reqname = reqname.split(".")[-1]
        if reqname in self.CONNECTOR_REQS:
            self._do_connector_operation(reqname, *args)
        elif reqname in self.OTHER_REQS:
            return getattr(self, "_do_{0}".format(reqname))(*args)

    ### The following helper methods are used in implementing this plugin's
    ### functions
    @defer.inlineCallbacks
    def _wait_for_op(self, channel, set_duration=0):
        """Returns a deferred that fires when the bot has op in the named
        channel, which may be immediately. If the bot does not have op, it will
        be requested and the defer will fire when it is acquired. May error if
        op is un-acquirable in the channel.

        If set_duration is non-zero, the op_until variable will be set to
        set_duration seconds after the time that op is acquired.

        """
        # If we have op, just return immediately.
        if (yield self.transport.issue_request("irc.has_op", channel)):
            return
        # Start an event watcher immediately to help curb race conditions
        # involved in op being acquired after we check but before the event
        # watcher is active. It's probably not even possible, but it doesn't
        # hurt to do this anyways. (notice how we don't yield-wait for this
        # until after)
        op_waiter = self.wait_for(Event("ircutil.hasop.acquired"), timeout=30)

        connector = self.config["opmethod"][channel].get("op")
        if not connector:
            raise OpFailed("I have no way to acquire op in {0}".format(channel))

        nick = (yield self.transport.issue_request("irc.getnick"))

        issued = False
        try:
            if not self.op_pending[channel]:
                self.op_pending[channel] = True
                issued = True
                log.msg("We need op. Asking the {0} connector".format(connector))
                try:
                    yield self.transport.issue_request("connector.{0}.op".format(connector),
                            channel=channel,
                            nick = nick,
                            )
                except NotImplementedError:
                    raise OpFailed("Connector {0} is not loaded, does not exist, or does not provide 'op'".format(connector))
            else:
                log.msg("We need op but op is already pending. waiting...")

            # Wait for op to be acquired by waiting for the event watcher started
            # at the beginning of this method.
            if not (yield op_waiter):
                raise OpFailed("Timeout in waiting for op request to be fulfilled")
        finally:
            if issued:
                self.op_pending[channel] = False

        if issued:
            if set_duration:
                self.op_until[channel] = max(self.op_until[channel], time.time()+set_duration)

            # If we opped ourself, which we must have if the code got here,
            # then put in a request to de-op ourself.  Issue the deop request
            # right away. The deop request takes a second to go through as
            # implemented in _do_mode, so that should give it enough time to do
            # any other op-requiring operations before the deop actually goes
            # through.
            # This behavior also has the advantage that the bot will never
            # de-op itself if it was opped on purpose from another user, only
            # if it needed to give itself op
            self._deop(channel)

    @defer.inlineCallbacks
    def _deop(self, channel):
        """Issues a deop request, either immediately, or once the timer
        expires

        This is only called from _wait_for_op, but is implemented as a second
        method with inlineCallbacks so that it can go ahead and return even if
        this method continues to wait out the timer
        
        """
        while self.op_until[channel] - time.time() > 0:
            yield self.wait_for(timeout=self.op_until[channel] - time.time())

        if not (yield self.transport.issue_request("irc.has_op", channel)):
            return

        log.msg("deoping")
        self._do_mode(channel, "-o",
                (yield self.transport.issue_request("irc.getnick"))
                )
        # It may be tempting to set the delay parameter to the _do_mode here to
        # configure how long the bot holds op, similar to the behavior of the
        # old admin plugin where any op-acquisition would hold it for a
        # configured amount of time after it was last requested.
        # However, this wouldn't do exactly the same thing. Setting the delay
        # only puts an upper bound on the amount of time you're willing to wait
        # for the mode request. So a 10 minute timer just means it will flush
        # the buffer after 10 minutes if nothing else triggers it. Any other
        # mode requests with smaller triggers will flush the entire buffer
        # including the deop request. May still be desired behavior, but it's
        # not the same as the old behavior.

    ### The following method implements operations that can be handled by a
    ### connector
    @defer.inlineCallbacks
    def _do_connector_operation(self, operation, channel, target):
        """Handles the operations that require OP but can be handled by a
        connector if available. Does one of: op, deop, voice, devoice, quiet,
        unquiet, or topic (all but kick).  If we are OP in the given channel,
        does the operation ourself.  Otherwise, uses one of the connectors.
        
        """
        # Special case for the topic operation: we do not need op if channel
        # mode +t is not set
        if (operation == "topic" and 
                "t" not in (
                        yield self.transport.issue_request("irc.chanmode", channel)
                        )[0]
                ):
            self.transport.send_event(Event("irc.do_topic",
                channel=channel,
                topic=target,))
            return

        # for everything else we need to know: are we op?
        are_op = (yield self.transport.issue_request("irc.has_op", channel))
        connector = self.config["opmethod"][channel].get(operation)
        if not are_op and connector:
            # Try to send this operation through a connector, if defined
            log.msg("trying the connector {0}".format(connector))
            try:
                yield self.transport.issue_request("connector.{0}.{1}".format(
                        connector, operation),
                        channel, target)
            except NotImplementedError:
                raise OpFailed("Connector {0} is not loaded, does not exist, or does not provide '{1}'".format(connector, operation))
            return

        # Otherwise, do the operation ourself
        modes = {"op":      "+o",
                 "deop":    "-o",
                 "voice":   "+v",
                 "devoice": "-v",
                 "quiet":   "+q",
                 "unquiet": "-q",
                 }
        if operation in modes:
            # Delegate
            self._do_mode(channel, modes[operation], param=target)

        elif operation == "topic":
            # Acquire op if and change the topic. We know we need op because we
            # already checked for +t at the beginning of this method.
            yield self._wait_for_op(channel)
            self.transport.send_event(Event("irc.do_topic",
                channel=channel,
                topic=target,))
        else:
            # Could happen if we defined more connector operations than we have
            # coded handlers for in this method.
            raise Exception("Unknown mode. This is a bug")


    ### What follows here are methods to perform certain operations that don't
    ### have a connector. That is, we have to do them ourselves as OP.
    @defer.inlineCallbacks
    def _do_kick(self, channel, target, reason):
        kickevent = Event("irc.do_kick", channel=channel,
                user=target, reason=reason)
        yield self._wait_for_op(channel)
        self.transport.send_event(kickevent)

    @defer.inlineCallbacks
    def _do_mode(self, channel, mode, param=None, delay=1):
        """Handles arbitrary mode requests that aren't handled by a connector.
        This method never uses a connector and will always acquire op to
        perform the mode request (may use a connector to acquire op, though).

        mode is a two character string, where the first character is + or - and
        the second is the mode character.
        
        param is the parameter, if any, or None.

        This request can only handle one mode change per call. They are
        internally buffered so if you need more than one mode change simply
        issue more than one request.

        the 4th optional parameter delay specifies how long it should wait for
        other mode requests to come in before it will force the buffer flushed
        and all outstanding mode requests are fulfilled. The mode request may
        be fulfilled earlier. The default is 1 second.
        
        """
        # Add the mode request(s) to the buffer
        self.mode_buffer[channel].add((mode, param))

        # Wait delay seconds for any other mode requests to come in so we can
        # de-duplicate the requests and make one mode request to the irc server
        if (yield self.wait_for(Event("ircop.mode_buffer_emptied"), timeout=delay)):
            # Something else handled it in this interval? okay bail
            return

        ### Handle the mode buffer here
        yield self._wait_for_op(channel)

        modelist = list(self.mode_buffer[channel])
        self.mode_buffer[channel].clear()

        # If there is a self-deop mode request in here, re-order it to be last
        mynick = (yield self.transport.issue_request("irc.getnick")),
        modelist.sort(key=lambda x: x[0] == "-o" and x[1] == mynick)

        modeline = ""
        params = []
        for i, modereq in enumerate(modelist):
            if len(modereq[0]) == 1:
                modeline += "+"+modereq[0]
            elif len(modereq[0]) == 2:
                modeline += modereq[0]
            else:
                log.msg("Warning: Invalid mode request in buffer: {0!r}. Ignoring.".format(modereq))
                continue

            if modereq[1]:
                params.append(modereq[1])

            # If this was the last of the buffer or we've accumulated 3
            # requests, send them to the server. The length below is 6 because
            # there are 2 chars per mode request, the + or -, and the letter.
            if i == len(modelist)-1 or len(modeline) >= 6:
                # Send the mode line ourselves as a do_raw because do_mode can
                # only set or unset one thing at a time.
                log.msg("Sending mode requests {0} {1}".format(modeline, params))
                self.transport.send_event(Event("irc.do_raw",
                    line="MODE {channel} {modeline} {params}".format(
                        channel=channel,
                        modeline=modeline,
                        params=" ".join(params),
                        )))
                modereq = ""
                params = []
        # This signals any other instances of this method currently waiting
        # that the buffer has been fulfilled and they should not bother
        self.transport.send_event(Event("ircop.mode_buffer_emptied"))
        log.msg("Mode buffer emptied")


    def _do_become_op(self, channel, duration):
        return self._wait_for_op(channel, duration)
