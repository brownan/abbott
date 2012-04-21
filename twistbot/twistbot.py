from . import pluginbase
from . import transport

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.ssl import ClientContextFactory

import json
import sys

def main():
    transportobj = transport.Transport()
    boss = pluginbase.PluginBoss(sys.argv[1], transportobj)

    boss.load_all_plugins()

    reactor.run()

if __name__ == "__main__":
    main()
