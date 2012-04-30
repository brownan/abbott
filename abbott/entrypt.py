from . import pluginbase
from . import transport

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.ssl import ClientContextFactory
from twisted.python import log

import json
import sys

def main():
    observer = log.FileLogObserver(sys.stdout)
    observer.timeFormat = "%Y-%m-%d %H:%M:%S"
    log.startLoggingWithObserver(observer.emit)
    log.msg("Q-bot starting up!")

    transportobj = transport.Transport()
    boss = pluginbase.PluginBoss(sys.argv[1], transportobj)

    boss.load_all_plugins()

    reactor.run()

if __name__ == "__main__":
    main()
