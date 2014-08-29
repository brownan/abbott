# encoding: UTF-8
from __future__ import unicode_literals

from collections import deque
import time
import re

from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass, require_channel
from . import ircop, ircutil
from .. import pluginbase
from ..transport import Event

class Spam(CommandPluginSuperclass):
    """A plugin that watches the configured channel for spammers/flooders.
    Tuned for the kinds of spam we see in #minecraft, this plugin looks for
    webchat users who, as their first two lines, say the same line within 2
    seconds.

    If line is less than 15 characters long, the number of lines said increases
    by 1 before they're quieted.

    If the user is not a webchat user, or the webchat user has said some other
    lines first, the number of lines said increases by 1 before they're
    quieted.

    So a webchat user may be quieted after just two lines if they are repeats
    and are the first things he/she says.

    This is a single-channel plugin.

    """
    REQUIRES = ["admin.IRCAdmin"]

    DEFAULT_CONFIG = {
            "channel": None,
            "msg": "You're talking too fast. Please be mindful and don't flood the channel.",
            "duration": 30,
            # X Lines said within Y seconds
            "LS_THRESH": (6, 7),

            # X repeat lines within Y seconds
            "REPEAT_THRESH": (3, 3),
            }

    def reload(self):
        super(Spam, self).reload()

        # The config may have changed, and we may need a larger deque
        self._initdeque()

    def _initdeque(self):
        LS_THRESH = self.config['LS_THRESH']
        REPEAT_THRESH = self.config['REPEAT_THRESH']

        # This holds the last few lines said in the channel. Specifically, each
        # element is a tuple: (hostmask, timestamp, message)
        self.lastlines = deque(maxlen=max(x[0] for x in (LS_THRESH, REPEAT_THRESH)))

    def start(self):
        super(Spam, self).start()

        self._initdeque()

        permgroup = self.install_cmdgroup(
                grpname="spam",
                permission="ircspam",
                helptext="Spam control configuration commands",
                )

        permgroup.install_command(
                cmdname="on",
                callback=self.spamon,
                helptext="Enables the spam plugin on this channel",
                )
        permgroup.install_command(
                cmdname="off",
                callback=self.spamoff,
                helptext="Enables the spam plugin on this channel",
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
    
    @require_channel
    def spamon(self, event, match):
        channel = event.channel
        if self.config['channel'] == channel:
            event.reply("Spam detection is already on for {0}".format(channel))
        else:
            self.config['channel'] = channel
            self.config.save()
            event.reply("Spam detection is now on for {0}".format(channel))

    @require_channel
    def spamoff(self, event, match):
        channel = event.channel
        self.config['channel'] = None
        event.reply("Spam detection is now off in {0}.".format(channel))
        self.config.save()
        
    @defer.inlineCallbacks
    def on_event_irc_on_privmsg(self, event):
        super(Spam, self).on_event_irc_on_privmsg(event)
        channel = event.channel
        hostmask = event.user
        nick = hostmask.split("!")[0]

        LS_THRESH = self.config['LS_THRESH']
        REPEAT_THRESH = self.config['REPEAT_THRESH']

        if self.config['channel'] != channel:
            return

        # Now go and log this line in the rotating buffer of last lines for the
        # channel. the self.lastlines deque holds (hostmask, timestamp,
        # messagestr) tuples.
        now = time.time()
        self.lastlines.append((hostmask, now, event.message))

        # Count how many lines the current user has said in the last few seconds.
        # This will always be at least 1
        linessaid = sum(1 for t in self.lastlines if
                t[0] == hostmask and t[1] + LS_THRESH[1] > now
                ) 

        # count how many lines from the current user match the current line in
        # the last few seconds. This will always be at least 1
        repeats = sum(1 for t in self.lastlines if
                t[0] == hostmask and t[1] + REPEAT_THRESH[1] > now and
                t[2] == event.message
                )

        if linessaid >= LS_THRESH[0] or repeats >= REPEAT_THRESH[0]:
            log.msg("A line by {} was detected as spam. {}/{} lines said within {} seconds, {}/{} repeat lines said within {} seconds".format(nick, linessaid, LS_THRESH[0], LS_THRESH[1], repeats, REPEAT_THRESH[0],REPEAT_THRESH[1]))

            # serve punishment:
            try:
                yield self.transport.issue_request("ircadmin.timedquiet",
                        channel, nick, self.config['duration'])
            except (ircop.OpFailed, ValueError, ircutil.NoSuchNick) as e:
                log.msg("Was going to quiet user {0} for flooding but I got an error: {1}".format(nick, e))

            # If they manage to get another few lines in before they're quited,
            # don't try and quiet them again right away. Do this by clearing
            # the last lines.
            self.lastlines.clear()

            msglines = self.config['msg']
            for l in msglines.split("\\n"):
                event.reply(l, direct=True, notice=True)

    @require_channel
    def setmsg(self, event, match):
        msg = match.groupdict()['msg']
        self.config['msg'] = msg
        self.config.save()
        event.reply("Message set. Go ahead, try it out! ;)")

    @require_channel
    def setduration(self, event, match):
        duration = int(match.groupdict()['duration'])
        self.config['duration'] = duration
        self.config.save()
        event.reply("Quiet duration set to {0} seconds".format(duration))

class ServerAd(pluginbase.EventWatcher, CommandPluginSuperclass):
    REQUIRES = ["admin.IRCAdmin"]

    DEFAULT_CONFIG = {
            "channel": None,
            "msg": "Server advertising is strictly forbidden in #minecraft. Please take the time to read the rules before you come back. http://www.reddit.com/r/minecraft/wiki/irc",
            "kickmsg": "No server advertising",
            }

    regex = re.compile(
            r'''(?:^|\s|ip(?:=|:)|\*)(\d{1,3}(?:\.\d{1,3}){3})\.?(?:\s|$|:|\*|!|\.|,|;|\?)''', re.I)

    def reload(self):
        super(ServerAd, self).reload()

    def start(self):
        super(ServerAd, self).start()
        self.listen_for_event("irc.on_user_joined")
        permgroup = self.install_cmdgroup(
                grpname="serverad",
                permission="serverad",
                helptext="Server Ad control configuration commands",
                )

        permgroup.install_command(
                cmdname="on",
                callback=self.on,
                helptext="Enables the server ad plugin on this channel",
                )
        permgroup.install_command(
                cmdname="off",
                callback=self.off,
                helptext="Enables the server ad plugin on this channel",
                )

    @require_channel
    def on(self, event, match):
        channel = event.channel
        if self.config['channel'] == channel:
            event.reply("Server Ad detection is already on for {0}".format(channel))
        else:
            self.config['channel'] = channel
            self.config.save()
            event.reply("Server Ad detection is now on for {0}".format(channel))

    @require_channel
    def off(self, event, match):
        channel = event.channel
        self.config['channel'] = None
        event.reply("Server ad detection is now off in {0}.".format(channel))
        self.config.save()

    @classmethod
    def _server_in(cls, text):
        try:
            ip = cls.regex.findall(text)
            if ip:
                split_ip = [int(i) for i in ip[0].split(".")]
            else:
                return False
        except ValueError:
            return False
        if split_ip[:3] == [10, 0, 0]:
            return False
        elif split_ip[:3] == [127, 0, 0]:
            return False
        elif split_ip[:2] == [192, 168]:
            return False
        elif split_ip == [0] * 4:
            return False
        elif split_ip in [[1,2,3,4], [1,1,1,1], [8,8,8,8], [8,8,4,4]]:
            return False
        for i in split_ip:
            if not i <= 255:
                return False
        return True

    @defer.inlineCallbacks
    def on_event_irc_on_user_joined(self, event):
        nick = event.user.split("!")[0]
        channel = event.channel

        if channel == self.config['channel']:
            yield self.watch_user(nick, channel)

    @pluginbase.non_reentrant(self=0, nick=1)
    @defer.inlineCallbacks
    def watch_user(self, nick, channel):
        joined_time = time.time()
        while time.time() < joined_time + 60*5:

            event = (yield self.wait_for(
                    Event("irc.on_privmsg", channel=channel),
                    timeout=joined_time+60*5-time.time()
                ))

            if not event:
                # Timed out
                break

            if event.user.split("!")[0] != nick:
                continue

            if self._server_in(event.message):
                yield self.transport.issue_request("ircop.kick", event.channel, nick,
                        self.config['kickmsg'])
                self.transport.send_event(Event("irc.do_notice",
                        user=nick,
                        message=self.config['msg'],
                        ))

class ChannelAd(ServerAd):
    DEFAULT_CONFIG = {
            "channel": None,
            "msg": "Channel advertising is strictly forbidden in #minecraft. Please take the time to read the rules before you come back. http://www.reddit.com/r/minecraft/wiki/irc",
            "kickmsg": "No channel advertising",
            }

    regex = re.compile(r"join #[^ ]+")

    @classmethod
    def _server_in(cls, text):
        return bool(cls.regex.search(text))
