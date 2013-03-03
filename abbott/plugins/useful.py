# encoding: UTF-8
import re
from functools import partial
import os
from io import StringIO

from twisted.internet import defer, reactor
from twisted.python import log
from twisted.internet.protocol import ProcessProtocol

from ..pluginbase import BotPlugin
from ..command import CommandPluginSuperclass

"""
Miscellaneous, useful plugins

"""

class TempConverter(BotPlugin):
    # A list of keywords that will cause no temperatures to be converted
    blacklist = frozenset(["capacitor", "coulomb", "farad"])
    c_re = re.compile(r"""
            # Make sure it's either at the beginning of a word, beginning of the
            # line, or at least not proceeded by an alphanumeric character
            (?: \A | \b | [ ] )
            (?<![%]) # Negative lookbehind assertion to exclude temps that
                     # come after a %
            (
                [-−]? # optional minus
                \d+ # Capture a number
                (?: [.] \d+)? # optional decimal point
            )
            [  ]? # Optional space, or non-breaking space
            (?: degrees[  ] )? # An optional "degrees " spelled out
            (?: ° )? # An optional degrees sign
            C # Capital C
            (?: elsius|entigrade )? # optionally spelled out
            \b # only capture at word boundaries
            """, re.X)
    f_re = re.compile(r"""
            # Make sure it's either at the beginning of a word, beginning of the
            # line, or at least not proceeded by an alphanumeric character
            (?: \A | \b | [ ] )
            (?<![%]) # Negative lookbehind assertion to exclude temps that
                     # come after a %
            (
                [-−]? # optional minus
                \d+ # Capture a number
                (?: [.] \d+)? # optional decimal point
            )
            [  ]? # Optional space, or non-breaking space
            (?: degrees[  ] )? # An optional "degrees " spelled out
            (?: ° )? # An optional degrees sign
            F # Capital F
            (?: ahrenheit )? # optionally spelled out
            \b # only capture at word boundaries
            """, re.X)

    def start(self):
        self.listen_for_event("irc.on_privmsg")

    def on_event_irc_on_privmsg(self, event):
        for word in self.blacklist:
            if word in event.message.lower():
                return
        c_matches = set(self.c_re.findall(event.message))
        f_matches = set(self.f_re.findall(event.message))

        reply = partial(event.reply, direct=False, userprefix=False, notice=False)

        if c_matches and not f_matches:
            # Convert the given C to F
            replies = []
            for c in c_matches:
                c = c.replace("−","-")
                if len(c) > 6:
                    continue
                c = int(round(float(c)))
                f = (c * 9 / 5) + 32
                f = int(round(f))

                replies.append("%d °C is %d °F" % (c, f))

            reply("(btw: " + ", ".join(replies) + ")")

        elif f_matches and not c_matches:
            # Convert the given F to C
            replies = []
            for f in f_matches:
                f = f.replace("−","-")
                if len(f) > 6:
                    continue
                f = int(round(float(f)))
                c = (f - 32) * 5 / 9
                c = int(round(c))

                replies.append("%d °F is %d °C" % (f, c))

            reply("(btw: " + ", ".join(replies) + ")")

class MyProcessProtocol(ProcessProtocol):
    """Runs a command, and calls a callback with output and optionally stderr
    on exit. Optional timeout, too!

    """
    def __init__(self, callback, stderr=False, timeout=0, timeoutstr=""):
        self.callback=callback
        self.capstderr=stderr
        self.timeout = timeout
        self.timeoutstr = timeoutstr
        self.running=True

        self.output = StringIO()


    def timed_out(self):
        if self.running:
            self.transport.signalProcess("KILL")
            self.running=False
            if self.timeoutstr:
                self.callback.callback(self.timeoutstr)

    def connectionMade(self):
        if self.timeout:
            self.timer = reactor.callLater(self.timeout, self.timed_out)

    def outReceived(self, data):
        self.output.write(data)
    def errReceived(self, data):
        if self.capstderr:
            self.output.write(data)

    def processEnded(self, status):
        if self.running:
            self.running = False
            self.callback.callback(self.output.getvalue())


