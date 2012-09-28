from . import pluginbase
from . import transport

from twisted.internet import reactor
from twisted.python import log

import sys

def main():
    transportobj = transport.Transport()
    boss = pluginbase.PluginBoss(sys.argv[1], transportobj)

    observer = log.FileLogObserver(sys.stdout)
    observer.timeFormat = "%Y-%m-%d %H:%M:%S"
    log.startLoggingWithObserver(observer.emit)
    log.msg("Abbott starting up!")


    boss.load_all_plugins()

    reactor.run()

if __name__ == "__main__":
    main()
