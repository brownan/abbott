# encoding: UTF-8
from __future__ import unicode_literals, division

import random
from collections import defaultdict, deque
import datetime
import time
import bisect

from twisted.internet import reactor, defer
from twisted.python import log

from ..command import CommandPluginSuperclass, require_channel
from ..pluginbase import EventWatcher, non_reentrant
from ..transport import Event
from . import ircop

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

# http://code.activestate.com/recipes/577363-weighted-random-choice/
def weighted_random_choice(seq, weight):
    """Returns a random element from ``seq``. The probability for each element
    ``elem`` in ``seq`` to be selected is weighted by ``weight(elem)``.

    ``seq`` must be an iterable containing more than one element.

    ``weight`` must be a callable accepting one argument, and returning a
    non-negative number. If ``weight(elem)`` is zero, ``elem`` will not be
    considered.

    """
    weights = 0
    elems = []
    for elem in seq:
        w = weight(elem)
        try:
            is_neg = w < 0
        except TypeError:
            raise ValueError("Weight of element '%s' is not a number (%s)" %
                             (elem, w))
        if is_neg:
            raise ValueError("Weight of element '%s' is negative (%s)" %
                             (elem, w))
        if w != 0:
            try:
                weights += w
            except TypeError:
                raise ValueError("Weight of element '%s' is not a number "
                                 "(%s)" % (elem, w))
            elems.append((weights, elem))
    if not elems:
        raise ValueError("Empty sequence")
    random_pos = random.uniform(0, weights)
    ix = bisect.bisect(elems, (random_pos, None))
    return elems[ix][1]


