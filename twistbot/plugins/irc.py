from twisted.words.protocols import irc
from twisted.internet import reactor, protocol
from twisted.internet.ssl import ClientContextFactory

from ..pluginbase import BotPlugin


class IRCBot(irc.IRCClient):
    nickname = "brownan-bot2"

    # Maps event names to method names that should be called on this event
    events = {
            'irc_joinchannel': 'join',
            'irc_leavechannel': 'leave',
            'irc_kick': 'kick',
            'irc_invite': 'invite',
            'irc_topic': 'topic',
            'irc_mode': 'mode',
            'irc_say': 'say',
            'irc_msg': 'msg',
            'irc_notice': 'notice',
            'irc_away': 'away',
            'irc_back': 'back',
            'irc_whois': 'whois',
            'irc_setnick': 'setNick',
            'irc_quit': 'quit',
            }

    ### ALL METHODS BELOW ARE OVERRIDDEN METHODS OF irc.IRCClient (or ancestors)
    ### AND ARE CALLED AUTOMATICALLY UPON THE APPROPRIATE EVENTS

    def connectionMade(self):
        """This is called by Twisted once the connection has been made, and has
        access to self.factory. This is where we set up callbacks for actions
        we can perform

        """
        super(IRCBot, self).connectionMade()
        for eventname, methodname in self.events.iteritems():
            self.factory.listen_for_events(eventname, getattr(self, methodname))

    def connectionLost(self):
        """The connection is down and this object is about to be destroyed,
        unhook our event listeners
        
        """
        super(IRCBot, self).connectionLost()
        for eventname in self.events.iterkeys():
            self.factory.stop_listening(eventname)

    ### The following are things that happen to us

    def privmsg(self, user, channel, message):
        """Someone sent us a private message or we received a channel
        message
        
        """
        if channel == self.nickname:
            self.factory.broadcast_message("privmsg", user, message)
        else:
            self.factory.broadcast_message("channelmsg", user, channel, message)

    def notice(self, user, channel, message):
        """Received a notice"""
        if channel == self.nickname:
            self.factory.broadcast_message("noticemsg", user, message)
        else:
            self.factory.broadcast_message("channelnotice", user, channel, message)

    def joined(self, channel):
        """We have joined a channel"""
        self.factory.broadcast_message("joined", channel)

    def left(self, channel):
        """We have left a channel"""
        self.factory.broadcast_message("left", channel)

    ### Things we see other users doing or observe about the channel

    def modeChanged(self, user, channel, set, modes, args):
        """A mode has changed on a user or a channel.

        user is who instigated the change

        channel is the channel where the mode changed.

        set is true if the mode is being added, falst if it is being removed.

        modes is the mode or modes which are being changed

        args is a tuple with any additional info required for the mode
        """
        self.factory.broadcast_message("modechange", user, channel, set, modes, args)

    def userJoined(self, user, channel):
        self.factory.broadcast_message("userjoined", user, channel)

    def userLeft(self, user, channel):
        self.factory.broadcast_message("userleft", user, channel)

    def userQuit(self, user, quitmessage):
        self.factory.broadcast_message("userquit", user, quitmessage)

    def userKicked(self, kickee, channel, kicker, message):
        self.factory.broadcast_message("userkicked", kickee, channel, kicker, message)

    def action(self, user, channel, data):
        """User performs an action on the channel"""
        self.factory.broadcast_message("action", user, channel, data)

    def topicUpdated(self, user, channel, newtopic):
        self.factory.broadcast_message("topicupdated", user, channel, newtopic)

    def userRenamed(self, oldname, newname):
        self.factory.broadcast_message("nickchange", oldname, newname)



class IRCBotPlugin(protocol.ReconnectingClientFactory, BotPlugin):
    """Implements a bot plugin and a twisted protocol client factory.

    """
    protocol = IRCBot

    def start(self):
        reactor.connectSSL("irc.freenode.net", 7000, self, ClientContextFactory())

    def stop(self):
        self.stopTrying()
        # XXX Figure out how to remove this from the reactor
