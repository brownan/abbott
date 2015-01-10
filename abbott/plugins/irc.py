from time import time
import unicodedata

from twisted.words.protocols import irc
from twisted.internet import reactor, protocol, defer
from twisted.internet.ssl import ClientContextFactory
from twisted.python import log

from ..pluginbase import BotPlugin
from ..transport import Event
from ..command import CommandPluginSuperclass

"""
This module houses the bot plugins IRCBotPlugin and IRCController, and one
twisted Protocol-derived object (IRCBot) used in conjunction with IRCBotPlugin
(which is also a twisted ClientFactory)

"""

class IRCBot(irc.IRCClient):
    """This is the IRC protocol object (not a bot plugin). One of these objects
    is created per connection to an IRC server by the Factory object
    (IRCBotPlugin) below, in its buildProtocol() method.

    See twisted.words.protocols.irc for more information.

    """

    ### ALL METHODS BELOW ARE OVERRIDDEN METHODS OF irc.IRCClient (or ancestors)
    ### AND ARE CALLED AUTOMATICALLY UPON THE RESPECTIVE EVENTS FROM THE IRC
    ### SERVER

    def lineReceived(self, line):
        """Overrides IRCClient.lineReceived to decode incoming strings to unicode"""
        try:
            line = line.decode("UTF-8")
        except UnicodeDecodeError:
            line = line.decode("CP1252", 'replace')
        return irc.IRCClient.lineReceived(self, line)

    def sendLine(self, line):
        """Overrides IRCClient.sendLine to encode outgoing lines with UTF-8.
        Also implements some rate-limiting logic.

        This method is also exported to other plugins as the irc.do_raw event.
        
        """
        if isinstance(line, bytes):
            line = line.decode("ASCII")

        # Do some filtering. Make sure no characters are control characters,
        # except perhaps for some color control characters
        whitelist = frozenset("\x02\x03\x0f\x12\x1f")
        line = "".join(x for x in line if
                unicodedata.category(x) != "Cc" or
                x in whitelist
                )

        line = line.encode("UTF-8")

        # Implement some simple rate limiting logic: If a line is received
        # within 2 seconds of the last line, increment the line_count var. If
        # line_count is over 5, set lineRate to 2 seconds.
        time_since_last_line = time() - self.time_of_last_line
        self.time_of_last_line = time()

        if time_since_last_line < 2:
            self.line_count += 1
        else:
            self.line_count = 0

        if self.line_count >= 5:
            # Queuing of lines is done by the IRCClient class, we just have to
            # set this var
            self.lineRate = 2
        elif not self._queue:
            # Only reset here if the queue has been emptied. It's possible the
            # queue is still emptying, in which case we want to keep the
            # limited rate up until the queue is emptied.
            self.lineRate = None

        return irc.IRCClient.sendLine(self, line)

    def connectionMade(self):
        """This is called by Twisted once the connection has been made, and has
        access to self.factory. Join the configured channels and do other
        initialization.

        """

        # Connection is successful
        # Reset the delay from the reconnecting factory
        try:
            self.factory.resetDelay()
        except AttributeError:
            # but don't error if it's not a reconnecting factory
            pass

        # These vars are used in rate limiting logic in sendLine()
        self.time_of_last_line = 0
        self.line_count = 0

        # Can't use super() because twisted doesn't use new-style classes
        irc.IRCClient.connectionMade(self)
        self.factory.client = self

        log.msg("Connection made")

        # Join the configured channels
        for channel in self.factory.config['channels']:
            self.join(channel)

    def connectionLost(self, reason):
        """The connection is down and this object is about to be destroyed,
        so do any cleanup here.
        
        """
        self.factory.client = None
        irc.IRCClient.connectionLost(self, reason)

        log.msg("IRC Connection lost!")

    ### The following are things that happen to us

    def joined(self, channel):
        """We have joined a channel"""
        log.msg("Joined channel %s" % channel)
        self.factory.broadcast_message("irc.on_join", channel=channel)

        if channel not in self.factory.config['channels']:
            self.factory.config['channels'].append(channel)
            self.factory.config.save()

    def left(self, channel):
        """We have left a channel"""
        self.factory.broadcast_message("irc.on_part", channel=channel)

        if channel in self.factory.config['channels']:
            self.factory.config['channels'].remove(channel)
            self.factory.config.save()

    ### Things we see other users doing or observe about the channel

    def privmsg(self, user, channel, message):
        """Someone sent us a private message or we received a channel
        message.

        This event has an extra attribute added: direct
        it is equal to event.channel == self.nickname
        
        """

        self.factory.broadcast_message("irc.on_privmsg",
                user=user, channel=channel, message=message,
                direct=channel == self.nickname)

    def noticed(self, user, channel, message):
        """Received a notice. This is like a privmsg, but distinct."""
        self.factory.broadcast_message("irc.on_notice",
                user=user, channel=channel, message=message)

    def modeChanged(self, user, channel, set, modes, args):
        """A mode has changed on a user or a channel.

        user is who instigated the change

        channel is the channel where the mode changed.

        set is true if the mode is being added, false if it is being removed.

        modes is the mode or modes which are being changed

        args is a tuple with any additional info required for the mode
        """
        # The event broadcast out is slightly different. Events will always
        # contain exactly one mode change, while we may get more than one mode
        # change from the irc server in a single call to this method. arg may
        # be None for modes that don't set an arg.
        for mode, arg in zip(modes, args):
            self.factory.broadcast_message("irc.on_mode_change",
                    user=user, channel=channel, set=set, mode=mode, arg=arg)

    def userJoined(self, user, channel):
        self.factory.broadcast_message("irc.on_user_joined",
                user=user, channel=channel)

    def userLeft(self, user, channel):
        self.factory.broadcast_message("irc.on_user_part",
                user=user, channel=channel)

    def userQuit(self, user, message):
        self.factory.broadcast_message("irc.on_user_quit",
                user=user, message=message)

    def userKicked(self, kickee, channel, kicker, message):
        self.factory.broadcast_message("irc.on_user_kick",
                kickee=kickee, channel=channel, kicker=kicker, message=message)

    def action(self, user, channel, data):
        """User performs an action on the channel"""
        self.factory.broadcast_message("irc.on_action",
                user=user, channel=channel, data=data)

    def topicUpdated(self, user, channel, newtopic):
        self.factory.broadcast_message("irc.on_topic_updated",
                user=user, channel=channel, newtopic=newtopic)

    def userRenamed(self, oldnick, newnick):
        self.factory.broadcast_message("irc.on_nick_change",
                oldnick=oldnick, newnick=newnick)

    def irc_unknown(self, prefix, command, params):
        """This hooks into all sorts of miscellaneous things the server sends
        us, including whois replies

        """
        self.factory.broadcast_message("irc.on_unknown",
                prefix=prefix, command=command, params=params)

    def mode(self, channel, set, modes, limit=None, user=None, mask=None):
        """This overridden method exists solely to make the parameter 'channel'
        uniform with the rest of this code. twisted uses the parameter name
        'chan'

        """
        irc.IRCClient.mode(self, channel, set, modes, limit, user, mask)

    def kick(self, channel, user, reason=None):
        """Overrides the twisted kick method. If we support REMOVE according to
        the config, use that.

        """
        if self.factory.config.get("remove", False):
            if channel[0] not in irc.CHANNEL_PREFIXES:
                channel = '#' + channel
            if reason:
                self.sendLine("REMOVE %s %s :%s" % (channel, user, reason))
            else:
                self.sendLine("REMOVE %s %s" % (channel, user))
        else:
            irc.IRCClient.kick(self, channel, user, reason)