class Units(CommandPluginSuperclass):
    def start(self):
        super(Units, self).start()

        self.install_command(
                cmdname="convert",
                cmdmatch="convert|units?",
                cmdusage="<from unit> [to <to unit>]",
                argmatch="(?P<from>.+?)(?: (in|to) (?P<to>.+))?$",
                permission=None,
                helptext="Invokes the 'units' command to do a unit conversion.",
                callback=self.invoke_units,
                )

        self.install_command(
                cmdname="define",
                cmdusage="<unitname> [as] <unitdefinition>",
                argmatch=r"(?P<name>\w+-?) (?:as )?(?P<def>.+)$",
                helptext="Define a new unit for use with the units command",
                callback=self.define,
                )

    def define(self, event, match):
        gd = match.groupdict()
        unitname = gd['name']
        definition = gd['def']

        with open("customunits.dat", "r") as cu:
            contents = cu.readlines()

        with open("customunits.dat", "w") as out:
            redef = False
            for line in contents:
                try:
                    if line.split()[0].lower() == unitname.lower():
                        redef=True
                        continue
                except IndexError:
                    pass
                out.write(line)

            out.write("%s\t%s\n" % (unitname, definition))

        if redef:
            event.reply("Unit %s redefined as %s" % (unitname, definition))
        else:
            event.reply("Unit %s now defined as %s" % (unitname, definition))

    @defer.inlineCallbacks
    def invoke_units(self, event, match):
        gd = match.groupdict()
        if gd['to']:
            args = [gd['from'], gd['to']]
        else:
            args = [gd['from']]
        try:
            open("customunits.dat", "r").close()
        except IOError:
            open("customunits.dat", "w").close()
        d = defer.Deferred()
        reactor.spawnProcess(
                MyProcessProtocol(d,
                    stderr=False,
                    timeout=2,
                    timeoutstr="Command timed out. Do you have a circular definition?",
                    ),
                "/usr/bin/units",
                [
                    "/usr/bin/units",
                    "--verbose",
                    "-f", "customunits.dat",
                    "-f", "",
                    "--"
                ] + args,
                )
        output = (yield d)

        for line in output.split("\n"):
            line = line.strip()
            if not line:
                continue
            event.reply(line)

class Mueval(CommandPluginSuperclass):
    class MuevalProtocol(ProcessProtocol):
        def __init__(self, d):
            self.d = d
            self.s = StringIO()
        def outReceived(self, text):
            self.s.write(text)
        def processEnded(self, reason):
            self.d.callback(self.s.getvalue())

    def start(self):
        super(Mueval, self).start()

        self.install_command(
                cmdname="mueval",
                cmdusage="<expression>",
                argmatch="(?P<expression>.+)$",
                permission=None,
                helptext="Evaluates a line of Haskell and replies with the output.",
                callback=self.invoke_mueval,
                )


    @defer.inlineCallbacks
    def invoke_mueval(self, event, match):
        gd = match.groupdict()
        d = defer.Deferred()
        reactor.spawnProcess(
                Mueval.MuevalProtocol(d),
                "/usr/bin/env",
                [ "/usr/bin/env",
                   "mueval",
                   "-t", "5",
                   "-XBangPatterns",
                   "-XImplicitParams",
                   "-XNoMonomorphismRestriction",
                   "-XTupleSections",
                   "-XViewPatterns",
                   "-XScopedTypeVariables",
                   "-e", gd["expression"].encode("UTF-8"),
                   ],
                env=os.environ,
            )
        output = (yield d)

        lines = output.split("\n")
        lines = [x.strip() for x in lines]
        lines = [x for x in lines if x]
        lines = ["; ".join(lines)]

        for line in lines:
            maxlen = 200
            if len(line) >= maxlen:
                line = line[:maxlen-3] + "…"
            event.reply(line)

class URLShortener(BotPlugin):
    minlength=95
    urlmatcher = re.compile(r"""
        \b
        (                       # Capture 1: entire matched URL
          (?:
            https?://               # http or https protocol
            |                       #   or
            www\d{0,3}[.]           # "www.", "www1.", "www2." … "www999."
            |                           #   or
            [a-z0-9.\-]+[.][a-z]{2,4}/  # looks like domain name followed by a slash
          )
          (?:                       # One or more:
            [^\s()<>]+                  # Run of non-space, non-()<>
            |                           #   or
            \(([^\s()<>]+|(\([^\s()<>]+\)))*\)  # balanced parens, up to 2 levels
          )+
          (?:                       # End with:
            \(([^\s()<>]+|(\([^\s()<>]+\)))*\)  # balanced parens, up to 2 levels
            |                               #   or
            [^\s`!()\[\]{};:'".,<>?«»“”‘’]        # not a space or one of these punct chars
          )
        )
        """,
        re.VERBOSE | re.IGNORECASE)

    def start(self):
        self.listen_for_event("irc.on_privmsg")

        try:
            import googl
        except ImportError:
            print("Please install the python package 'python-googl'")
            raise
        self.shortener = googl.Googl()

    def on_event_irc_on_privmsg(self, event):

        match = self.urlmatcher.search(event.message)

        if not match: return

        url = match.group(1)

        if len(url) < self.minlength:
            return

        log.msg("Shortening '%s'" % url)
        shortened = self.shortener.shorten(url)

        event.reply("^ %s" % shortened['id'], userprefix=False)

class Owner(CommandPluginSuperclass):
    """Just a simple plugin to print out the bot's owner. There is no online
    config interface, so edit the json yourself and issue a configreload to
    set.

    """
    def start(self):
        super(Owner, self).start()
        self.install_command(
                cmdname="owner",
                helptext="Display who owns me",
                callback=self.do_owner,
                )
        self.install_command(
                cmdname="code",
                cmdmatch="code|source",
                helptext="Replies with a link to my code. I'm open source!",
                callback=self.do_code,
                )

    def do_owner(self, event, match):
        if "owner" in self.config:
            event.reply("My owner is " + self.config['owner'])
        else:
            event.reply("I... I don't know! /me cries")
    def do_code(self, event, match):
        if "code" in self.config:
            event.reply(self.config["code"])
        else:
            event.reply("No repository configured. Please ask the owner to set one in the config", direct=True, notice=True)
