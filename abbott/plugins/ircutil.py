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
            log.msg("Got whois item %s: %s" % (command, params[1:]))
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
        def success(info):
            timer.cancel()
            return info
        d.addCallback(success)

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
        
