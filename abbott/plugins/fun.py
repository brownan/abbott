import time
import random
import re

from twisted.internet import reactor

from ..pluginbase import BotPlugin

"""
This module has miscelaneous fun plugins that don't do anything useful

"""

class Repeater(BotPlugin):
    matcher = re.compile(r"^[^!\. ][^ ]{0,5}$")
    timeout = 5
    chance = 0.7

    def start(self):
        self.listen_for_event("irc.on_privmsg")

        self.lastline = None
        self.lasttime = 0

    def on_event_irc_on_privmsg(self, event):

        if event.direct:
            return

        match = self.matcher.match(event.message)
        if not match:
            self.lastline = None
            return

        if time.time() < self.lasttime + self.timeout:
            self.lastline = None
            return

        if self.lastline != event.message:
            self.lastline = event.message
            return

        if random.random() > self.chance:
            # Don't reset the lastline here. Multiple repeats should increase
            # the chance
            return

        delay = random.uniform(0.5, 3)
        def later():
            self.lastline = None
            self.lasttime = time.time()
            event.reply(direct=False, userprefix=False, notice=False,
                    msg=event.message)
        reactor.callLater(delay, later)

        # A few more assurances that we won't repeat anything until this one
        # goes through
        self.lastline = None
        self.lasttime = time.time() + delay
