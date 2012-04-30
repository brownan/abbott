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

    def shutdown(self, event, match):
        event.reply("Goodbye")
        reactor.callLater(2, reactor.stop)

class Help(CommandPluginSuperclass):
    def start(self):
        super(Help, self).start()

        self.install_command(
                cmdname="help",
                callback=self.display_help,
                helptext="Displays a list of all top-level commands",
                )

    @defer.inlineCallbacks
    def display_help(self, event, match):
        command_groups = []
        for plugin in self.pluginboss.loaded_plugins.itervalues():
            try:
                command_groups.extend(plugin.cmdgs)
            except AttributeError:
                pass

        commands = []
        for group in command_groups:
            if not group.grpname:
                # This is a group of top-level commands
                for cmd in group.subcmds:
                    if cmd[1] is None or \
                            (yield event.has_permission(cmd[1].replace("%c", event.channel))):
                        commands.append(cmd[0])
            else:
                # Go through the sub-commands and make sure there is at least
                # one command we have access to
                for cmd in group.subcmds:
                    if cmd[1] is None or \
                            (yield event.has_permission(cmd[1].replace("%c", event.channel))):
                        commands.append(group.grpname)
                        break

        event.reply("Commands you have access to: %s" % ", ".join(commands))
        event.reply("Use 'help <command>' for more information about a command")
