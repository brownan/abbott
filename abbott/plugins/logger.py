import pprint

from ..pluginbase import BotPlugin
from ..command import CommandPluginSuperclass

class Log(BotPlugin):
    def start(self):
        self.listen_for_event("*.*")

    def received_event(self, event):
        print()
        print("Received event %s" % (event.eventtype,))
        print(pprint.pformat(event.__dict__))

class Repr(CommandPluginSuperclass):
    def start(self):
        super(Repr, self).start()
        self.install_command(
                cmdname="repr",
                callback=(lambda event,match:
                        event.reply(repr(match.groupdict()['text']))
                        ),
                argmatch="(?P<text>.*)$",
                cmdusage="<text>",
                helptext="echos the repr of your text",
                )
