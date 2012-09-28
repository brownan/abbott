import re
from collections import defaultdict

from twisted.internet import reactor
from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass
from ..transport import Event
from ..pluginbase import BotPlugin

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

        #self.install_command(
        #        cmdname="whois",
        #        argmatch=r"(?P<nick>[^ ]+)",
        #        callback=self.do_whois,
        #        cmdusage="<nick>",
        #        helptext="Does a whois and prints the results. This command is meant for debugging.",
        #        permission="irc.whois",
        #        )

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
                log.err("Got a RPL_WHOISUSER but we're already in a whois!")
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
        event.reply("Whois info for %s:" % nick)
        for command, params in info.iteritems():
            event.reply("%s: %s" % (command, params))

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
