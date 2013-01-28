import re
from collections import defaultdict

from twisted.internet import reactor
from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass
from ..transport import Event
from ..pluginbase import BotPlugin, EventWatcher, non_reentrant

"""

This module provides various miscelaneous IRC functionality that is useful in
certain situations

"""

class WhoisError(Exception):
    pass
class WhoisTimedout(WhoisError):
    pass
class NoSuchNick(WhoisError):
    pass

class IRCWhois(CommandPluginSuperclass):
    """Provides a request:

    irc.whois

    takes one argument: the nickname
    deferred fires with a dictionary of information returned from the server.

    deferreds returned may also errback with one of the following exceptions:
    WhoisTimedout
    NoSuchNick

    """

    def start(self):
        super(IRCWhois, self).start()

        self.provides_request("irc.whois")

        self.listen_for_event("irc.on_unknown")

        self.install_command(
                cmdname="whois",
                argmatch=r"(?P<nick>[^ ]+)",
                callback=self.do_whois,
                cmdusage="<nick>",
                helptext="Does a whois and prints the results. This command is meant for debugging.",
                permission="irc.whois",
                )

        # nick of the current whois that is coming in on the wire right this
        # moment
        self.currentwhois = None
        # Maps the command to the parameters. command is a string and is either
        # a symbolic representation like RPL_WHOISUSER or for unknown commands
        # a string number like "330"
        self.currentinfo = {}

        # Map of nicks to deferred callbacks
        self.pendingwhoises = defaultdict(set)

    def on_event_irc_on_unknown(self, event):
        """Here's how this works. When we get a RPL_WHOISUSER, we assume all
        the unknown category of lines from the server are about that user until
        we get an RPL_ENDOFWHOIS

        """
        command = event.command
        params = event.params
        if command == "RPL_WHOISUSER":
            # Start a new one
            if self.currentwhois:
                log.msg("Error: Got a RPL_WHOISUSER but we're already in a whois!")
            nick = params[1]
            self.currentwhois = nick
            self.currentinfo = {command: params[1:]}

        elif command == "RPL_ENDOFWHOIS":
            if not self.currentwhois or not self.pendingwhoises[self.currentwhois]:
                return

            for callback in self.pendingwhoises.pop(self.currentwhois):
                callback.callback(dict(self.currentinfo))

            self.currentwhois = None

        elif command == "ERR_NOSUCHNICK":
            nick = params[1]
            for callback in self.pendingwhoises.pop(nick):
                callback.errback(NoSuchNick(params[2]))

        else:
            self.currentinfo[command] = params[1:]

    def on_request_irc_whois(self, nick):
        d = defer.Deferred()
        self.pendingwhoises[nick].add(d)

        event = Event("irc.do_whois",
                nickname=nick,
                )
        self.transport.send_event(event)

        def timeout():
            d.errback(WhoisTimedout("No whois response from server"))
            self.pendingwhoises[nick].remove(d)
        timer = reactor.callLater(10, timeout)
        def canceltimer(info):
            timer.cancel()
            return info
        d.addBoth(canceltimer)

        return d

    @defer.inlineCallbacks
    def do_whois(self, event, match):
        """A request from a !whois command"""
        nick = match.groupdict()['nick']
        try:
            info = (yield self.transport.issue_request("irc.whois", nick))
        except WhoisTimedout:
            event.reply("No response from the server. huh.")
            return
        except NoSuchNick:
            event.reply("Server said: no such nick")
            return
        if "330" in info:
            event.reply("{nick}!{username}@{host} {2} {1}".format(*info["330"],
                nick=info["RPL_WHOISUSER"][0],
                username=info["RPL_WHOISUSER"][1],
                host=info["RPL_WHOISUSER"][2]
                ))
        else:
            event.reply("{nick}!{username}@{host} is not logged in".format(
                nick=info["RPL_WHOISUSER"][0],
                username=info["RPL_WHOISUSER"][1],
                host=info["RPL_WHOISUSER"][2]
                ))
        #event.reply("Whois info for %s:" % nick)
        #for command, params in info.iteritems():
        #    event.reply("%s: %s" % (command, params))

