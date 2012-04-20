import twistbot.pluginbase
import twistbot.transport

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.ssl import ClientContextFactory

import json
import sys

def main():
    transport = twistbot.transport.Transport()
    boss = twistbot.pluginbase.PluginBoss(sys.argv[1], transport)

    boss.load_all_plugins()

    reactor.run()

if __name__ == "__main__":
    main()
