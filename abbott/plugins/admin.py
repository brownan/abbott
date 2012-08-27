from collections import defaultdict, deque
import shlex
from functools import wraps
import glob, os.path

from twisted.internet import reactor
from twisted.python import log
from twisted.internet.utils import getProcessOutput
from twisted.internet import defer

from ..command import CommandPluginSuperclass
from ..transport import Event
from ..pluginbase import BotPlugin

def require_channel(func):
    """Wraps command callbacks and requires them to be in response to a channel
    message, not a private message directed to the bot.

    """
    @wraps(func)
    def newfunc(self, event, match):
        if event.direct:
            event.reply("Hey, you can't do that in here!")
        else:
            return func(self, event, match)
    return newfunc

# One of the following Chanserv plugins should be running for the rest of the
# plugins to work

class Weechat_Chanserv(BotPlugin):
    """This plugin provides access to chanserv operations through a weechat
    connector.
    It listens for these events:
    * ircadmin.op
    * ircadmin.deop
    * ircadmin.voice
    * ircadmin.devoice
    * ircadmin.quiet
    * ircadmin.unquiet
    each event takes two attributes: channel and nick. Upon reciept of an
    event, a message will be sent to the chanserv by way of a weechat connector
    run by the current user.

    """
    path = "~/.weechat/weechat_fifo_*"
    network = "freenode"

    def start(self):
        self.listen_for_event("ircadmin.op")
        self.listen_for_event("ircadmin.deop")
        self.listen_for_event("ircadmin.voice")
        self.listen_for_event("ircadmin.devoice")
        self.listen_for_event("ircadmin.quiet")
        self.listen_for_event("ircadmin.unquiet")

    def _send_to_chanserv(self, msg):
        path = glob.glob(os.path.expanduser(self.path))[0]

        with open(path, 'w') as out:
            out.write("irc.server.{network} */msg chanserv {msg}\n".format(
                network=self.network,
                msg=msg.encode("UTF-8")))

    def received_event(self, event):
        operation = event.eventtype.split(".")[1]
        # sanity check
        if operation not in frozenset(["op","deop","voice","devoice","quiet","unquiet"]):
            raise ValueError("Invalid event type")
        self._send_to_chanserv("{operation} {chan} {nick}".format(
            operation=operation,
            chan=event.channel,
            nick=event.nick,
            ))

class Direct_Chanserv(BotPlugin):
    """This plugin provides access to chanserv operations. It messages chanserv
    directly, so the bot must be configured to be logged in and have
    appropriate access with chanserv for any operations to succeed.
    It listens for these events:
    * ircadmin.op
    * ircadmin.deop
    * ircadmin.voice
    * ircadmin.devoice
    * ircadmin.quiet
    * ircadmin.unquiet
    each event takes two attributes: channel and nick. Upon reciept of an
    event, a message will be sent to the chanserv by way of a weechat connector
    run by the current user.

    """
    def start(self):
        self.listen_for_event("ircadmin.op")
        self.listen_for_event("ircadmin.deop")
        self.listen_for_event("ircadmin.voice")
        self.listen_for_event("ircadmin.devoice")
        self.listen_for_event("ircadmin.quiet")
        self.listen_for_event("ircadmin.unquiet")

    def _send_to_chanserv(self, msg):
        event = Event("irc.do_msg",
                user="ChanServ",
                message=msg,
                )
        self.transport.send_event(event)

    def received_event(self, event):
        operation = event.eventtype.split(".")[1]
        # sanity check
        if operation not in frozenset(["op","deop","voice","devoice","quiet","unquiet"]):
            raise ValueError("Invalid event type")
        self._send_to_chanserv("{operation} {chan} {nick}".format(
            operation=operation,
            chan=event.channel,
            nick=event.nick,
            ))

