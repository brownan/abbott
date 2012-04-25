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
        mynick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname
        if mynick == event.channel:
            event.reply("Hey, you can't do that in here!")
        else:
            return func(self, event, match)
    return newfunc

class IRCAdmin(CommandPluginSuperclass):
    """A plugin to do various IRC OP-related tasks. The plugin does not keep
    op, but acquires it on demand and then drops it.

    """
    prefix="."

    def start(self):
        super(IRCAdmin, self).start()

        self.install_command(r"(kick|KICK) (?P<nick>[^ ]+)( (?P<reason>.*))?$",
                "irc.op.kick",
                self.kick)
        self.help_msg("kick|KICK",
                "irc.op.kick",
                "'.KICK <nick> [reason]' Kicks a user from the current channel")
        self.define_command(".KICK")

        self.install_command("(voice|VOICE) (?P<nick>[^ ]+)$",
                "irc.op.voice",
                self.voice)
        self.help_msg("voice|VOICE",
                "irc.op.voice",
                "'.VOICE <nick>' Grants a user voice in the current channel",
                )
        self.define_command(".VOICE")

        self.install_command("(devoice|DEVOICE) (?P<nick>[^ ]+)$",
                "irc.op.voice",
                self.devoice)
        self.help_msg("devoice|DEVOICE",
                "irc.op.voice",
                "'.DEVOICE <nick>' Revokes a user's voice in the current channel",
                )
        self.define_command(".DEVOICE")

        self.install_command(r"op with external command (?P<command>.*)$",
                "irc.op",
                self.set_op_external_command)
        self.install_command(r"op with chanserv$",
                "irc.op",
                self.set_op_with_chanserv)

        # Topic commands
        topic_permission = None
        self.install_command("topic append (?P<text>.+)$",
                topic_permission,
                self.topicappend)
        self.help_msg("topic append",
                topic_permission,
                "'topic append <text>' Appends text to the end of the channel topic")

        self.install_command(r"topic insert (?P<pos>[-\d]+) (?P<text>.+)$",
                topic_permission,
                self.topicinsert)
        self.help_msg("topic insert",
                topic_permission,
                "'topic insert <pos> <text>' Inserts text into the topic at the given position")

        self.install_command(r"topic remove (?P<pos>[-\d]+)$",
                topic_permission,
                self.topicremove)
        self.help_msg("topic remove",
                topic_permission,
                "'topic remove <pos>' Removes the pos'th topic section")

        self.install_command(r"topic undo$",
                topic_permission,
                self.topic_undo)
        self.help_msg("topic undo",
                topic_permission,
                "'topic undo' Reverts the topic to the last known channel topic")

        self.install_command("topic requireop",
                "irc.op",
                self.topic_requireop)
        self.help_msg("topic requireop",
                topic_permission,
                "'topic requireop' The bot will acquire OP to change the topic in this channel")

        self.install_command("topic norequireop",
                "irc.op",
                self.topic_norequireop)
        self.help_msg("topic norequireop",
                topic_permission,
                "'topic norequireop' The bot will not try to acquire OP to change the topic in this channel")

        self.help_msg("topic",
                topic_permission,
                "'topic <command> [args]' Topic commands: append, insert, remove, undo")

        self.define_command("topic")

        # Maps channel names to the last so many topics (not going to fall into
        # the trap of duplicating my constants in comments!)
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

        self.listen_for_event("irc.on_mode_change")

    def on_event_irc_on_mode_change(self, event):
        """A mode has changed. If we now have op, call all the callbacks in
        self.waiting_for_op

        """
        mynick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

        if (event.set == True and "o" in event.modes and event.args and
                event.args[0] == mynick):
            log.msg("I was given op. Calling op callbacks on %s" % event.chan)
            for deferred in self.waiting_for_op.pop(event.chan, set()):
                deferred.callback(None)

            # Now set -o on ourselves now that all callbacks have been called
            deop = Event("irc.do_mode", 
                    chan=event.chan,
                    set=False,
                    modes="o",
                    user=mynick,
                    )
            log.msg("Relinquishing op")
            self.transport.send_event(deop)

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
        channel = event.channel

        voiceevent = Event("irc.do_mode",
                chan=channel,
                set=False,
                modes="v",
                user=nick,
                )
        log.msg("De-voicing %s in %s" % (nick, channel))
        self._send_event_as_op(channel, voiceevent, event.reply)

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
            # Issue a kick request!
            log.msg("OP succeeded, proceeding to issue event %s" % event.__dict__)
            self.transport.send_event(event)
        deferred.addCallbacks(success, fail)

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

    def _get_op(self, channel):
        """Returns a deferred object that's called when OP is acquired.
        
        the error callback is called if op is not acquired in a timely manner
        
        """
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
    def topicremove(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            del topic_parts[pos]

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