class IRCBotPlugin(protocol.ReconnectingClientFactory, BotPlugin):
    """Implements a bot plugin and a twisted protocol client factory.

    """
    maxDelay = 60*5

    def start(self):
        self.client = None
        self.listen_for_event("irc.do_*")
        self.connector = reactor.connectSSL(self.config['server'], self.config['port'], self, ClientContextFactory())

        # Set a quit handler
        def shutdown():
            log.msg("reactor shutdown event triggered, stopping irc bot")
            self.shutdown_trigger = None
            self.stop()
            # Delay the shutdown by one second to give the event a chance to
            # get through.
            d = defer.Deferred()
            reactor.callLater(1, d.callback, None)
            return d
        self.shutdown_trigger = reactor.addSystemEventTrigger("before", "shutdown", shutdown)

        self.provides_request("irc.getnick")
        self.provides_request("irc.get_channel_mode_params")

    def stop(self):
        log.msg("IRCBotPlugin stopping...")
        if self.shutdown_trigger is not None:
            reactor.removeSystemEventTrigger(self.shutdown_trigger)
        self.stopTrying()
        if self.client:
            log.msg("Sending quit message")
            self.client.quit("Daisy, daisy...")

        # The server should disconnect us after a QUIT command, but just in
        # case, terminate the connection after 5 seconds.
        reactor.callLater(5, self.connector.disconnect)

    def buildProtocol(self, addr):
        """When a connection to the server is established, Twisted will call
        this method to create a Protocol object. Protocol objects handle sending
        and receiving data on the connection.
        """
        p = IRCBot()
        p.factory = self
        p.nickname = self.config['nick']
        p.password = self.config.get("password", None)
        p.realname = self.config.get("realname", "Abbott")
        return p

    def broadcast_message(self, eventname, **kwargs):
        """This method is called by the client protocol object when an event
        comes in from the network
        
        """
        event = Event(eventname, **kwargs)
        self.transport.send_event(event)

    def received_event(self, event):
        """A command received from another plugin. We must pass it on to the client

        """
        if not self.client:
            # TODO buffer the requests if the client is not currently connected
            return

        # Maps event names to (method names, arguments) that should be called
        # on the client protocol object.
        # In other words, when an event by the given name comes in, the given
        # method on the protocol object is called with as many of the named
        # keyword arguments as it can find from the event object.
        events = {
            'irc.do_join_channel':  ('join',    ('channel',)),
            'irc.do_leave_channel':  ('leave',   ('channel',)),
            'irc.do_kick':          ('kick',    ('channel', 'user', 'reason')),
            'irc.do_invite':        ('invite',  ('user', 'channel')),
            'irc.do_topic':         ('topic',   ('channel', 'topic')),
            'irc.do_mode':          ('mode',    ('channel','set','modes','limit','user','mask')),
            'irc.do_say':           ('say',     ('channel', 'message', 'length')),
            # This is just privmsg. It can send to channels or users
            'irc.do_msg':           ('msg',     ('user', 'message', 'legnth')),
            'irc.do_notice':        ('notice',  ('user', 'message')),
            'irc.do_away':          ('away',    ('away', 'message')),
            'irc.do_back':          ('back',    ()),
            'irc.do_whois':         ('whois',   ('nickname', 'server')),
            'irc.do_setnick':       ('setNick', ('nickname',)),
            'irc.do_quit':          ('quit',    ('message',)),
            'irc.do_raw':           ('sendLine',('line',)),
            }

        methodname, methodargs = events[event.eventtype]

        kwargs = {}
        for argname in methodargs:
            try:
                arg = getattr(event, argname)
                kwargs[argname] = arg
            except AttributeError:
                pass

        method = getattr(self.client, methodname)
        method(**kwargs)

    def on_request_irc_getnick(self):
        return defer.succeed(self.client.nickname)

    def on_request_irc_get_channel_mode_params(self):
        return self.client.getChannelModeParams()