class HoldsOp(BotPlugin):
    """This plugin provides the same interface as the last two ChanServ plugins
    except it expects the bot to hold op and issue the mode changes itself
    insteads of asking chanserv. You probably don't want to set an optimeout on
    any channels if you're using this method.

    """
    def start(self):
        self.listen_for_event("ircadmin.op")
        self.listen_for_event("ircadmin.deop")
        self.listen_for_event("ircadmin.voice")
        self.listen_for_event("ircadmin.devoice")
        self.listen_for_event("ircadmin.quiet")
        self.listen_for_event("ircadmin.unquiet")

    def received_event(self, event):
        operation = event.eventtype.split(".")[1]
        # sanity check
        if operation not in frozenset(["op","deop","voice","devoice","quiet","unquiet"]):
            raise ValueError("Invalid event type")
        event = Event("irc.do_mode",
                chan=event.channel,
                set=operation in frozenset(['op','voice','quiet']),
                modes={'op':    'o',
                       'deop':  'o',
                       'voice': 'v',
                       'devoice':'v',
                       'quiet': 'q',
                       'unquiet':'q',
                       }[operation],
                user=event.nick,
                )
        self.transport.send_event(event)

class OpTimedOut(Exception):
    pass

class OpSelf(CommandPluginSuperclass):
    """This plugin provides a request that ops the bot itself. After a timeout,
    the bot will relinquish OP.

    The request name provided is ircadmin.opself. The one parameter is the
    channel name.

    """
    def start(self):
        super(OpSelf, self).start()

        self.provides_request("ircadmin.opself")
        self.listen_for_event("irc.on_mode_change")
        self.listen_for_event("irc.on_join")

        # Maps channel names to a boolean indicating whether we currently hold
        # op there or not
        self.have_op = defaultdict(bool)

        # Maps channel names to a set of (deferred, IDelayedCall) tuples where
        # deferred is to be called when we get op, and the delayed call will
        # call the errback of the deferred after a timeout has occurred.
        self.waiting_for_op = defaultdict(set)

        # Maps channel names to timeout calls
        # (twisted.internet.interfaces.IDelayedCall) to relinquish op, if we
        # have it
        self.op_timeout_event = {}

        # Initialize the conifig directive
        if "optimeout" not in self.config or \
                not isinstance(self.config['optimeout'], dict):
            self.config['optimeout'] = {}
            self.pluginboss.save()

        self.install_command(
                cmdname="optimeout",
                argmatch=r"(?P<timeout>-?\d+)( (?P<channel>[^ ]+))?$",
                callback=self.set_op_timeout,
                cmdusage="<timeout> [channel]",
                helptext="Sets how long I'll keep OP before I give it up. -1 for forever",
                permission="irc.op",
                )

    def stop(self):
        # Cancel all pending timers
        # Let items in waiting_for_op expire on their own
        
        for delayedcall in self.op_timeout_event.itervalues():
            delayedcall.cancel()

    def _set_op_timeout(self, chan):
        """Sets the op timeout for the given channel. If there is not currently
        an op timeout, makes one. If there is currently a timeout set, resets
        the timer.

        """
        try:
            timeout = self.config['optimeout'][chan]
        except KeyError:
            self.config['optimeout'][chan] = 0
            self.pluginboss.save()
            timeout = 0

        delayedcall = self.op_timeout_event.get(chan, None)
        if timeout < 1:
            # This channel has no timeout set. Remove one if there is one set.
            if delayedcall:
                delayedcall.cancel()

        else:
            # Set a timeout if not set, or reset the timer otherwise
            if delayedcall:
                delayedcall.reset(timeout)
            else:
                # Set a callback to relinquish op
                @defer.inlineCallbacks
                def relinquish():
                    mynick = (yield self.transport.issue_request("irc.getnick"))
                    event = Event("irc.do_mode",
                            chan=chan,
                            set=False,
                            modes="o",
                            user=mynick,
                            )
                    self.transport.send_event(event)
                    log.msg("Relinquishing op")
                    # Go ahead and set this to false here, to avoid race
                    # conditions if a plugin requests op right now after we've
                    # sent the -o mode but before it's granted.
                    self.have_op[chan] = False
                    
                    del self.op_timeout_event[chan]
                self.op_timeout_event[chan] = reactor.callLater(timeout, relinquish)

    def on_request_ircadmin_opself(self, channel):
        if self.have_op[channel]:
            # Already has op. Reset the timeout timer and return success
            self._set_op_timeout(channel)
            return defer.succeed(channel)

        else:
            # Submit an OP request for ourselves
            nickdefer = self.transport.issue_request("irc.getnick")
            def submit(nick):
                event = Event("ircadmin.op",
                        channel=channel,
                        nick=nick,
                        )
                self.transport.send_event(event)
            nickdefer.addCallback(submit)

            # Now create a new deferred object
            d = defer.Deferred()

            # Create a timeout that will cancel this call
            def timedout():
                # call the errback
                d.errback(OpTimedOut("Op request timed out"))
                # Remove the deferred from the waiters. timeout is from the
                # enclosing scope created below
                self.waiting_for_op[channel].remove((d, timeout))
            timeout = reactor.callLater(10, timedout)

            self.waiting_for_op[channel].add((d, timeout))

            return d

    @defer.inlineCallbacks
    def on_event_irc_on_mode_change(self, event):
        """Called when we observe a mode change. Check to see if it was an op
        operation on ourselves

        """
        mynick = (yield self.transport.issue_request("irc.getnick"))

        if (event.set == True and "o" in event.modes and event.args and
                event.args[0] == mynick):
            # Op acquired. Make a note of it
            self.have_op[event.chan] = True

            # Now that we have op, call deferreds on anything that was waiting
            log.msg("I was given op. Calling op callbacks on %s" % event.chan)
            for deferred, timeout in self.waiting_for_op.pop(event.chan, set()):
                timeout.cancel()
                deferred.callback(event.chan)

            # Now that we have op, set a timeout to relinquish it
            self._set_op_timeout(event.chan)

        elif (event.set == False and "o" in event.modes and event.args and
                event.args[0] == mynick):
            # Op gone
            self.have_op[event.chan] = False
            try:
                timeoutevent = self.op_timeout_event[event.chan]
            except KeyError:
                pass
            else:
                timeoutevent.cancel()
                del self.op_timeout_event[event.chan]

    def on_event_irc_on_join(self, event):
        """If we find ourself joining a channel that we thought we had op, then
        we actually don't anymore. This happens on disconnects/reconnects or on
        a manual part/join. Anything that doesn't involve this plugin being
        restarted.

        """
        self.have_op[event.channel] = False
        try:
            timeoutevent = self.op_timeout_event[event.channel]
        except KeyError:
            pass
        else:
            timeoutevent.cancel()
            del self.op_timeout_event[event.channel]

    def set_op_timeout(self, event, match):
        """Configures the op timeout for a channel. This is the event handler
        for the command, not to be confused by the other method of a similar
        name that sets a timeout call to relinquish op. This one just
        configures the timeout value.

        """
        gd = match.groupdict()
        channel = gd['channel']
        timeout = int(gd['timeout'])

        if not channel:
            if event.direct:
                event.reply("on what channel?")
                return
            channel = event.channel

        self.config['optimeout'][channel] = timeout
        self.pluginboss.save()

        timeoutevent = self.op_timeout_event.get(channel, None)
        if timeout < 1:
            event.reply("Done. I'll never give up my op")
            if timeoutevent:
                timeoutevent.cancel()
                del self.op_timeout_event[channel]
        else:
            event.reply("Done. I'll hold op for %s seconds after I get it" % timeout)
            if timeoutevent:
                timeoutevent.reset(timeout)
            elif self.have_op[channel]:
                self._set_op_timeout(channel)


