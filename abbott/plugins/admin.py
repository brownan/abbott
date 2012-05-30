from collections import defaultdict, deque
import shlex
from functools import wraps

from twisted.internet import reactor
from twisted.python import log
from twisted.internet.utils import getProcessOutput
from twisted.internet import defer

from ..command import CommandPluginSuperclass
from ..transport import Event

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

class IRCAdmin(CommandPluginSuperclass):
    """A plugin to do various IRC OP-related tasks. The plugin automatically
    drops OP after it performs an OP task, but can be configured to keep OP.

    """

    def start(self):
        super(IRCAdmin, self).start()

        self.install_command(
                cmdname="kick",
                cmdmatch="kick|KICK",
                cmdusage="<nickname> [reason]",
                argmatch = "(?P<nick>[^ ]+)( (?P<reason>.*))?$",
                permission="irc.op.kick",
                prefix=".",
                callback=self.kick,
                helptext="Kicks a user from the current channel")

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

        ircadmin = self.install_cmdgroup(
                grpname="ircadmin",
                permission="irc.op",
                helptext="IRC adminstrative commands",
                )

        ircadmin.install_command(
                cmdname="op with external command",
                cmdmatch=None, # uses the cmdname literally
                cmdusage="<command>",
                argmatch="(?P<command>.*)$",
                prefix=None,
                callback=self.set_op_external_command,
                helptext=None, # will not appear in help text
                )
        ircadmin.install_command(
                cmdname="op with chanserv",
                cmdmatch=None,
                cmdusage=None, # no usage
                argmatch=None, # no arguments
                prefix=None,
                callback=self.set_op_with_chanserv,
                helptext=None,
                )

        ircadmin.install_command(
                cmdname="optimeout",
                argmatch=r"(?P<timeout>-?\d+)( (?P<channel>[^ ]+))?$",
                callback=self.set_op_timeout,
                cmdusage="<timeout> [channel]",
                helptext="Sets how long I'll keep OP before I give it up. -1 for forever",
                )

                

        # Topic commands
        topicgroup = self.install_cmdgroup(
                grpname="topic",
                prefix=None,
                permission="irc.op.topic",
                helptext="Topic manipulation commands",
                )

        topicgroup.install_command(
                cmdname="append",
                cmdmatch=None,
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
                cmdmatch=None,
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
                cmdname="undo",
                callback=self.topic_undo,
                helptext="Reverts the topic to the last known channel topic",
                )

        topicgroup.install_command(
                cmdname="requireop",
                callback=self.topic_requireop,
                helptext="The bot will acquire OP to change the topic in this channel",
                permission="irc.op",
                )
        topicgroup.install_command(
                cmdname="norequireop",
                callback=self.topic_norequireop,
                helptext="The bot will not try to acquire OP to change the topic in this channel",
                permission="irc.op",
                )

        # Maps channel names to the last so many topics
        # (The top most item on the stack should be the current topic. But the
        # handlers should handle the case that the stack is empty!)
        self.topic_stack = defaultdict(lambda: deque(maxlen=10))
        self.listen_for_event("irc.on_topic_updated")
        if "requiresop" not in self.config:
            self.config['requiresop'] = []
            self.pluginboss.save()
        # set of deferreds waiting for the current topic response in a channel
        self.topic_waiters = defaultdict(set)

        # Maps channel names to a set of deferred objects indicating someone is
        # waiting for op on a channel
        self.waiting_for_op = defaultdict(set)

        if "opmethod" not in self.config:
            self.config['opmethod'] = None
            self.pluginboss.save()

        # If we currently have OP, there may be a reactor timeout to relinquish
        # it. This instance var maps channels to the IDelayedCall object for
        # the timeout, which should be reset if we perform any other OP calls
        self.op_timeout_event = {}

        # This var holds whether we currently have OP or not in each channel
        self.have_op = defaultdict(bool)

        if "optimeout" not in self.config or \
                not isinstance(self.config['optimeout'], dict):
            self.config['optimeout'] = {}
            self.pluginboss.save()

        self.listen_for_event("irc.on_mode_change")
        self.listen_for_event("irc.on_join")

    def on_event_irc_on_mode_change(self, event):
        """A mode has changed. If we now have op, call all the callbacks in
        self.waiting_for_op

        """
        mynick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

        if (event.set == True and "o" in event.modes and event.args and
                event.args[0] == mynick):
            # Op acquired. Make a note of it
            self.have_op[event.chan] = True

            # Now that we have op, call deferreds on anything that was waiting
            log.msg("I was given op. Calling op callbacks on %s" % event.chan)
            for deferred in self.waiting_for_op.pop(event.chan, set()):
                deferred.callback(None)

            # Now that we have op, set a timeout to relinquish it
            timeout = self._get_op_timeout(event.chan)
            if timeout >= 0:
                delayedcall = reactor.callLater(
                        timeout,
                        self._relinquish_op,
                        event.chan)
                self.op_timeout_event[event.chan] = delayedcall

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

    def _get_op_timeout(self, chan):
        """Returns the op timeout for the given channel. If one isn't defined
        in the config, write out the default and save it.

        """
        try:
            return self.config['optimeout'][chan]
        except KeyError:
            self.config['optimeout'][chan] = 30
            self.pluginboss.save()
            return 30

    def _relinquish_op(self, chan):
        """This is called by a reactor.callLater() call to relinquish op on the
        given channel

        """
        self.have_op[chan] = False
        del self.op_timeout_event[chan]

        mynick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname
        deop = Event("irc.do_mode", 
                chan=chan,
                set=False,
                modes="o",
                user=mynick,
                )
        log.msg("Relinquishing op")
        self.transport.send_event(deop)

    def _send_event_as_op(self, channel, event, reply):
        """Gets op on the specified channel, then sends the given event
        
        The given reply callable is used if we need to reply to the channel (on
        failure)
        
        """
        deferred = self._get_op(channel)
        def fail(reason):
            log.err("failed because we failed to get OP")
            reply(reason.getErrorMessage())
        def success(_):
            log.msg("OP succeeded, proceeding to issue event %s" % event.__dict__)
            self.transport.send_event(event)
        deferred.addCallbacks(success, fail)

    def _get_op(self, channel):
        """Returns a deferred object that's called when OP is acquired.
        
        the error callback is called if op is not acquired in a timely manner
        
        """
        if self.have_op[channel]:
            # We already have op. Reset the timer if it exists, and return
            # succeess
            timeout = self._get_op_timeout(channel)
            if timeout >= 0:
                try:
                    self.op_timeout_event[channel].reset(timeout)
                except KeyError:
                    pass
            return defer.succeed(None)

        hasop = defer.Deferred()

        if self.waiting_for_op[channel]:
            # Already had some waiters, that must mean op is already pending
            log.msg("Was going to submit an OP requst, but there's already one pending")
            self.waiting_for_op[channel].add(hasop)
            return hasop

        if self.config['opmethod'] == "external":
            mynick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

            cmd = self.config['opcommand']
            # Break the command up into parts. This is done before the %c and
            # %n replacement to avoid any injection attacks
            cmd_parts = shlex.split(cmd.encode("UTF-8"))
            # Now do the replacements
            cmd_parts = [x.replace("%c", channel.encode("UTF-8")) for x in cmd_parts]
            cmd_parts = [x.replace("%n", mynick.encode("UTF-8"))  for x in cmd_parts]
            log.msg("Executing %s" % cmd)
            log.msg("Final arguments are %s" % cmd_parts)
            proc_defer = getProcessOutput("/usr/bin/env", cmd_parts, errortoo=True)
            def finished(output):
                if output.strip():
                    log.msg("Process finished. Returned %s" % output)
            proc_defer.addCallback(finished)

        elif self.config['opmethod'] == "chanserv":
            return defer.fail("chanserv mode not supported just yet")
        else:
            return defer.fail("No suitable methods to acquire OP are defined!")

        self.waiting_for_op[channel].add(hasop)
        reactor.callLater(10, self._timed_out, channel)

        return hasop

    def _timed_out(self, channel):
        """The request to gain OP in the given channel has timed out. Call the
        error methods on any deferreds waiting for it

        """
        deferreds = self.waiting_for_op.pop(channel, set())
        for deferred in deferreds:
            deferred.errback("OP request timed out. Check your config and the error log.")

    ###
    ### Command callbacks for OP related tasks
    ###

    def set_op_timeout(self, event, match):
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
        if timeout < 0:
            event.reply("Done. I'll never give up my op")
            if timeoutevent:
                timeoutevent.cancel()
                del self.op_timeout_event[channel]
        else:
            event.reply("Done. I'll hold op for %s seconds after I get it" % timeout)
            if timeoutevent:
                timeoutevent.reset(timeout)
            elif self.hasop[channel]:
                # Set a new timeout event
                timeout = self._get_op_timeout(channel)
                if timeout >= 0:
                    delayedcall = reactor.callLater(
                            timeout,
                            self._relinquish_op,
                            channel)
                    self.op_timeout_event[channel] = delayedcall


    @require_channel
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

        self._send_event_as_op(channel, kickevent, event.reply)

    @require_channel
    def voice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        voiceevent = Event("irc.do_mode",
                chan=channel,
                set=True,
                modes="v",
                user=nick,
                )
        log.msg("Voicing %s in %s" % (nick, channel))
        self._send_event_as_op(channel, voiceevent, event.reply)
    @require_channel
    def devoice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        voiceevent = Event("irc.do_mode",
                chan=channel,
                set=False,
                modes="v",
                user=nick,
                )
        log.msg("De-voicing %s in %s" % (nick, channel))
        self._send_event_as_op(channel, voiceevent, event.reply)

    @require_channel
    def give_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        opevent = Event("irc.do_mode",
                chan=channel,
                set=True,
                modes="o",
                user=nick,
                )
        log.msg("Opping %s in %s" % (nick, channel))
        self._send_event_as_op(channel, opevent, event.reply)
    @require_channel
    def take_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        opevent = Event("irc.do_mode",
                chan=channel,
                set=False,
                modes="o",
                user=nick,
                )
        log.msg("De-Opping %s in %s" % (nick, channel))
        self._send_event_as_op(channel, opevent, event.reply)

    @require_channel
    def quiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        channel = event.channel

        newevent = Event("irc.do_mode",
                chan=channel,
                set=True,
                modes="q",
                user=nick,
                )
        log.msg("quieting %s in %s" % (nick, channel))
        self._send_event_as_op(channel, newevent, event.reply)

    @require_channel
    def unquiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        channel = event.channel

        newevent = Event("irc.do_mode",
                chan=channel,
                set=False,
                modes="q",
                user=nick,
                )
        log.msg("unquieting %s in %s" % (nick, channel))
        self._send_event_as_op(channel, newevent, event.reply)

    def set_op_external_command(self, event, match):
        """Configure the plugin to use an external command to acquire op.
        
        The command will be executed by the shell. The string %c will be
        replaced by the channel, and %n is the nick to op
        
        """
        self.config['opmethod'] = 'external'
        self.config['opcommand'] = match.groupdict()['command']
        self.pluginboss.save()
        event.reply("OP method changed. Op command saved")
        
    def set_op_with_chanserv(self, event, match):
        """Configure the plugin to use chanserv to acquire op"""
        self.config['opmethod'] = 'chanserv'
        self.pluginboss.save()
        event.reply("Op method changed")


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
            if channel in self.config['requiresop']:
                self._send_event_as_op(chanel,
                        topicevent,
                        event.reply)
            else:
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

            if channel in self.config['requiresop']:
                self._send_event_as_op(chanel,
                        topicevent,
                        event.reply)
            else:
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

            if channel in self.config['requiresop']:
                self._send_event_as_op(chanel,
                        topicevent,
                        event.reply)
            else:
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

            if channel in self.config['requiresop']:
                self._send_event_as_op(chanel,
                        topicevent,
                        event.reply)
            else:
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

        if channel in self.config['requiresop']:
            self._send_event_as_op(chanel,
                    topicevent,
                    event.reply)
        else:
            self.transport.send_event(topicevent)


    @require_channel
    def topic_requireop(self, event, match):
        channel = event.channel
        requires_op = self.config["requiresop"]
        if channel not in requires_op:
            requires_op.append(channel)

        self.config['requiresop'] = requires_op
        self.pluginboss.save()
        event.reply("I will now try to acquire OP before changing the topic in this channel")



    @require_channel
    def topic_norequireop(self, event, match):
        channel = event.channel
        requires_op = self.config["requiresop"]
        if channel in requires_op:
            requires_op.remove(channel)

        self.config['requiresop'] = requires_op
        self.pluginboss.save()
        event.reply("I will no longer try to acquire OP before changing the topic in this channel")
