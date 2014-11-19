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
* Connector plugins that interface with a method of performing IRC operator
  operations. They expose one method per operation (quiet, deop, etc)
  - Chanserv connector: sends requests to chanserv
  - Weechat connector: sends requests to chanserv via a local weechat fifo

  These plugins should be relatively lightweight. These plugins don't have any
  return value, and never error. Some connectors may have no way of detecting
  if it didn't work, so detection of whether the request worked must happen at
  a higher level.

* The OpProvider plugin provides a unified interface to OP functions. It takes
  care of choosing which connector to use (or sending a mode request directly
  to the irc plugin itself) depending on the channel, the command, the
  current OP status of the bot, and the plugin's configuration.

  Specific features of this plugin:
  - exposes to other plugins one function per operation: op, deop, voice,
    devoice, quiet, unquiet, ban, unban, kick, and set topic
  - keeps track of what it can do in which channels and how. For example, some
    channels it may can do everything through chanserv, but others it may can
    only OP itself with chanserv and must do everything else itself.
  - if it has a method of acquiring OP, it will automatically de-op itself when
    it's not needed.
  - provides a become_op() function for other plugins to force it to hold OP
    for a while
  - Submits multiple mode requests in batch, and submits multiple operations in
    one op session before deopping, if possible

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

        paths = glob.glob(os.path.expanduser("~/.weechat/weechat_fifo_*"))
        if len(paths) == 0:
            raise IndexError("No weechat fifo connectors found. Cannot use weechat connector")

        path = paths[0]

        log.msg("Weechat connector sending command {0} {1} {2}".format(operation, channel, nick))
        with open(path, 'w') as out:
            out.write(u"{weechat_server} */msg ChanServ {op} {channel} {nick}\n".format(
                weechat_server=self.config['weechat_server'],
                op=operation,
                channel=channel,
                nick=nick,
                ).encode("UTF-8"))

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
            message=u"{op} {channel} {nick}".format(
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

    Each request returns a deferred that will callback when the operation
    succeeds. The deferred may errback with an OpFailed error if the operation
    required the bot gain OP but OP could not be acquired.

    The plugin will automatically deop itself after performing operations if
    the config defines a way to gain OP.

    Requests are internally buffered and batched so that several simultaneous
    requests will all be submitted with one OP+DEOP cycle. For this to work,
    callers should submit all requests before waiting for any of them to
    callback/errback.

    (If you wait for each submitted operation, then you end up waiting for each
    one to be completely processed before the next one, forcing each operation
    to re-op and process in individual batches. If the API were changed so that
    requests return immediately, this would make the batching more fool-proof
    for callers, but then the caller couldn't be informed of errors in
    acquiring OP. Therefore, this burden must be placed on the caller.)

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
    OTHER_REQS = frozenset(['kick', 'become_op', 'mode', 'ban', 'unban'])

    def start(self):
        super(OpProvider, self).start()

        # A unix timestamp that we should hold op until, tracked per-channel.
        # Used to keep track of op requests from the become_op request call.
        self.op_until = defaultdict(float)

        # A unix timestamp for when we should process the buffers. Set by
        # _set_buffer_processor_timer()
        self.buffer_timer = defaultdict(float)

        # This plugin keeps three internel buffers per channel, stored in the
        # following three attribute variables. Each is a dict mapping channel
        # names to a set of tuples.
        # mode_buffer sets contain (mode, argument, deferred)
        # event_buffer sets contain (Event, deferred)
        # connector_buffer sets contain (operation_name, param, deferred)
        # The typical workflow is for handler methods to add an item to one or
        # more of the buffers and then call _set_buffer_processor_timer(),
        # which will set a timer to process the buffer after a brief delay.
        self.mode_buffer = defaultdict(set)
        self.event_buffer = defaultdict(set)
        self.connector_buffer = defaultdict(set)

        # Register the requests we handle
        for operation in self.CONNECTOR_REQS | self.OTHER_REQS:
            self.provides_request("ircop.{0}".format(operation))

        # Events we listen for
        self.listen_for_event("ircutil.hasop.acquired")
        self.listen_for_event("ircutil.hasop.lost")
        self.listen_for_event("irc.on_join")

    def reload(self):
        super(OpProvider, self).reload()
        
        # Keeps a mapping of channels to a dict mapping operations to connectors.
        self.config["opmethod"] = defaultdict(dict, self.config["opmethod"])

    def on_event_irc_on_join(self, event):
        """Convenience: when we join a channel, see if this channel exists in
        the config, and create the config items for it

        """
        channel = event.channel
        defined_reqs = set(self.config["opmethod"][channel].keys())
        undefined_reqs = self.CONNECTOR_REQS - defined_reqs
        if undefined_reqs:
            for x in undefined_reqs:
                self.config["opmethod"][channel][x] = None
            self.config.save()

    def incoming_request(self, reqname, *args, **kwargs):
        # Request dispatch
        # Choose the appropriate handler here.
        reqname = reqname.split(".")[-1]
        if reqname in self.CONNECTOR_REQS:
            return self._do_connector_operation(reqname, *args, **kwargs)
        elif reqname in self.OTHER_REQS:
            return getattr(self, "_do_{0}".format(reqname))(*args, **kwargs)

    ### The following helper methods are used in implementing this plugin's
    ### functions
    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _wait_for_op(self, channel):
        """Returns a deferred that fires when the bot has op in the named
        channel, which may be immediately if the bot already has OP. If the bot
        does not have op, it will be requested and the defer will fire when it
        is acquired. Errbacks with an OpFailed error if op is un-acquirable in
        the channel.

        """
        # If we have op, just return immediately.
        if (yield self.transport.issue_request("irc.has_op", channel)):
            return
        # Start an event watcher immediately to help curb race conditions
        # involved in op being acquired after we check but before the event
        # watcher is active. Actually I don't think a race condition is even
        # possible, but it doesn't hurt to do this anyways. (notice how we
        # don't yield-wait for this until after)
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
            log.msg("Error: Connector {0} is not loaded, does not exist, or does not provide 'op'".format(connector))
            raise OpFailed("I am not configured correctly to acquire OP on {0}".format(channel))

        # Wait for op to be acquired by waiting for the event watcher started
        # at the beginning of this method.
        if not (yield op_waiter):
            raise OpFailed("Timeout waiting for OP. Do I have the correct permission with e.g. Chanserv?")

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _deop_later(self, channel):
        """Waits until the current time reaches the timestamp stored in
        self.op_until, and then issues a deop request

        This should be called after setting self.op_until[channel] to some
        timestamp in the future. Right now it is only called from
        _do_become_op().

        Returns a deferred that fires when we deop. but it doesn't make much
        sense to wait for it. at least not in the context of _do_become_op()       

        """
        while self.op_until[channel] - time.time() > 0:
            if (yield self.wait_for(
                    Event("irc.hasop.lost", channel=channel),
                    timeout=self.op_until[channel] - time.time())
                    ):
                # Lost op by something else? Manual intervention? okay fine
                # cancel this
                log.msg("Op cancelled before timer. Did you do that?")
                self.op_until[channel] = time.time()
                return

        log.msg("op_until reached: issuing a -o mode request in {0}".format(channel))
        yield self._do_mode(channel, "-o",
                (yield self.transport.issue_request("irc.getnick")),
                )

    def _set_buffer_processor_timer(self, channel):
        """Indicates an item has been added to one of the buffers and we should
        process it shortly. (x seconds after the last call to this method)

        Request handlers should call this method after adding an item to the
        mode or event buffer.

        This method sets a timer to process the buffers in a few tenths of a
        second. This is so that requests by the code can be batched together to
        the server. Otherwise we risk getting OP to process a request a split
        second before the code makes another request.

        Note: it is no longer recommended that callers also call _wait_for_op()
        along with calling this method, since a race condition may cause us to
        not deop after the buffers are processed. The extra 2 tenths of a
        second shouldn't matter much anyways.
        
        This method returns no value, and returns immediately.

        """
        # The time here is mostly arbitrary, but 0.2 seconds seems like a good
        # time that gives the code a chance to submit multiple requests, while
        # not delaying potentially time sensitive operations too much.
        # Operations that we may want to push to the server as soon as
        # possible.
        self.buffer_timer[channel] = time.time() + 0.2
        self._wait_buffer_processor_timer(channel)

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _wait_buffer_processor_timer(self, channel):
        """Called only by _set_buffer_processor_timer() to wait for the
        buffer_timer to expire, and then call _process_buffer(). This is
        implemented as a separate method with inlineCallbacks so that
        _set_buffer_processor_timer() can return while this continues to wait
        asynchronously.
        
        """
        while self.buffer_timer[channel] - time.time() > 0:
            yield self.wait_for(timeout=self.buffer_timer[channel] - time.time())

        self._process_buffer(channel)

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _process_buffer(self, channel):
        """Processes the buffers right now. This is only called from
        _wait_buffer_processor_timer(), and should not be called directly by
        handlers. (handlers should call _set_buffer_processor_timer() unless
        they have a reason to process the buffer *right this instant*)

        If there is a method of gaining op defined (a connector for the 'op'
        method is set) and we're not holding op, a deop-self request will be
        submitted as part of the last mode request sent to the server.

        """
        already_opped = (yield self.transport.issue_request("irc.has_op",
            channel))

        # If there are items in the connector_buffer but the other buffers are
        # empty, then process them with a connector and exit. Otherwise, since
        # we'll have to gain OP anyways, skip using the connector and do
        # everything ourself.
        # Also if the connector buffer is 3 or more items long, it's more
        # efficient for us to just do the operations ourself.
        # This is the only opportunity we have to process items with a
        # connector.
        if (
                self.connector_buffer[channel]
                and len(self.connector_buffer[channel]) < 3
                and (not self.event_buffer[channel])
                and (not self.mode_buffer[channel])
                and not already_opped
                ):
            # Send all connector items to their connector plugins and callback
            # the deferreds.
            for operation, param, d in self.connector_buffer.pop(channel, set()):
                try:
                    yield self.transport.issue_request(
                            "connector.{0}.{1}".format(
                                self.config['opmethod'][channel][operation],
                                operation),
                            channel,
                            param,
                            )
                except NotImplementedError:
                    log.msg("Error: Connector {0} is not loaded, does not exist, or does not provide '{1}'".format(
                        self.config['opmethod'][channel][operation],
                        operation))
                    d.errback(OpFailed("I am not configured correctly to do {1} on {0}".format(channel, operation)))
                else:
                    d.callback(None)
            return

        # Acquire op here. We'll need it. (if we already have it, this will
        # fall right through)
        try:
            yield self._wait_for_op(channel)
        except OpFailed as e:
            # We need OP but couldn't get it. Send an errback to all items in
            # the mode buffer and event buffer. Send all connector buffer items
            # to their connectors (because we can still do them).
            for mode, arg, d in self.mode_buffer.pop(channel, set()):
                d.errback(e)
            for event, d in self.event_buffer.pop(channel, set()):
                d.errback(e)
            for operation, param, d in self.connector_buffer.pop(channel, set()):
                try:
                    yield self.transport.issue_request(
                            "connector.{0}.{1}".format(
                                self.config['opmethod'][channel][operation],
                                operation),
                            channel,
                            param,
                            )
                except NotImplementedError:
                    log.msg("Error: Connector {0} is not loaded, does not exist, or does not provide '{1}'".format(
                        self.config['opmethod'][channel][operation],
                        operation))
                    d.errback(OpFailed("I am not configured correctly to do {1} on {0}".format(channel, operation)))
                else:
                    d.callback(None)
            return

        # At this point we're doing everything ourself as OP. Convert the
        # connector operations into a mode or an event item and add them to
        # those buffers.
        for operation, param, d in self.connector_buffer.pop(channel, set()):
            self._convert_connector(operation, channel, param, d)

        # Submit all events in the event buffer
        for event, d in self.event_buffer.pop(channel, set()):
            self.transport.send_event(event)
            d.callback(None)

        # Now process the mode buffer. We make an ordered list so that we may
        # put a deop request at the end.
        modelist = list(self.mode_buffer[channel])
        self.mode_buffer[channel].clear()

        # If there is a self-deop mode request in here already, re-order it to
        # be last
        mynick = (yield self.transport.issue_request("irc.getnick"))
        is_self_deop = lambda x: x[0] == "-o" and x[1] == mynick
        modelist.sort(key=is_self_deop)

        # If there is an OP mode request for us, then remove it, because at
        # this point in the code we have already acquired op. A self-op mode
        # could get this far in the code in a couple ways.  When we get a
        # request to OP ourself explicitly (!op bot), which is usually
        # fulfilled by a connector, but wasn't for some reason, then we get OP
        # to process the buffers and one of the items in the mode buffer is a
        # +o on the bot This special case removes the +o mode request and sets
        # the already_opped flag to not remove OP after, thus fulfilling the
        # intent of the request
        is_self_op = lambda x: x[0] == "+o" and x[1] == mynick
        if any(is_self_op(m) for m in modelist):
            already_opped = True
            modelist = [m for m in modelist if not is_self_op(m)]


        # Check if we should insert a deop request to the end of the mode list
        if (
                # if there's not already one...
                (not modelist or not is_self_deop(modelist[-1]))
                # ... and if a connector is defined for OP
                and (self.config["opmethod"][channel].get("op"))
                # ... and we're not in "hold op" mode
                and (self.op_until[channel] < time.time())
                # ... and only if we had to acquire OP ourself to fulfill this
                # request. (don't relinquish if someone gave it to us
                # explicitly)
                and (not already_opped)
                ):
            modelist.append(("-o", mynick, defer.Deferred()))
        else:
            log.msg("Not issuing a deop request because...")
            if already_opped:
                log.msg("  ...we are already opped")
            if self.op_until[channel] >= time.time():
                log.msg("  ...are in 'hold op' mode for {0} more seconds".format(self.op_until[channel]-time.time()))
            if not self.config["opmethod"][channel].get("op"):
                log.msg("  ...we have no way of reacquiring op on {0}".format(channel))
            if modelist and is_self_deop(modelist[-1]):
                log.msg("  ...the last mode in the queue is already a self-deop")

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

            # If this was the last of the buffer or we've accumulated 4
            # requests, send them to the server. The length below is 8 because
            # there are 2 chars per mode request, the + or -, and the letter.
            # CONFIGURE THIS VALUE to change how many modes can be sent in a
            # single request. TODO: get this value from the server
            # automatically (it's sent on join I think)
            if i == len(modelist)-1 or len(modeline) >= 8:
                # Send the mode line ourselves as a do_raw because do_mode can
                # only set or unset one thing at a time.
                log.msg("Sending mode requests {0} {1}".format(modeline, params))
                self.transport.send_event(Event("irc.do_raw",
                    line="MODE {channel} {modeline} {params}".format(
                        channel=channel,
                        modeline=modeline,
                        params=" ".join(params),
                        )))
                modeline = ""
                params = []
        for _,_,d in modelist:
            d.callback(None)
        log.msg("buffers emptied for {0}".format(channel))

    def _convert_connector(self, operation, channel, target, d):
        """Called when a connector request cannot or will not be fulfilled by
        the connector plugin. This method adds an item to the event buffer or
        mode buffer.

        This method defines how to perform all connector operations ourself as
        op. (Add a new connector operation? Make sure to change this method to
        match)

        """
        modes = {"op":      "+o",
                 "deop":    "-o",
                 "voice":   "+v",
                 "devoice": "-v",
                 "quiet":   "+q",
                 "unquiet": "-q",
                 }
        if operation in modes:
            self.mode_buffer[channel].add((modes[operation], target, d))

        elif operation == "topic":
            self.event_buffer[channel].add((
                Event("irc.do_topic", channel=channel, topic=target),
                d,
            ))
        else:
            # Could happen if we defined more connector operations than we have
            # coded handlers for in this method.
            raise Exception("Unknown mode. This is a bug")

    ### The following methods implement the request handlers exported to other
    ### plugins

    @defer.inlineCallbacks
    def _do_connector_operation(self, operation, channel, target):
        """This is the entry point for inter-plugin requests that are handled
        by connectors (i.e. can be sent to chanserv instead of having to OP
        ourself)
        
        Does one of: op, deop, voice, devoice, quiet, unquiet, or topic.
        Chooses whether to use a connector or do the operation ourself
        depending on the config or other factors.

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

        # If a connector is not defined, we can convert it right away. This
        # lets us make the assumption in _process_buffer() that all connector
        # operations in the connector_buffer are defined and we don't need to
        # do this check there. (They could still fail with a
        # NotImplementedError if e.g. the connector plugin is not loaded,
        # however)
        if not self.config['opmethod'][channel].get(operation, None):
            d = defer.Deferred()
            self._convert_connector(operation, channel, target, d)
            # wait for the operation to finish, then return to the caller.
            # Yielding for this deferred will also propagate errors encountered
            # when processing the buffer to our caller.
            self._set_buffer_processor_timer(channel)
            yield d
            return

        # If the operation is a quiet or unquiet, and the target is an extban,
        # then workaround a bug in chanserv and submit this to do ourself as a
        # mode request instead of a connector request.
        if operation in ("quiet","unquiet") and target.startswith("$"):
            d = defer.Deferred()
            self._convert_connector(operation, channel, target, d)
            self._set_buffer_processor_timer(channel)
            yield d
            return


        # Go ahead and submit this as a connector operation. For now. Later on
        # when the buffer is processed, it may decide to process this ourself
        # instead of using the connector if for example we have to acquire OP
        # for some other reason.
        d = defer.Deferred()
        self.connector_buffer[channel].add((operation, target, d))

        self._set_buffer_processor_timer(channel)
        # d will return when the request is fulfilled or err trying
        yield d

    @defer.inlineCallbacks
    def _do_mode(self, channel, mode, param=None):
        """Called to implement ircop.mode. Handles arbitrary mode requests that
        aren't handled by a connector. This method never uses a connector and
        will always acquire op to perform the mode request (may use a connector
        to acquire op, though).

        mode is a two character string, where the first character is + or - and
        the second is the mode character.
        
        param is the parameter, if any, or None.

        Results are undefined if you specify a parameter for a mode that
        doesn't take one, or don't specify a parameter for a mode that requires
        one.

        This request can only handle one mode change per call. They are
        internally buffered so if you need more than one mode change simply
        issue more than one request.

        """
        # First do some error checking. The add list and remove list are the
        # channel modes that take parameters when being added and removed,
        # respectively.
        add_params, rem_params = (yield self.transport.issue_request("irc.get_channel_mode_params"))
        if len(mode) != 2 or mode[0] not in ("+","-"):
            raise ValueError("Invalid mode string")
        if mode[0] == "+":
            chklist = add_params
        else:
            chklist = rem_params
        if mode[1] in chklist and not param:
            raise ValueError("You must specify a parameter with {0}".format(mode))
        elif mode[1] not in chklist and param:
            raise ValueError("Mode {0} does not take a parameter".format(mode))

        # Add the mode request(s) to the buffer
        d = defer.Deferred()
        self.mode_buffer[channel].add((mode, param, d))

        # Process the buffers since an item has been added to the mode buffer
        self._set_buffer_processor_timer(channel)
        yield d

    @defer.inlineCallbacks
    def _do_become_op(self, channel, duration):
        """Tells the bot to hold op for the given duration, in seconds. The bot
        will attempt to gain OP and will not relinquish it on its own until the
        given time is up.
        
        The returned deferred fires as soon as OP is acquired.

        """
        already_opped = (yield self.transport.issue_request("irc.has_op",
            channel))
        if already_opped:
            # If we already have op, it could be for any number of reasons, but
            # they all involve overriding the behavior of wanting to gain op
            # for *only* the next duration seconds and instead keep our
            # existing op indefinitely (by not calling _deop_later or anything)
            return
        yield self._wait_for_op(channel)
        self.op_until[channel] = max(self.op_until[channel], time.time()+duration)
        self._deop_later(channel)

    def _do_ban(self, channel, target):
        """A shorthand for submitting a mode request for +b"""
        return self._do_mode(channel, "+b", param=target)

    def _do_unban(self, channel, target):
        """A shorthand for submitting a mode request for -b"""
        return self._do_mode(channel, "-b", param=target)

    def _do_kick(self, channel, target, reason):
        """Gains op and performs a kick"""
        kickevent = Event("irc.do_kick", channel=channel,
                user=target, reason=reason)
        d = defer.Deferred()
        self.event_buffer[channel].add((kickevent, d))
        self._set_buffer_processor_timer(channel)
        return d