class IRCAdmin(CommandPluginSuperclass):
    """Provides a command interface to IRC operator tasks. Uses one of the
    above chanserv plugins as an interface to do the tasks. Since the bot must
    aquire OP itself for some operations like kick, this plugin relies on the
    above OpSelf plugin to function.

    """

    def start(self):
        super(IRCAdmin, self).start()

        # kick command
        self.install_command(
                cmdname="kick",
                cmdmatch="kick|KICK",
                cmdusage="<nickname> [reason]",
                argmatch = "(?P<nick>[^ ]+)( (?P<reason>.*))?$",
                permission="irc.op.kick",
                prefix=".",
                callback=self.kick,
                helptext="Kicks a user from the current channel")

        # Op commands
        self.install_command(
                cmdname="op",
                prefix=".",
                cmdusage="[nick]",
                argmatch="(?P<nick>[^ ]+)?",
                permission="irc.op.op",
                callback=self.give_op,
                helptext="Gives op to the specified user",
                )
        self.install_command(
                cmdname="deop",
                prefix=".",
                cmdusage="[nick]",
                argmatch="(?P<nick>[^ ]+)?",
                permission="irc.op.op",
                callback=self.take_op,
                helptext="Takes op from the specified user",
                )

        # voice commands
        self.install_command(
                cmdname="voice",
                cmdmatch="voice|VOICE",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.voice,
                helptext="Grants a user voice in the current channel"
                )

        self.install_command(
                cmdname="devoice",
                cmdmatch="devoice|DEVOICE",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.devoice,
                helptext="Revokes a user's voice in the current channel"
                )

        # Quiet commands
        self.install_command(
                cmdname="quiet",
                cmdmatch="quiet|QUIET",
                cmdusage="<nick>",
                argmatch = "(?P<nick>[^ ]+)$",
                prefix=".",
                permission="irc.op.quiet",
                callback=self.quiet,
                helptext="Quiets a user"
                )

        self.install_command(
                cmdname="unquiet",
                cmdmatch="unquiet|UNQUIET",
                cmdusage="<nick>",
                argmatch = "(?P<nick>[^ ]+)$",
                prefix=".",
                permission="irc.op.quiet",
                callback=self.unquiet,
                helptext="Un-quiets a user"
                )


    @require_channel
    @defer.inlineCallbacks
    def kick(self, event, match):
        """A user has issued the kick command. Our job here is to acquire OP
        for this channel and set a callback to issue a kick event

        """
        groupdict = match.groupdict()
        nick = groupdict['nick']
        reason = groupdict.get("reason", None)
        channel = event.channel

        kickevent = Event("irc.do_kick", channel=channel,
                user=nick, reason=reason)

        try:
            yield self.transport.issue_request("ircadmin.opself", channel)
        except OpTimedOut:
            event.reply("I could not become OP. Check the error log, configuration, etc.")
        else:
            self.transport.send_event(kickevent)

    @require_channel
    def voice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        voiceevent = Event("ircadmin.voice",
                channel=channel,
                nick=nick,
                )
        log.msg("Voicing %s in %s" % (nick, channel))
        self.transport.send_event(voiceevent)

    @require_channel
    def devoice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        voiceevent = Event("ircadmin.devoice",
                channel=channel,
                nick=nick,
                )
        log.msg("De-voicing %s in %s" % (nick, channel))
        self.transport.send_event(voiceevent)

    @require_channel
    def give_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        opevent = Event("ircadmin.op",
                channel=channel,
                nick=nick,
                )
        log.msg("Opping %s in %s" % (nick, channel))
        self.transport.send_event(opevent)

    @require_channel
    def take_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        opevent = Event("ircadmin.deop",
                channel=channel,
                nick=nick,
                )
        log.msg("De-Opping %s in %s" % (nick, channel))
        self.transport.send_event(opevent)

    @require_channel
    def quiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        channel = event.channel

        newevent = Event("ircadmin.quiet",
                channel=channel,
                nick=nick,
                )
        log.msg("quieting %s in %s" % (nick, channel))
        self.transport.send_event(newevent)

    @require_channel
    def unquiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        channel = event.channel

        newevent = Event("ircadmin.unquiet",
                channel=channel,
                nick=nick,
                )
        log.msg("unquieting %s in %s" % (nick, channel))
        self.transport.send_event(newevent)