class Names(CommandPluginSuperclass):
    """Provides a NAMES request for other plugins and a !names command"""
    def start(self):
        super(Names, self).start()

        self.provides_request("irc.names")

        self.listen_for_event("irc.on_unknown")

        #self.install_command(
        #        cmdname="names",
        #        argmatch=r"(?P<channel>\S+)?$",
        #        cmdusage="[channel]",
        #        callback=self.do_names,
        #        helptext="Does an IRC NAMES command and replies with the result",
        #        permission="irc.names",
        #        )

        self.currentinfo = []
        self.pending = defaultdict(set)

    @defer.inlineCallbacks
    def on_request_irc_names(self, channel):
        self.transport.send_event(Event("irc.do_raw",
                line="NAMES " + channel))
        log.msg("NAMES line sent for channel %s. Awaiting reply..." % channel)

        d = defer.Deferred()
        self.pending[channel].add(d)

        names = (yield d)

        defer.returnValue(names)

    def on_event_irc_on_unknown(self, event):
        command = event.command

        if command == "RPL_NAMREPLY":
            channel = event.params[2]
            names = event.params[3]
            self.currentinfo.append(names)

        elif command == "RPL_ENDOFNAMES":
            channel = event.params[1]
            names = " ".join(self.currentinfo)
            name_list = names.split()
            self.currentinfo = []
            if channel in self.pending:
                for d in self.pending.pop(channel):
                    d.callback(name_list)


    @defer.inlineCallbacks
    def do_names(self, event, match):
        gd = match.groupdict()
        channel = gd['channel']

        if not channel:
            if event.direct:
                event.reply("on what channel?")
                return
            channel = event.channel

        info = (yield self.transport.issue_request("irc.names", channel))

        event.reply("NAMES info for {0}: {1}".format(channel, info))

        
class ReplyInserter(CommandPluginSuperclass):
    """This plugin's function is to insert a reply() function to each incoming
    irc.on_privmsg event. It is required for a lot of functionality, including
    all Command-derived plugins, so you should probably have this activated!

    """
    def start(self):
        super(ReplyInserter, self).start()

        self.install_middleware("irc.on_privmsg")

        self.install_command(
                cmdname="echo",
                callback=lambda event,match: event.reply(match.groupdict()['msg']),
                cmdusage="<text to echo>",
                argmatch="(?P<msg>.+)$",
                helptext="Echos text back to where it came",
                permission="irc.echo"
                )

        self.install_command(
                cmdname="echoto",
                callback=lambda event,match: self.transport.send_event(
                    Event("irc.do_msg",
                        user=match.groupdict()['channel'],
                        message=match.groupdict()['msg'],
                    )),
                permission="irc.echoto",
                cmdusage="<channel> <text to echo>",
                argmatch="(?P<channel>[^ ]+) (?P<msg>.+)$",
                helptext="Echos text to the given channel",
                )


    def on_middleware_irc_on_privmsg(self, event):

        # If the message ends in this regular expression, redirect any replies
        # at the named user
        match = re.match(r"^(?P<msg>.*)(?:\s+@\s*(?P<target>[^ ]+))\s*$", event.message)
        if match:
            gd = match.groupdict()
            newtarget = gd['target']
            event.message = gd['msg']
        else:
            newtarget = None

        def reply(msg, userprefix=True, notice=False, direct=False):
            """This function is inserted to every irc.on_privmsg event that's
            sent. It sends a reply directed at the user that sent the message.

            if userprefix is True (the default), the message is prefixed by
            "user: ", so that the user is notified and the reply is
            highlighted. (This is skipped for direct replies)

            If notice is True, the reply is sent as an irc NOTICE command
            instead of a PRIVMSG

            If direct is True, the message will be sent direct to the user
            instead of through the channel where this message originated. (This
            obviously has no effect if the incoming message was a direct
            message; the reply will always be direct)

            """
            if notice:
                eventname = "irc.do_notice"
            else:
                eventname = "irc.do_msg"

            nick = event.user.split("!",1)[0]
            
            # In addition to if it was explicitly requested, send the response
            # "direct" if the incoming response was sent direct to us
            direct = direct or event.direct

            # Decide whether to prefix the name to the message: if requested,
            # but not if the message will be sent back directly
            if userprefix and not direct:
                msg = "%s: %s" % (newtarget or nick, msg)

            # Decide where to send it back: send it direct if it was sent
            # incoming direct (or if direct was requested). Otherwise, send it
            # to the channel
            if direct:
                outchannel = nick
            else:
                outchannel = event.channel

            newevent = Event(eventname, user=outchannel, message=msg)
            self.transport.send_event(newevent)
        event.reply = reply
        return event

