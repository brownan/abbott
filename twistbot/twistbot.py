from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.ssl import ClientContextFactory

import json
import sys

if __name__ == "__main__":
    f = BotFactory()

    reactor.connectSSL("irc.freenode.net", 7000, f, ClientContextFactory())

    reactor.run()