class IRCTopic(CommandPluginSuperclass):
    def start(self):
        super(IRCTopic, self).start()

        # Topic commands
        topicgroup = self.install_cmdgroup(
                grpname="topic",
                prefix=None,
                permission="irc.op.topic",
                helptext="Topic manipulation commands",
                )

        topicgroup.install_command(
                cmdname="append",
                cmdmatch="append|push",
                cmdusage="<text>",
                argmatch="(?P<text>.+)$",
                permission=None, # Inherits permissions from the group
                callback=self.topicappend,
                helptext="Appends text to the end of the channel topic",
                )
        topicgroup.install_command(
                cmdname="insert",
                cmdmatch=None,
                cmdusage="<pos> <text>",
                argmatch=r"(?P<pos>-?\d+) (?P<text>.+)$",
                callback=self.topicinsert,
                helptext="Inserts text into the topic at the given position",
                )

        topicgroup.install_command(
                cmdname="replace",
                cmdmatch="set|replace",
                cmdusage="<pos> <text>",
                argmatch=r"(?P<pos>-?\d+) (?P<text>.+)$",
                callback=self.topicreplace,
                helptext="Replaces the given section with the given text",
                )

        topicgroup.install_command(
                cmdname="remove",
                cmdmatch=None,
                cmdusage="<pos>",
                argmatch=r"(?P<pos>-?\d+)$",
                callback=self.topicremove,
                helptext="Removes the pos'th topic selection",
                )
        topicgroup.install_command(
                cmdname="pop",
                callback=self.topicpop,
                helptext="Removes the last topic item",
                )

        topicgroup.install_command(
                cmdname="undo",
                callback=self.topic_undo,
                helptext="Reverts the topic to the last known channel topic",
                )

        # Maps channel names to the last so many topics
        # (The top most item on the stack should be the current topic. But the
        # handlers should handle the case that the stack is empty!)
        self.topic_stack = defaultdict(lambda: deque(maxlen=10))
        self.listen_for_event("irc.on_topic_updated")
        # set of deferreds waiting for the current topic response in a channel
        self.topic_waiters = defaultdict(set)

    ### Topic methods
    def on_event_irc_on_topic_updated(self, event):
        channel = event.channel
        newtopic = event.newtopic
        oldtopic = None
        try:
            oldtopic = self.topic_stack[channel][-1]
        except IndexError:
            pass
        if newtopic != oldtopic:
            self.topic_stack[event.channel].append(newtopic)
            log.msg("Topic updated in %s. Now I know about %s past topics (including this one)" % (event.channel,
                len(self.topic_stack[event.channel])))

        for d in self.topic_waiters.pop(channel, set()):
            d.callback(newtopic)

    def _get_change_topic_event(self, channel, to):
        topicchange = Event("irc.do_topic",
                channel=channel,
                topic=to)
        return topicchange

    def _get_current_topic(self, channel):
        """Returns a deferred object with the current topic.
        The callback will be called with the channel topic once it's known. The
        errback will be called if the topic cannot be determined
        
        """
        topic_stack = self.topic_stack[channel]
        if topic_stack:
            return defer.succeed(topic_stack[-1])

        # We need to ask what the topic is. Go ahead and send off that event.
        log.msg("Sending a request for the current topic since I don't know it")
        topicrequest = Event("irc.do_topic",
                channel=channel)
        self.transport.send_event(topicrequest)

        # Now set up a deferred object that will be called when the topic comes in
        deferreds = self.topic_waiters[channel]
        new_d = defer.Deferred()

        if not deferreds:
            # No current deferreds in the set. Set up a failure callback
            def failure(_):
                log.msg("Topic request timed out. Calling errbacks")
                for d in self.topic_waiters.pop(channel, set()):
                    d.errback()
            c = reactor.callLater(10, failure)
            # Set a success callback to cancel the failure timeout
            def success(result):
                log.msg("Topic result came in")
                c.cancel()
                return result
            new_d.addCallback(success)

        deferreds.add(new_d)
        return new_d

    @require_channel
    def topicappend(self, event, match):
        channel = event.channel
        def callback(currenttopic):
            currenttopic += " | " + match.groupdict()['text']
            topicevent = self._get_change_topic_event(channel, currenttopic)
            self.transport.send_event(topicevent)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicinsert(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])
            text = gd['text']

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            topic_parts.insert(pos, text)

            newtopic = " | ".join(topic_parts)
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicreplace(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])
            text = gd['text']

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                topic_parts[pos] = text
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return


            newtopic = " | ".join(topic_parts)
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicremove(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                del topic_parts[pos]
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return

            newtopic = " | ".join(topic_parts)
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicpop(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                del topic_parts[-1]
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return

            newtopic = " | ".join(topic_parts)
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topic_undo(self, event, match):
        channel = event.channel

        topicstack = self.topic_stack[channel]
        if len(topicstack) < 2:
            event.reply("I don't know what the topic used to be. Cannot undo =(")
            return
        # Pop the current item off
        topicstack.pop()
        # Now pop the next item, which will be our new topic
        newtopic = topicstack.pop()

        topicevent = self._get_change_topic_event(channel, newtopic)

        self.transport.send_event(topicevent)


