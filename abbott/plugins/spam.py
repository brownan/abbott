# encoding: UTF-8

from collections import deque
import time

from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass, require_channel
from . import ircop

class Spam(CommandPluginSuperclass):
    """A plugin that watches the configured channels for spammers. A spammer is
    defined as a user that says more than X lines in Y seconds, where X and Y
    are parameters tunable on a per-channel basis.

    """
    REQUIRES = ["admin.IRCAdmin"]

    # Channel maps channel names to (X,Y) tuples
    DEFAULT_CONFIG = {
            "channel": dict(),
            "msg": dict(),
            "duration": dict(),
            }

    def start(self):
        super(Spam, self).start()

        permgroup = self.install_cmdgroup(
                grpname="spam",
                permission="ircspam",
                helptext="Spam control configuration commands",
                )

        permgroup.install_command(
                cmdname="on",
                cmdusage="<X lines> <Y seconds>",
                argmatch = r"(?P<X>\d+) (?P<Y>\d+)$",
                callback=self.spamcfg,
                helptext="Enables the spam plugin on this channel, configured to detect when a user says X lines in Y seconds",
                )
        permgroup.install_command(
                cmdname="off",
                callback=self.spamcfg,
                helptext="Enables the spam plugin on this channel, configured to detect when a user says X lines in Y seconds",
                )

        permgroup.install_command(
                cmdname="setmsg",
                cmdusage="<message>",
                argmatch="(?P<msg>.+)$",
                callback=self.setmsg,
                helptext="Sets the message said to a user when they spam on this channel. Use the string \\n to specify multiple lines",
                )
        permgroup.install_command(
                cmdname="setduration",
                cmdusage="<duration in seconds>",
                argmatch=r"(?P<duration>\d+)$",
                callback=self.setduration,
                helptext="Sets the duration of the quiet when a user spams on this channel",
                )

        # This maps nicknames to a deque of length X full of the last X
        # timestamps
        self.timestamps = {}
    
    @require_channel
    def spamcfg(self, event, match):
        # not sure why I decided to implement the enable and disable functions
        # as one function. I'm too lazy to split them at this point though.
        channel = event.channel
        gd = match.groupdict()
        X = gd.get("X", None)
        Y = gd.get("Y", None)

        if not X:
            # disable
            try:
                del self.config['channel'][channel]
            except KeyError:
                event.reply("Spam detection is already off in {0}!".format(channel))
            else:
                event.reply("Spam detection is now off in {0}.".format(channel))
                self.config.save()
            return
        
        self.config['channel'][channel] = (int(X),int(Y)) 
        self.config.save()
        event.reply("Spam detection is on. Users will be quieted when they say {0} lines in {1} seconds".format(X,Y))

    
    @defer.inlineCallbacks
    def on_event_irc_on_privmsg(self, event):
        super(Spam, self).on_event_irc_on_privmsg(event)
        channel = event.channel
        nick = event.user.split("!")[0]
        try:
            channelcfg = self.config["channel"][channel]
        except KeyError:
            return

        X, Y = channelcfg

        timestamps = self.timestamps.get(nick, None)

        if not timestamps or timestamps.maxlen != X:
            timestamps = deque(maxlen=X)
            self.timestamps[nick] = timestamps

        now = time.time()

        timestamps.append(now)

        if len(timestamps) == X and timestamps[0] + Y > now:

            # Positive spammer
            mask = "*!*@{0}".format(event.user.split("@")[-1])
            try:
                yield self.transport.issue_request("ircadmin.timedquiet",
                        channel, mask, self.config['duration'].get(channel,
                            60*5))
            except (ircop.OpFailed, ValueError) as e:
                log.msg("Was going to quiet user {0} for spamming but I got an error: {1}".format(nick, e))

            # If they manage to get another few lines in before they're quited,
            # don't try and quiet them again right away.
            timestamps.clear()

            msglines = self.config['msg'].get(channel, "Spamming is not allowed on this channel!")
            for l in msglines.split("\\n"):
                event.reply(l, direct=True, notice=True)

            #event.reply("Spamming is not allowed on this channel. Please be respectful of the channel rules.", direct=True, notice=True)
            #event.reply("If you have a lot of text to share, use a pastebin such as http://pastie.org/", direct=True, notice=True)

    @require_channel
    def setmsg(self, event, match):
        channel = event.channel
        msg = match.groupdict()['msg']
        self.config['msg'][channel] = msg
        self.config.save()
        event.reply("Message set. Go ahead, try it out! ;)")

    @require_channel
    def setduration(self, event, match):
        channel = event.channel
        duration = int(match.groupdict()['duration'])
        self.config['duration'][channel] = duration
        self.config.save()
        event.reply("Quiet duration set to {0} seconds".format(duration))
