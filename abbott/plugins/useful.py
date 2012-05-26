# encoding: UTF-8
import re
from functools import partial
import os
from StringIO import StringIO

from twisted.internet import defer, reactor
from twisted.internet.utils import getProcessOutput
from twisted.python import log
from twisted.internet.protocol import ProcessProtocol

from ..pluginbase import BotPlugin
from ..command import CommandPluginSuperclass

"""
Miscellaneous, useful plugins

"""

class TempConverter(BotPlugin):
    c_re = re.compile(ur"""
            # Make sure it's either at the beginning of a word, beginning of the
            # line, or at least not proceeded by an alphanumeric character
            (?: \A | \b | [ ] )
            (
                -? # optional minus
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
    f_re = re.compile(ur"""
            # Make sure it's either at the beginning of a word, beginning of the
            # line, or at least not proceeded by an alphanumeric character
            (?: \A | \b | [ ] )
            (
                -? # optional minus
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
        c_matches = self.c_re.findall(event.message)
        f_matches = self.f_re.findall(event.message)

        reply = partial(event.reply, direct=False, userprefix=False, notice=False)

        if c_matches and not f_matches:
            # Convert the given C to F
            replies = []
            for c in c_matches:
                if len(c) > 6:
                    continue
                c = int(round(float(c)))
                f = (c * 9 / 5) + 32
                f = int(round(f))

                replies.append(u"%d °C is %d °F" % (c, f))

            reply("(btw: " + ", ".join(replies) + ")")

        elif f_matches and not c_matches:
            # Convert the given F to C
            replies = []
            for f in f_matches:
                if len(f) > 6:
                    continue
                f = int(round(float(f)))
                c = (f - 32) * 5 / 9
                c = int(round(c))

                replies.append(u"%d °F is %d °C" % (f, c))

            reply("(btw: " + ", ".join(replies) + ")")

class Units(CommandPluginSuperclass):
    def start(self):
        super(Units, self).start()

        self.install_command(
                cmdname="convert",
                cmdmatch="convert|units?",
                cmdusage="<from unit> [to <to unit>]",
                argmatch="(?P<from>.+?)(?: to (?P<to>.+))?$",
                permission=None,
                helptext="Invokes the 'units' command to do a unit conversion.",
                callback=self.invoke_units,
                )


    @defer.inlineCallbacks
    def invoke_units(self, event, match):
        gd = match.groupdict()
        if gd['to']:
            args = [gd['from'], gd['to']]
        else:
            args = [gd['from']]
        output = (yield getProcessOutput(
            "/usr/bin/units",
            [ "--verbose", "--"] + args,
            errortoo=True,
            ))

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
                line = line[:maxlen-3] + "..."
            event.reply(line)