class HasOp(BotPlugin):
    """A simple plugin to determine if the bot has OP in a channel or not.
    Also fires an event irc.hasop.acquired when op is acquired (no matter the
    source)
    
    """
    REQUIRES = ["ircutil.Names"]
    def start(self):
        super(HasOp, self).start()

        self.has_op = {}

        self.provides_request("irc.has_op")
        self.listen_for_event("irc.on_join")
        self.listen_for_event("irc.on_mode_change")

    @defer.inlineCallbacks
    def on_request_irc_has_op(self, channel):

        try:
            defer.returnValue( self.has_op[channel] )
        except KeyError:

            names_list = (yield self.transport.issue_request("irc.names",channel))

            nick = (yield self.transport.issue_request("irc.getnick"))

            has_op = "@"+nick in names_list
            self.has_op[channel] = has_op
            defer.returnValue(has_op)

    def on_event_irc_on_join(self, event):
        """If we find ourself joining a channel that we thought we had op, then
        we actually don't anymore. This happens on disconnects/reconnects or on
        a manual part/join. Anything that doesn't involve this plugin being
        restarted.

        """
        self.has_op[event.channel] = False

    @defer.inlineCallbacks
    def on_event_irc_on_mode_change(self, event):
        """Called when we observe a mode change. Check to see if it was an op
        operation on ourselves and cache it

        """
        mynick = (yield self.transport.issue_request("irc.getnick"))

        if (event.set == True and "o" == event.mode and
                event.arg == mynick):
            # Op acquired. Make a note of it
            self.has_op[event.channel] = True
            self.transport.send_event(Event("ircutil.hasop.acquired",
                channel=event.channel))

        elif (event.set == False and "o" == event.mode and
                event.arg == mynick):
            # Op gone
            log.msg("Lost op on {0}".format(event.channel))
            self.has_op[event.channel] = False
            self.transport.send_event(Event("ircutil.hasop.lost",
                channel=event.channel))

class ChanMode(EventWatcher, BotPlugin):
    """A simple plugin that provides channel mode information to other channels
    
    """
    REQUIRES = []
    def start(self):
        super(ChanMode, self).start()

        self.mode = {}

        self.provides_request("irc.chanmode")

        self.listen_for_event("irc.on_join")
        self.listen_for_event("irc.on_mode_change")
        self.listen_for_event("irc.on_unknown")

    @defer.inlineCallbacks
    def on_request_irc_chanmode(self, channel):

        try:
            defer.returnValue( self.mode[channel] )
        except KeyError:

            yield self._get_mode(channel)

            defer.returnValue( self.mode[channel] )

    @non_reentrant(channel=1)
    @defer.inlineCallbacks
    def _get_mode(self, channel):
        log.msg("Sending a request for the mode of channel {0}".format(channel))
        self.transport.send_event(Event("irc.do_raw",line="MODE {0}".format(channel)))

        reply = (yield self.wait_for(Event("irc.on_unknown", command="RPL_CHANNELMODEIS"),
                timeout=5))
        
        if not reply:
            raise Exception("no response from server")

        mode, params = reply.params[2], reply.params[3:]

        self.mode[channel] = (mode, params)
        log.msg("mode in {chan} is {0} {1}".format(mode, " ".join(params), chan=channel))

    @defer.inlineCallbacks
    def on_event_irc_on_mode_change(self, event):
        """On channel join and when we see a mode change, issue a mode request
        and record the full modeline

        """
        nick = (yield self.transport.issue_request("irc.getnick"))
        if event.channel == nick:
            return
        self._get_mode(event.channel)
    def on_event_irc_on_join(self, event):
        """On channel join and when we see a mode change, issue a mode request
        and record the full modeline

        """
        self._get_mode(event.channel)