class VoiceOfTheDay(EventWatcher, CommandPluginSuperclass):
    REQUIRES = ["ircop.OpProvider", "ircutil.Names", "ircutil.IRCWhois"]
    def __init__(self, *args):
        self.started = False
        self.timer = None

        # Keeps track of the last x times that the !odds command was issued.
        # Will only do so many in a minute to prevent spam
        self.last_odds = deque(maxlen=3)

        super(VoiceOfTheDay, self).__init__(*args)

    def start(self):
        super(VoiceOfTheDay, self).start()

        self.listen_for_event("irc.on_nick_change")
        self.listen_for_event("irc.on_user_kick")

        votdgroup = self.install_cmdgroup(
                grpname="votd",
                permission="votd.configure",
                helptext="Voice of the Day configuration commands",
                )

        votdgroup.install_command(
                cmdname="enable",
                callback=self.enable,
                helptext="Turns on votd for this channel",
                )
        votdgroup.install_command(
                cmdname="disable",
                callback=self.disable,
                helptext="Turns off votd for this channel",
                )

        votdgroup.install_command(
                cmdname="draw",
                callback=self.draw,
                helptext="Draws the raffle right now",
                )

        votdgroup.install_command(
                cmdname="settime",
                cmdusage="<hour:minute>",
                argmatch=r"(?P<hour>\d+)(?:[:](?P<minute>\d+))$",
                callback=self.settime,
                helptext="Sets which hour the drawing will happen, in the current locale",
                )

        self.install_command(
                cmdname="transfer",
                cmdusage="<nick>",
                argmatch="(?P<nick>[^ ]+)$",
                callback=self.transfer,
                helptext="if you are the VOTD, transfer it to another",
                permission=None,
                )

        self.install_command(
                cmdname="odds",
                cmdusage="[nick]",
                argmatch="(?P<user>[^ ]+)?$",
                callback=self.check_prob,
                helptext="Check your odds for winning voice of the day",
                permission=None,
                )

        # Don't forget!
        self._set_timer()
        self.started = True

        # last spoken time keeps track of channel idleness. We don't want to
        # interrupt conversation, so we see when the last time anyone spoke and
        # will only do a drawing if nobody is talking
        self.lastspoken = 0

    def stop(self):
        if self.timer:
            self.timer.cancel()

        self.started = False

        super(VoiceOfTheDay, self).stop()

    def reload(self):
        super(VoiceOfTheDay, self).reload()

        # maps nicks to counts
        self.config["counter"] = defaultdict(int, self.config.get("counter", {}))

        # The channel we're doing this in, or None for disabled
        self.config['channel'] = self.config.get('channel', None)

        # The hour of the day to do the drawing
        self.config['hour'] = tuple(self.config.get('hour', (0,0)))

        # Who currently has voice due to this plugin, right now. Will be
        # devoiced at the next drawing
        self.config['currentvoice'] = self.config.get("currentvoice", None)
        # These hold the hostmast and the logged-in username of the current
        # voice, so we can give them their hat back if they leave and re-enter
        self.config['currenthostmask'] = self.config.get("currenthostmask", None)
        self.config['currentusername'] = self.config.get("currentusername", None)

        # The multipliers get multiplied by vote totals before the drawing.
        # This also defines the default for new users
        self.config['multipliers'] = defaultdict(lambda: 0.01, self.config.get("multipliers", {}))

        self.config['scalefactor'] = self.config.get("scalefactor", 100)

        self.config['win_counter'] = defaultdict(int, self.config.get("win_counter", {}))

        # reset the timer, in case the hour in the config was changed manually
        if self.started:
            self._set_timer()

        old_save = self.config.save
        def add_probs_and_save():
            # Add the probability to the saved config for convenience of
            # external apps that may want to read this data but not have
            # to calculate the odds themselves
            total = 0
            self.config['chance'] = {}
            for name, count in self.config['counter'].items():
                ecount = count * self.config['multipliers'][name] * self.config['scalefactor']
                ecount = int(ecount)
                self.config['chance'][name] = ecount
                total += ecount
            if total:
                for name, ecount in list(self.config['chance'].items()):
                    self.config['chance'][name] = ecount / total
            old_save()
        self.config.save = add_probs_and_save

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

    @defer.inlineCallbacks
    def _get_winner_info(self, winner):
        """For the given winner, does a whois and sets the currenthostmask and
        currentusername vars in the config"""
        info = (yield self.transport.issue_request("irc.whois", winner))

    @require_channel
    def enable(self, event, match):
        channel = event.channel
        self.config["channel"] = channel
        self.config.save()
        self._set_timer()
        event.reply("Done. Next scheduled drawing is {0} seconds".format(
                find_time_until(self.config['hour']).seconds
            ))

    @require_channel
    def disable(self, event, match):
        channel = event.channel
        self.config["channel"] = None
        self.config.save()
        self._set_timer()
        event.reply("Voice of the Day disabled for {0}".format(channel))

    @require_channel
    def settime(self, event, match):
        hour = int(match.groupdict()['hour'])
        minute = int(match.groupdict().get('minute', None) or 0)
        if hour < 0 or hour > 23:
            event.reply("What kind of hour is that?")
            return
        if minute < 0 or minute > 59:
            event.reply("What kind of minute is that?")
            return
        self.config['hour'] = (hour, minute)
        self.config.save()
        self._set_timer()

        event.reply("VOTD drawing {3} happen at {0}:{1}, which is in {2} seconds".format(
            hour, minute,
            find_time_until((hour,minute)).seconds,
            "will" if self.config['channel'] else "would",
            ))

    def _timer_up(self):
        self.timer = None

        if not self.started:
            # Maybe the plugin was unloaded?
            return

        IDLE_TIME = self.config.get("idle_time", 60*5)

        now = time.time()
        if now - self.lastspoken >= IDLE_TIME:
            self._do_votd()
        else:
            towait = IDLE_TIME - (now - self.lastspoken)
            log.msg("Was going to do VOTD but the channel is active. Waiting %s seconds and trying again"%towait)
            self.timer = reactor.callLater(towait, self._timer_up)

    @non_reentrant() # since this method can take a while, sanity against an admin doing a bunch of !votd draws
    @defer.inlineCallbacks
    def _do_votd(self, channel=None):
        # Assume the timer is already None or has fired to call this method
        channel = self.config["channel"] or channel
        if not channel:
            raise RuntimeError("_do_votd() was called, but no channel defined")
        log.msg("Doing VOTD for %s" % channel)
        def say(msg):
            self.transport.send_event(Event("irc.do_msg",
                user=channel,
                message=msg,
                ))

        names = set((yield self.transport.issue_request("irc.names", channel)))

        # Gain op here
        try:
            # Enough time to say everything we need to say
            yield self.transport.issue_request("ircop.become_op", channel, 20)
        except ircop.OpFailed as e:
            say("I was going to do Voice of the Day, but there was an error. someone halp plz!")
            log.msg("Error while un-voicing previous voice. Bailing. "  + str(e))
            return

        # de-voice the current voice if he/she still has it
        currentvoice = self.config.get("currentvoice")
        if currentvoice:
            if "+"+currentvoice in names:
                yield self.transport.issue_request("ircop.devoice", channel, currentvoice)
                # edit the names set to reflect the current channel state. This
                # is only really important later so the current voice is still
                # technically eligible for the new drawing (although that
                # should be unlikely)
                names.remove("+"+currentvoice)
                names.add(currentvoice)

            self.config['currentvoice'] = None
            self.config['currenthostmask'] = None
            self.config['currentusername'] = None

        # Take a copy of the counters and use that for the rest of the method
        counter = self.config["counter"]
        self.config['counter'] = defaultdict(int, counter)

        # Halve the counter for everyone for next time. If an entry goes to 0,
        # remove it
        for i, c in list(self.config['counter'].items()):
            if c <= 1:
                del self.config['counter'][i]
            else:
                self.config['counter'][i] = c // 2

        # Prune out the multipliers dict. Users that don't have an entry in the
        # counter dict are removed
        for user in list(self.config['multipliers'].keys()):
            if user not in self.config['counter']:
                del self.config['multipliers'][user]
        # And the win_counter dict, but only if the value is 0
        for user, count in list(self.config["win_counter"].items()):
            if user not in self.config["counter"] and count == 0:
                del self.config["win_counter"][user]

        # don't count any user that isn't actually here, and users that already
        # have voice or op for some other reason
        names = set(
                x for x in names if not (x.startswith("@") or x.startswith("+"))
                )
        for contestant in list(counter.keys()):
            if contestant not in names:
                del counter[contestant]

        effective_entries = dict(
                    (user, int(
                                counter[user] *
                                self.config['multipliers'][user] *
                                self.config['scalefactor']
                              )
                    ) for user in counter.keys()
                )

        try:
            winner = weighted_random_choice(iter(counter.keys()),
                    effective_entries.get
                    )
        except ValueError:
            say("I was going to do the voice of the day, but nobody seems to be eligible =(")
            self.config.save()
            return

        # The following are not used in calculations but for informational
        # purposes and in the response messages
        total_entries = sum(effective_entries.values())
        chances = dict(
                (user, eentry/total_entries*100)
                for user, eentry in effective_entries.items()
                )
        winner_chance = chances[winner]


        # Adjust all the multipliers up, except for the winner
        for user, m in list(self.config['multipliers'].items()):
            # (except the winner)
            if user != winner:
                self.config['multipliers'][user] = min(1.0, m*1.5)
            else:
                self.config['multipliers'][user] = m*0.01

        self.config.save()

        say("Ready everyone? It’s time to choose a new Voice of the Day!")
        yield self.wait_for(timeout=3)

        # do this here because we use this value below
        self.config["win_counter"][winner] += 1

        say("{phrase} {0:.2f}%{otherphrase}".format(
                winner_chance,
                phrase=
                       (lambda sorted_chance:
                           # Had the most odds with a >xx% lead over the runner up
                           "In a landslide win with" if sorted_chance[-1] == winner_chance and winner_chance - sorted_chance[-2] > 5 else
                           # Had the most odds with a <xx% lead
                           "Narrowly beating the competition with" if sorted_chance[-1] == winner_chance else
                           # Had the second most odds
                           "the underdog in today’s race with" if sorted_chance[-2] == winner_chance else
                           # Special phrases for low odds
                           "with the impossible odds of" if winner_chance<1 else
                           "beating the odds with" if winner_chance < 5 else
                           # catch all
                           "coming in with"
                           )(sorted(chances.values())),

                otherphrase=
                        (lambda win_count, sorted_winners:
                                ", today’s first-time winner is…" if win_count == 1 else
                                " and winning for the second time, today’s hat goes to…" if win_count == 2 else
                                ", today’s winner and three-time champion of voice is…" if win_count == 3 else
                                " and tied for number of all-time wins with {0}, today’s hat goes to…".format(win_count) if win_count == sorted_winners[-1] == sorted_winners[-2] else
                                ", presenting the winner and reigning champion of voice with {0} all-time wins…".format(win_count) if win_count == sorted_winners[-1] else
                                ", presenting the winner and runner-up in all-time wins with {0}…".format(win_count) if win_count == sorted_winners[-2] else
                                " and {0} total wins, today the hat goes to…".format(win_count)
                        )(self.config["win_counter"][winner], sorted(self.config["win_counter"].values())),
                ))
        yield self.wait_for(timeout=2)
        say("{0}!".format(winner))

        yield self.wait_for(timeout=1)
        yield self.transport.issue_request("ircop.voice", channel, winner)
        self.config['currentvoice'] = winner
        self.config.save()
        yield self.wait_for(timeout=5)
        extra = self.config.get("extra", "until next time...").split("\n")
        for l in extra:
            say(l.format(winner=winner))
            yield self.wait_for(timeout=2)

        self._set_timer()



    def draw(self, event, match):
        channel = event.channel
        if self.config['channel'] and self.config['channel'] != channel:
            event.reply("I can only do that in {0}".format(self.config['channel']))
        else:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self._do_votd(channel)

    @defer.inlineCallbacks
    def on_event_irc_on_privmsg(self, event):
        super(VoiceOfTheDay, self).on_event_irc_on_privmsg(event)

        if event.channel == self.config["channel"]:
            self.lastspoken = time.time()

        if getattr(event, "_was_odds", False):
            return

        # This delay is a bit of a hack. If we do e.g. a configreload, and this
        # handler happens to run before the reload, this will save the config,
        # clobbering the new one. So here we wait a second to let the other
        # handler run, reload our config, THEN we increment the counter.
        yield self.wait_for(timeout=1)
        if event.channel == self.config["channel"]:
            nick = event.user.split("!")[0]
            self.config["counter"][nick] += 1
            self.config.save()

    def on_event_irc_on_nick_change(self, event):
        oldnick = event.oldnick
        newnick = event.newnick

        if self.config["currentvoice"] and self.config["currentvoice"] == oldnick:
            self.config["currentvoice"] = newnick
            self.config.save()

    def on_event_irc_on_user_kick(self, event):
        target = event.kickee
        channel = event.channel

        if channel != self.config['channel']:
            return

        self.config['counter'][target] = 0
        self.config['multipliers'][target] = 0.01
        self.config.save()

    @non_reentrant()
    @defer.inlineCallbacks
    def transfer(self, event, match):
        target = match.groupdict()['nick']
        channel = self.config['channel']
        if channel != event.channel:
            event.reply("I'm not doing votd in this channel. This command only works in " + channel)
            return

        names = (yield self.transport.issue_request("irc.names", channel))
        if (yield event.has_permission("votd.transfer", event.channel)):
            requestor = self.config["currentvoice"]
        else:
            requestor = event.user.split("!")[0]
            if self.config["currentvoice"] != requestor:
                event.reply("You are not the VOTD. Get out of here, you!")
                return

            if "+"+requestor not in names:
                event.reply("Hey, where'd your hat go?")
                return

            if "+"+target in names:
                event.reply("{0} already has voice".format(target))
                return

            if "@"+target in names:
                event.reply("no can do")
                return
        
        if target not in names and "+"+target not in names:
            event.reply("who?")
            return

        if "+"+requestor in names:
            self.transport.issue_request("ircop.devoice", channel=channel, target=requestor)

        if "+"+target not in names:
            try:
                yield self.transport.issue_request("ircop.voice", channel=channel, target=target)
            except ircop.OpFailed as e:
                event.reply("Oops, something went wrong and I could not change the channel mode")
                log.msg(str(e))
                return

        event.reply("Bam!")
        
        self.config["currentvoice"] = target
        self.config.save()

    def check_prob(self, event, match):
        event._was_odds = True
        user = match.groupdict()['user']

        if len(self.last_odds) == self.last_odds.maxlen and time.time() - self.last_odds[0] < 60:
            reply_opts = {"notice": True, "direct": True}
        else:
            reply_opts = {}
        self.last_odds.append(time.time())

        if not user:
            user = event.user.split("!")[0]

        win_times = self.config["win_counter"].get(user, 0)

        if user.lower() == event.user.split("!")[0].lower():
            msg = "Your chance of winning the next VOTD drawing is"
            msg2 = ""
            if win_times == 0:
                msg2 = "You have never won"
            elif win_times == 1 or win_times == 2:
                msg2 = "You have won " + ("twi" if win_times-1 else "on") + "ce"
            else:
                msg2 = "You have won " + str(win_times) + " times"
            punishment = lambda x: max(0, min(int(x*0.9), x-5))
            self.config["counter"][user] = punishment(self.config["counter"][user])
            self.config.save()
        else:
            msg = "{0}’s chance of winning the next VOTD is".format(user)
            msg2 = user
            if win_times == 0:
                msg2 = " has never won"
            elif win_times == 1 or win_times == 2:
                msg2 = " has won " + ("twi" if win_times-1 else "on") + "ce"
            else:
                msg2 = " has won " + str(win_times) + " times"

        if user not in self.config['counter']:
            return

        total = 0
        for name, count in self.config['counter'].items():
            # compute the effective entry count
            ecount = count * self.config['multipliers'][name] * self.config['scalefactor']
            ecount = int(ecount)
            total += ecount

        my_ecount = self.config['counter'][user] * self.config['multipliers'][user] * self.config['scalefactor']
        my_chances = int(my_ecount) / total * 100
        event.reply("{1} {0:.2f}% with {2} entries and a multiplier of {3:.3f}".format(my_chances, msg, self.config['counter'][user], self.config['multipliers'][user]),
                **reply_opts)
        event.reply(msg2, **reply_opts)
