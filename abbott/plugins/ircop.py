import glob
from collections import defaultdict
import time
import os.path

from twisted.python import log
from twisted.internet import defer

from ..transport import Event
from ..pluginbase import BotPlugin, EventWatcher, non_reentrant

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
    """Provides a unified interface for other plugins to various IRC operator
    tasks.  Provides the following requests in the form of ircop.X where X is
    one of: op, deop, voice, devoice, quiet, unquiet, ban, unban, kick, topic.
    All take a first parameter: channel. All except topic take a second
    parameter: a hostmask or nick.

    Also provides ircop.become_op which takes two parameters: a channel name
    and a duration, in seconds. When called, the bot will attempt to acquire op
    status in the given channel and  hold it for (at least) the given duration.

    Each request will try to acquire op before returning, and raise an OpFailed
    exception if Op cannot be acquired. Requests are internally buffered and
    this plugin will attempt to issue all requests at once, and then deop.

    If a caller wishes to submit more than one event, it should submit all but
    the last without waiting for the returned deferred to fire, and then wait
    on the last one. The reason is so that all the requests get submitted at
    the same time. If a caller waits for one request to return, then that means
    op has been acquired and the submitted request could be processed at any
    moment. If the request is processed before the next submitted request, then
    the bot will be forced to op a second time. Minimize the chances by not
    waiting for any but the last request. (or wait for none of them if you
    don't care about catching and handling errors)

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
    OTHER_REQS = frozenset(['kick', 'become_op', 'mode', 'ban', 'unban'])

    def start(self):
        super(OpProvider, self).start()

        # A unix timestamp that we should hold op until, tracked per-channel.
        # Used to keep track of op requests from the become_op request call.
        self.op_until = defaultdict(float)

        # A unix timestamp for when we should process the mode and event
        # buffers. Set by _set_buffer_processor_timer
        self.buffer_timer = defaultdict(float)

        # holds a timer which, when expired, will process the mode and event
        # buffers
        self.buffer_timer = defaultdict(float)

        # A per-channel buffer of mode requests that we will fulfill shortly.
        # This is held so that we may de-duplicate mode requests. Each item is
        # a tuple: (mode, argument), where argument may be None.
        self.mode_buffer = defaultdict(set)

        # A per-channel event buffer, holding events that we are going to emit
        # once we gain op. These events are held explicitly in this buffer, as
        # opposed to having each request method block waiting for op, so that
        # we can guarantee the event_buffer is processed before the
        # mode_buffer. This is important because the deop request will be part
        # of the mode buffer, and we can't deop before all events are
        # submitted. Each set holds Event objects
        self.event_buffer = defaultdict(set)

        for operation in self.CONNECTOR_REQS | self.OTHER_REQS:
            self.provides_request("ircop.{0}".format(operation))

        self.listen_for_event("ircutil.hasop.*")

    def reload(self):
        super(OpProvider, self).reload()
        
        # Keeps a mapping of channels to a dict mapping operations to connectors.
        self.config["opmethod"] = defaultdict(dict, self.config["opmethod"])

    def incoming_request(self, reqname, *args, **kwargs):
        # Choose the appropriate handler here.
        reqname = reqname.split(".")[-1]
        if reqname in self.CONNECTOR_REQS:
            self._do_connector_operation(reqname, *args, **kwargs)
        elif reqname in self.OTHER_REQS:
            return getattr(self, "_do_{0}".format(reqname))(*args, **kwargs)

    ### The following helper methods are used in implementing this plugin's
    ### functions
    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _wait_for_op(self, channel):
        """Returns a deferred that fires when the bot has op in the named
        channel, which may be immediately. If the bot does not have op, it will
        be requested and the defer will fire when it is acquired. May error if
        op is un-acquirable in the channel.

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

        log.msg("We need op. Asking the {0} connector".format(connector))
        try:
            yield self.transport.issue_request("connector.{0}.op".format(connector),
                    channel=channel,
                    nick = nick,
                    )
        except NotImplementedError:
            raise OpFailed("Connector {0} is not loaded, does not exist, or does not provide 'op'".format(connector))

        # Wait for op to be acquired by waiting for the event watcher started
        # at the beginning of this method.
        if not (yield op_waiter):
            raise OpFailed("Timeout in waiting for op request to be fulfilled")

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _deop_later(self, channel):
        """Waits until the current time reaches the timestamp stored in
        self.op_until, and then issues a deop request

        This is called from _do_become_op() after setting
        self.op_until[channel] to some timestamp in the future.
        
        """
        while self.op_until[channel] - time.time() > 0:
            if (yield self.wait_for(
                    Event("irc.hasop.lost", channel=channel),
                    timeout=self.op_until[channel] - time.time())
                    ):
                # Lost op by something else? okay fine cancel this
                log.msg("Op cancelled before timer. Did you do that?")
                self.op_until[channel] = time.time()
                return

        log.msg("op_until reached: issuing a -o mode request in {0}".format(channel))
        self._do_mode(channel, "-o",
                (yield self.transport.issue_request("irc.getnick")),
                )

    def _set_buffer_processor_timer(self, channel):
        """Indicates an item has been added to the buffer and we should process
        it shortly. (x seconds after the last call to this method)

        Request handlers should call this method after adding an item to the
        mode or event buffer.

        """
        # The time to wait here pretty much doesn't matter, because as a
        # minimum we must wait for chanserv to respond and op us, which
        # typically takes around 2 seconds, and could be as many as 20.
        self.buffer_timer[channel] = time.time() + 0.5
        self._wait_buffer_processor_timer(channel)

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _wait_buffer_processor_timer(self, channel):
        """Called by _set_buffer_processor_timer to process the buffer when the
        timer expires
        
        """
        while self.buffer_timer[channel] - time.time() > 0:
            yield self.wait_for(timeout=self.buffer_timer[channel] - time.time())

        self._process_buffer(channel)

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _process_buffer(self, channel):
        """Processes the mode and event buffers right now

        If there is a method of gaining op defined, a deop-self request will be
        submitted as part of the last mode request sent to the server

        This is called by _wait_buffer_processor_timer when the buffer timer
        expires

        """

        # Acquire op here
        yield self._wait_for_op(channel)

        # Submit all events in the event buffer
        for event in self.event_buffer.pop(channel, set()):
            self.transport.send_event(event)

        # Now process the mode buffer. We make an ordered list so that we may
        # put a deop request at the end.
        modelist = list(self.mode_buffer[channel])
        self.mode_buffer[channel].clear()

        # If there is a self-deop mode request in here already, re-order it to
        # be last
        mynick = (yield self.transport.issue_request("irc.getnick"))
        is_self_deop = lambda x: x[0] == "-o" and x[1] == mynick
        modelist.sort(key=is_self_deop)

        if not modelist or not is_self_deop(modelist[-1]):
            # If the last item in the mode list is not a self-deop, check if we
            # should add one
            if self.config["opmethod"][channel].get("op"):
                # Yes a connector is defined.

                if self.op_until[channel] < time.time():
                    # And yes, we're not currently in hold-op mode
                    modelist.append(("-o", mynick))

        # Loop through all the mode requests and combine them to submit them to
        # the server in batch.
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
        log.msg("buffers emptied for {0}".format(channel))



    ### The following methods implement the request handlers exported to other
    ### plugins

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
            yield self._do_mode(channel, modes[operation], param=target)

        elif operation == "topic":
            # Acquire op if and change the topic. We know we need op because we
            # already checked for +t at the beginning of this method.
            yield self._wait_for_op(channel)
            self.event_buffer[channel].add(Event("irc.do_topic",
                channel=channel,
                topic=target,))
            self._set_buffer_processor_timer(channel)
        else:
            # Could happen if we defined more connector operations than we have
            # coded handlers for in this method.
            raise Exception("Unknown mode. This is a bug")

    @defer.inlineCallbacks
    def _do_kick(self, channel, target, reason):
        kickevent = Event("irc.do_kick", channel=channel,
                user=target, reason=reason)
        self.event_buffer[channel].add(kickevent)
        self._set_buffer_processor_timer(channel)
        yield self._wait_for_op(channel)

    @defer.inlineCallbacks
    def _do_mode(self, channel, mode, param=None):
        """Handles arbitrary mode requests that aren't handled by a connector.
        This method never uses a connector and will always acquire op to
        perform the mode request (may use a connector to acquire op, though).

        mode is a two character string, where the first character is + or - and
        the second is the mode character.
        
        param is the parameter, if any, or None.

        This request can only handle one mode change per call. They are
        internally buffered so if you need more than one mode change simply
        issue more than one request.

        """

        # Add the mode request(s) to the buffer
        self.mode_buffer[channel].add((mode, param))

        self._set_buffer_processor_timer(channel)
        yield self._wait_for_op(channel)

    @defer.inlineCallbacks
    def _do_become_op(self, channel, duration):
        yield self._wait_for_op(channel)
        self.op_until[channel] = max(self.op_until[channel], time.time()+duration)
        self._deop_later(channel)

    def _do_ban(self, channel, target):
        """A shorthand for submitting a mode request for +b"""
        return self._do_mode(channel, "+b", param=target)
    def _do_unban(self, channel, target):
        """A shorthand for submitting a mode request for -b"""
        return self._do_mode(channel, "-b", param=target)