class IRCController(CommandPluginSuperclass):
    """This plugin provides a few administrative tasks in conjunction with the
    IRCBotPlugin.

    """

    def start(self):
        super(IRCController, self).start()
        
        self.install_command(
                cmdname="join",
                argmatch=r"(?P<channel>#+[\w-]+)$",
                permission="irc.control",
                callback=self.join,
                cmdusage="<channel>",
                helptext="Joins an IRC channel"
                )

        self.install_command(
                cmdname="part",
                cmdmatch="part|leave",
                argmatch="(?P<channel>#+[\w-]+)?$",
                permission="irc.control",
                callback=self.part,
                cmdusage="[channel]",
                helptext="Leaves the current or specified IRC channel",
                )

        self.install_command(
                cmdname="nick",
                argmatch=r"(?P<newnick>[\w-]+)$",
                permission="irc.control",
                callback=self.nickchange,
                cmdusage="<new nick>",
                helptext="Changes my nickname",
                )

        self.install_command(
                cmdname="quote",
                argmatch=r"(?P<line>.+)$",
                permission="irc.quote",
                cmdusage="<line>",
                helptext="Sends a raw line to the IRC server",
                callback=lambda event,match:
                        self.transport.send_event(Event("irc.do_raw",
                            line=match.groupdict()['line'].strip())),
                )

    def join(self, event, match):
        channel = match.groupdict()['channel']
        
        newevent = Event("irc.do_join_channel", channel=channel)
        self.transport.send_event(newevent)

        event.reply("See you in %s!" % channel)

    def part(self, event, match):
        channel = match.groupdict().get("channel", None)

        if channel:
            newevent = Event("irc.do_leave_channel", channel=channel)
            self.transport.send_event(newevent)
            event.reply("Leaving %s" % channel)
        else:

            channel = event.channel
            if event.direct:
                # This was sent via a direct PM... I can't leave that!
                event.reply("You must let me know what channel to leave")
                return

            newevent = Event("irc.do_leave_channel", channel=channel)
            event.reply("Goodbye %s!" % channel)
            self.transport.send_event(newevent)

    def nickchange(self, event, match):
        newnick = match.groupdict()['newnick']

        if event.direct:
            event.reply("Changing nick to %s" % newnick)

        newevent = Event("irc.do_setnick", nickname=newnick)
        self.transport.send_event(newevent)

        # Also change the configuration
        botplugin_config = self.pluginboss.get_plugin_config("irc.IRCBotPlugin")
        botplugin_config['nick'] = newnick
        botplugin_config.save()
        self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].reload()

