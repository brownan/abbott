# encoding: UTF-8

import random
import datetime
import time
import string

from twisted.internet import reactor, defer
from twisted.python import log

try:
    from pretty import date as prettydate
except ImportError:
    print("Please install the pypi package 'py-pretty'")
    raise

from ..command import CommandPluginSuperclass, require_channel
from ..pluginbase import EventWatcher, non_reentrant
from ..transport import Event

def find_time_until(hour_minute):
    """Returns a datetime.timedelta for the time interval between now and the
    next time of the given hour, in the current locale

    """
    today = datetime.date.today()

    hour = datetime.time(hour=hour_minute[0], minute=hour_minute[1])

    targetdt = datetime.datetime.combine(today, hour)
    if targetdt <= datetime.datetime.now():
        tomorrow = today + datetime.timedelta(days=1)
        targetdt = datetime.datetime.combine(tomorrow, hour)

    timeuntil = targetdt - datetime.datetime.now()
    return timeuntil

def td_to_str(td):
    """Takes a timedelta and returns a string describing the interval as if it
    were taking place at a point in the future from now

    """
    return prettydate(
            datetime.datetime.now() + td
            )
class WordOfTheDay(EventWatcher, CommandPluginSuperclass):
    REQUIRES = ["ircop.OpProvider", "ircutil.Names"]
    DEFAULT_CONFIG = {
            "channel": None,
            "hour": [0,0],
            "dictionary": "/usr/share/dict/american-english",
            "idle_time": 60*5,
            "theword": None,
            "winners": [],
            "maxwinners": 5,
            }

    def __init__(self, *args):
        self.started = False
        self.timer = None

        super(WordOfTheDay, self).__init__(*args)

    def start(self):
        super(WordOfTheDay, self).start()

        self.listen_for_event("irc.on_nick_change")

        # Don't forget!
        self._set_timer()
        self.started = True

        # last spoken time keeps track of channel idleness. We don't want to
        # interrupt conversation, so we see when the last time anyone spoke and
        # will only do a drawing if nobody is talking
        self.lastspoken = 0

        # Last Winner Time keeps track of the time the last winner was awarded
        # voice. This is used later to punish those that try, immediately after
        # a winner, to spam and get voice by copying the winning line.
        self.lastwintime = 0
        self.winlines = []

        wotdgroup = self.install_cmdgroup(
                grpname="wotd",
                permission="wotd.configure",
                helptext="Woice of the Day configuration commands",
                )
        wotdgroup.install_command(
                cmdname="reset",
                callback=self.reset,
                helptext="Resets the word of the day right now",
                )

    def stop(self):
        if self.timer:
            self.timer.cancel()

        self.started = False

        super(WordOfTheDay, self).stop()

    def reload(self):
        super(WordOfTheDay, self).reload()

        # Get the words of the file
        self.words = []
        for line in open(self.config['dictionary'], "r"):
            line = line.strip()
            self.words.append(line)

        # reset the timer, in case the hour in the config was changed manually
        if self.started:
            self._set_timer()

    def _set_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

        channel = self.config["channel"]
        if channel:
            timeuntil = find_time_until(self.config['hour'])
            self.timer = reactor.callLater(
                    max(int(timeuntil.total_seconds()), 5),
                    self._timer_up,
                    )

    def _timer_up(self):
        self.timer = None

        if not self.started:
            # Maybe the plugin was unloaded?
            return

        IDLE_TIME = self.config["idle_time"]

        now = time.time()
        if now - self.lastspoken >= IDLE_TIME:
            self._do_wotd()
        else:
            towait = IDLE_TIME - (now - self.lastspoken)
            log.msg("Was going to reset WOTD but the channel is active. Waiting %s seconds and trying again"%towait)
            self.timer = reactor.callLater(towait, self._timer_up)

    @require_channel
    def reset(self, event, match):
        channel = event.channel
        if self.config['channel'] and self.config['channel'] != channel:
            event.reply("I can only do that in {0}".format(self.config['channel']))
        else:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self._do_wotd(channel)

    @non_reentrant()
    @defer.inlineCallbacks
    def _do_wotd(self, channel=None):
        # Assume the timer is already None or has fired to call this method
        channel = self.config["channel"] or channel
        if not channel:
            raise RuntimeError("_do_wotd() was called, but no channel defined")
        log.msg("Doing WOTD for %s" % channel)
        def say(msg):
            self.transport.send_event(Event("irc.do_msg",
                user=channel,
                message=msg,
                ))

        names = set((yield self.transport.issue_request("irc.names", channel)))

        # De-voice anyone that still has it.
        # intersect current channel set with a set of current voices
        current_voices = set("+"+x for x in self.config['winners']) & names
        reqs = []
        for v in current_voices:
            reqs.append(
                    self.transport.issue_request("ircop.devoice", channel, v.lstrip("+"))
                    )
        self.config['winners'] = []
        self.winlines = []
        self.lastwintime = 0
        for r in reqs:
            yield r

        # Announce what the winning word was.
        if self.config['theword']:
            if current_voices:
                say("Congratulations to our winners. The word of the day was “{0}”".format(self.config['theword']))
            else:
                say("The word of the day was “{0}”, but nobody guessed it :(. Choosing a new one…".format(self.config['theword']))
            self.config['theword'] = None
        else:
            say("Starting the Word of the Day game!")
        yield self.wait_for(timeout=2)
        say("Guess the word of the day and receive voice! (no spamming)")

        # Choose a new word
        self.config['theword'] = random.choice(self.words)
        log.msg("New word of the day: {0}".format(self.config['theword']))
        self.config.save()

        self._set_timer()



    @defer.inlineCallbacks
    def on_event_irc_on_privmsg(self, event):
        super(WordOfTheDay, self).on_event_irc_on_privmsg(event)

        if event.channel == self.config["channel"]:
            self.lastspoken = time.time()

        # This delay is a bit of a hack. If we do e.g. a configreload, and this
        # handler happens to run before the reload, this will save the config,
        # clobbering the new one. So here we wait a second to let the other
        # handler run, reload our config, THEN we proceded with this method
        yield self.wait_for(timeout=1)

        if event.channel == self.config["channel"]:
            
            nick = event.user.split("!")[0]

            if nick in self.config['winners']:
                return

            words = set(x.strip(string.punctuation) for x in event.message.lower().split())
            if self.config['theword'] in words:

                if event.message.lower() in self.winlines and time.time() - self.lastwintime < 60*2:
                    # if you repeated a message verbatim by a winner from the
                    # last 2 minutes...
                    self.transport.issue_request("ircop.kick", event.channel, nick, "What are you, a parrot?")

                elif len(self.config['winners']) >= self.config['maxwinners']:
                    # Everyone has already won for today
                    if time.time() - self.lastwintime < 10:
                        # if they just missed, kick
                        self.transport.issue_request("ircop.kick", event.channel, nick, "Oh, so close!")

                else:
                    # They won legit. Grant voice

                    self.config['winners'].append(nick)
                    log.msg("User {0} has guessed the word of the day".format(nick))
                    self.config.save()
                    # Delay a random amount to obscure the line that triggered it
                    # Warning: to avoid a rare race condition, the maximum
                    # delay here must be less than the configured waittime
                    yield self.wait_for(timeout=random.randint(5,min(30, self.config['idle_time']-1)))

                    # Set the last win time and add the winning line to the
                    # list *after* the delay, so there is no confusion
                    # involving people getting kicked for lines that look
                    # innocent
                    self.lastwintime = time.time()
                    self.winlines.append(event.message.lower())
                    yield self.transport.issue_request("ircop.voice", event.channel, nick)
                    self.transport.send_event(Event("irc.do_notice",
                        user=nick,
                        message="You have guessed the word of the day: “{0}”. Don’t tell anyone, it's a secret! Enjoy your hat.".format(self.config['theword']),
                        ))

                    if len(self.config['winners']) >= self.config['maxwinners']:
                        self.transport.send_event(Event("irc.do_msg",
                            user=event.channel,
                            message="That’s all the hats! Congrats to our winners. Until tomorrow…",
                            ))


    def on_event_irc_on_nick_change(self, event):
        oldnick = event.oldnick
        newnick = event.newnick

        if oldnick in self.config['winners']:
            self.config['winners'].remove(oldnick)
            self.config['winners'].append(newnick)
            self.config.save()

