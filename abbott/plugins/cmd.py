# encoding: UTF-8
from StringIO import StringIO

from twisted.internet import defer, reactor
from twisted.internet.utils import getProcessOutput
from twisted.python import log
from twisted.internet.protocol import ProcessProtocol

from ..pluginbase import BotPlugin
from ..command import CommandPluginSuperclass

class ContinuousProcess(ProcessProtocol):
    """Runs a command. When a line of output is received, send it to
    the given callback.

    """
    def __init__(self, plugin, line_recvd):
        self.plugin = plugin
        self.outReceived = line_recvd

    def processEnded(self, status):
        if self.plugin.currentprocess is self:
            self.plugin.currentprocess = None

class RunCommand(CommandPluginSuperclass):
    def start(self):
        super(RunCommand, self).start()

        self.currentprocess = None

        self.install_command(
                cmdname="shell",
                cmdusage="<shell command>",
                argmatch="(?P<cmd>.+)$",
                permission="cmd.shell",
                helptext="Runs a command in a shell and replies with the output. Caution: may be spammy",
                callback=self.start_process,
                )

        self.install_command(
                cmdname="sigkill",
                permission="cmd.shell",
                helptext="Kills the currently running process",
                callback=self.kill_process,
                )

    def stop(self):
        if self.currentprocess:
            self.currentprocess.transport.signalProcess("KILL")

    def start_process(self, event, match):
        if self.currentprocess:
            self.currentprocess.transport.signalProcess("KILL")

        self.currentprocess = ContinuousProcess(
                self,
                lambda s: event.reply(s, userprefix=False),
                )

        reactor.spawnProcess(
                self.currentprocess,
                "/bin/bash",
                ["/bin/bash", "-c",
                    match.groupdict()['cmd'],
                ]
                )

    def kill_process(self, event, match):
        if self.currentprocess:
            self.currentprocess.transport.signalProcess("KILL")
        event.reply("SIGKILL sent")
