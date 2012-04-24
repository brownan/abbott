from twisted.internet import reactor

from ..command import CommandPluginSuperclass

class CoreControl(CommandPluginSuperclass):
    def start(self):
        super(CoreControl, self).start()

        self.install_command(r"kill|die|stop|quit|shutdown|halt$",
                "core.shutdown",
                self.shutdown)
        self.define_command("shutdown")

    def shutdown(self, event, match):
        event.reply("Goodbye")
        reactor.callLater(2, reactor.stop)

class Help(CommandPluginSuperclass):
    def start(self):
        super(Help, self).start()

        self.install_command("help$",
                None,
                self.display_help)

        self.define_command("help")

    def display_help(self, event, match):
        commands = []
        for plugin in self.pluginboss.loaded_plugins.itervalues():
            try:
                commands.extend(plugin.commandlist)
            except AttributeError:
                pass

        event.reply("Defined commands: %s" % ", ".join(commands))
