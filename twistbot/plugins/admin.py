from collections import defaultdict
import shlex

from twisted.internet import reactor
from twisted.python import log
from twisted.internet.utils import getProcessOutput
from twisted.internet import defer

from ..command import CommandPluginSuperclass
from ..transport import Event

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

        self.install_command(r"op with external command (?P<command>.*)$",
                "irc.op.configure",
                self.set_op_external_command)
        self.install_command(r"op with chanserv$",
                "irc.op.configure",
                self.set_op_with_chanserv)

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

    def kick(self, event, match):
        """A user has issued the kick command. Our job here is to acquire OP
        for this channel and set a callback to issue a kick event

        """
        groupdict = match.groupdict()
        nick = groupdict['nick']
        reason = groupdict.get("reason", None)
        channel = event.channel

        deferred = self._get_op(event.channel)
        def fail(reason):
            log.msg("Kick failed because we failed to get OP")
            event.reply(reason.getErrorMessage())
        def success(_):
            # Issue a kick request!
            log.msg("OP succeeded, proceeding to issue a kick")
            kickevent = Event("irc.do_kick", channel=channel,
                    user=nick, reason=reason)
            self.transport.send_event(kickevent)
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
            cmd = cmd.replace("%c", channel)
            cmd = cmd.replace("%n", mynick)
            cmd_parts = shlex.split(cmd)
            log.msg("Executing %s" % cmd_parts)
            proc_defer = getProcessOutput(cmd_parts[0], cmd_parts[1:], errortoo=True)
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
            deferred.errback("OP request timed out. Check your config.")
