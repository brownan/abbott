from collections import defaultdict

from twisted.internet import reactor, defer

from ..command import CommandPluginSuperclass

class CoreControl(CommandPluginSuperclass):
    def start(self):
        super(CoreControl, self).start()

        self.install_command(
                cmdname="shutdown",
                cmdmatch="kill|die|stop|quit|shutdown|halt",
                permission="core.shutdown",
                callback=self.shutdown,
                helptext="Shuts down",
                )

        self.install_command(
                cmdname="configreload",
                permission="core.configreload",
                callback=self.configreload,
                helptext="Re-reads the config on disk and updates in-memory configuration",
                )

    def shutdown(self, event, match):
        event.reply("Goodbye")
        reactor.callLater(2, reactor.stop)

    def configreload(self, event, match):
        try:
            self.pluginboss.load()
        except Exception, e:
            event.reply("There was a problem loading the new json. Check for syntax errors maybe? Full traceback in log")
            raise
        for plugin in self.pluginboss.loaded_plugins.itervalues():
            plugin.reload()
        event.reply("Config reloaded!")
        
class Help(CommandPluginSuperclass):
    def start(self):
        super(Help, self).start()

        self.install_command(
                cmdname="help",
                # The help command is a bit different. Normally we wouldn't
                # include an end-of-string match in the re, but since help is
                # caught by every installed command by every plugin, we need to
                # make sure this matches help and only help.
                cmdmatch="help$",
                callback=self.display_help,
                helptext="Help on the help command. Displays a helpful help message about help, helps you help yourself use help. Helpful, huh?",
                )

    @defer.inlineCallbacks
    def display_help(self, event, match):
        command_groups = []
        for plugin in self.pluginboss.loaded_plugins.itervalues():
            try:
                command_groups.extend(plugin.cmdgs)
            except AttributeError:
                pass

        globalcommands = []
        channelcommands = defaultdict(list)

        for group in command_groups:
            if not group.grpname:
                # This is a group of top-level commands. List each command
                # individually
                for cmd in group.subcmds:
                    # Get a list of channels where this permission applies for
                    # this user
                    where = (yield event.where_permission(cmd[1]))
                    if None in where:
                        globalcommands.append(cmd[0])
                    else:
                        for chan in where:
                            channelcommands[chan].append(cmd[0])

            else:
                # Go through the sub-commands and make sure there is at least
                # one command we have access to. If so, just list the top-level
                # metacommand
                chans = set()
                for cmd in group.subcmds:
                    chans.update((yield event.where_permission(cmd[1])))

                if None in chans:
                    globalcommands.append(group.grpname)
                else:
                    for chan in chans:
                        channelcommands[chan].append(group.grpname)

        if globalcommands or channelcommands:
            if globalcommands:
                event.reply(notice=True, direct=True,
                        msg="Global commands you have access to: %s" % (
                            ", ".join(globalcommands)
                            ))

            for chan, cmds in channelcommands.iteritems():
                event.reply(notice=True, direct=True,
                        msg="In %s you can execute: %s" % (
                            chan, ", ".join(cmds)
                            ))

            event.reply(notice=True, direct=True,
                    msg="Use 'help <command>' for more information on a command")
        else:
            event.reply(notice=True, direct=True,
                    msg="You don't have access to any of my commands. Go away.")
