# encoding: UTF-8
import re
from functools import partial

from ..pluginbase import BotPlugin

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
            [ ]? # Optional space
            (?: degrees[ ] )? # An optional "degrees " spelled out
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
            [ ]? # Optional space
            (?: degrees[ ] )? # An optional "degrees " spelled out
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

                replies.append(u"%d°C is %d°F" % (c, f))

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

                replies.append(u"%d°F is %d°C" % (f, c))

            reply("(btw: " + ", ".join(replies) + ")")
