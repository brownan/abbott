from twisted.internet import reactor

from ..command import CommandPluginSuperclass

class CoreControl(CommandPluginSuperclass):
    def start(self):
        super(CoreControl, self).start()

        self.install_command(r"kill|die|stop|quit|shutdown|halt$",
                "core.shutdown",
                self.shutdown)

    def shutdown(self, event, match):
        event.reply("Goodbye")
        reactor.callLater(2, reactor.stop)
