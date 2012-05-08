# encoding: UTF-8
import time
import random
import re

from twisted.internet import reactor
from twisted.python import log

from ..pluginbase import BotPlugin
from ..command import CommandPluginSuperclass

"""
This module has miscellaneous fun plugins that don't do anything useful

"""

class RMSPlugin(CommandPluginSuperclass):
    def start(self):
        super(RMSPlugin, self).start()
        self.install_command(
                cmdname="rmsify",
                callback=self.rmsify,
                cmdusage="<thing to rmsify>",
                argmatch=r"(?P<text>.+)$",
                permission=None,
                helptext="RMS a thing",
                )

    def rmsify(self, event, match):
        thing = match.groupdict()['text']

        quote = u"""
I’d just like to interject for a moment. What you’re refering to as {0}, is in fact, GNU/{0}, or as I’ve recently taken to calling it, GNU plus {0}.
{1} is not an operating system unto itself, but rather another free component of a fully functioning GNU system made useful by the GNU corelibs, shell utilities and vital system components comprising a full OS as defined by POSIX.

Many computer users run a modified version of the GNU system every day, without realizing it. Through a peculiar turn of events, the version of GNU which is widely used today is often called “{0}”, and many of its users are not aware that it is basically the GNU system, developed by the GNU Project.

There really is a {0}, and these people are using it, but it is just a part of the system they use. {1} is the kernel: the program in the system that allocates the machine’s resources to the other programs that you run.
The kernel is an essential part of an operating system, but useless by itself; it can only function in the context of a complete operating system.
{1} is normally used in combination with the GNU operating system: the whole system is basically GNU with {0} added, or GNU/{0}. All the so-called “{0}” distributions are really distributions of GNU/{0}.""".format(thing, thing.capitalize())

        t=1
        for q in quote.split("\n"):
            if not q:
                continue
            reactor.callLater(t, event.reply, q, userprefix=False)
            t += 2

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
        reactor.callLater(
                delay,
                event.reply,
                direct=False,
                userprefix=False,
                notice=False,
                msg=event.message,
                )

        self.lastline = None
        self.lasttime = time.time() + delay + self.timeout
