# encoding: UTF-8
from __future__ import unicode_literals
from collections import defaultdict, deque, namedtuple
import time
import random
import re

from twisted.internet import reactor
from twisted.python import log
from twisted.internet import defer

from parsedatetime.parsedatetime import Calendar
import pretty

from ..command import CommandPluginSuperclass, require_channel
from ..pluginbase import EventWatcher
from ..transport import Event
from . import ircutil
from . import ircop

def parse_time(timestr):
    """Parses a time string and returns the number of seconds from now to wait

    """
    c = Calendar()
    result, status = c.parse(timestr)

    if status == 0:
        raise ValueError("I don't understand when you want me to do that")

    timestamp = time.mktime(result)
    now = time.time()
    return max(1, timestamp-now)

class IRCAdmin(EventWatcher, CommandPluginSuperclass):
    """Provides a command interface to IRC operator tasks. Uses the plugins in
    the ircop module to perform the operations.

    ALSO provides an interface to timed quiets for other plugins.

    """
    REQUIRES = ["ircop.OpProvider"]
    DEFAULT_CONFIG = {
            "defaulttime": None,
            }

    def __init__(self, *args):
        self.started = False

        # This dictionary maps tuples of (param, channel, mode) to twisted
        # timer objects. When the timer fires, the mode is set/unset on the
        # given channel with the given parameter
        self.later_timers = {}


        super(IRCAdmin, self).__init__(*args)

    def reload(self):
        super(IRCAdmin, self).reload()

        if "laters" not in self.config:
            self.config['laters'] = []

        if self.started:
            self._set_all_timers()

    def _set_all_timers(self):
        """Reads from the config and syncs the twisted timers with that.
        This is called when the plugin is started or reloaded. basically
        whenever we have to read the config and set twisted timers from
        that."""

        if self.initial_setalltimers_timer:
            if self.initial_setalltimers_timer.active():
                # This function was called from somewhere before the initial
                # call (maybe from reload?). Just ignore it. We'll get called
                # soon enough.
                return
            else:
                # First time this function is called. Set this to none
                self.initial_setalltimers_timer = None

        for timer in self.later_timers.values():
            timer.cancel()
        # Avoid AlreadyCancelled errors by deleting the timer objects
        self.later_timers = {}

        for activatetime, param, channel, mode in self.config['laters']:
            self._set_timer(activatetime - time.time(), param, channel, mode)


    def _set_timer(self, delay, param, channel, mode):
        """In delay seconds, issue a mode request with the given parameter on
        channel

        mode is a two character string where the first character is + or - and
        the second character is a letter

        """
        # First, cancel any existing timers and remove any existing saved
        # laters from the config that match this one.
        if (param, channel, mode) in self.later_timers:
            timer = self.later_timers.pop((param, channel, mode))
            timer.cancel()

        # Filter out any events that match this one from the persistent config.
        # If this is being called from _set_all_timers(), the current entry is
        # removed, but is added back below.
        self.config['laters'] = [item for item in self.config['laters']
                if not (item[1] == param and
                       item[2] == channel and
                       item[3] == mode
                       )]

        # This function will be run later
        @defer.inlineCallbacks
        def do_later():
            log.msg("timed request: %s for %s in %s" % (mode, param, channel))
            # First, take this item out of the mapping
            del self.later_timers[(param, channel, mode)]

            # And the persistent config
            self.config['laters'] = [item for item in self.config['laters']
                    if not (item[1] == param and
                           item[2] == channel and
                           item[3] == mode
                           )]
            self.config.save()

            # Now send the event
            try:
                try:
                    # If we can call a specific request, do so
                    yield self.transport.issue_request(
                            "ircop.{0}".format(
                                {
                                    "+b":"ban",
                                    "+q":"quiet",
                                    "+o":"op",
                                    "-o":"deop",
                                    "+v":"voice",
                                    "-v":"devoice",
                                    "-b":"unban",
                                    "-q":"unquiet"
                                    }[mode]
                                ),
                            channel=channel,
                            target=param
                            )
                except KeyError:
                    # ...otherwise, just use the generic mode call
                    yield self.transport.issue_request(
                            "ircop.mode",
                            channel=channel,
                            mode=mode,
                            param=param)
            except (ircop.OpFailed, ValueError) as e:
                s = "I was about to do a {0} {1}, but {2}".format(
                        mode,
                        param,
                        e,
                        )
                self.transport.send_event(Event("irc.do_msg",
                    user=channel,
                    message=s,
                    ))


        # Now submit the do_later() function to twisted to call it later
        timer = reactor.callLater(max(1,delay), do_later)

        log.msg("Setting {0} on {1} in {2} in {3} seconds".format(
            mode,
            param,
            channel,
            max(1,delay),
            ))

        # and file this timer away:
        self.later_timers[(param, channel, mode)] = timer

        # Save to the persistent config
        self.config['laters'].append(
                (time.time()+delay, param, channel, mode)
                )
        self.config.save()

    def on_event_irc_on_mode_change(self, event):
        """If a timer was set to un-ban or un-quiet a user, and we see them be
        un-banned or un-quieted before we get to it, cancel the timer.

        """
        if event.set == False:
            mode = "-"+event.mode
        else:
            mode = "+"+event.mode

        user = event.arg
        channel = event.channel

        # Cancel any pending timers for this
        try:
            timer = self.later_timers.pop((user, channel, mode))
        except KeyError:
            pass
        else:
            timer.cancel()

            # Also filter out the persistent config entry
            self.config['laters'] = [item for item in self.config['laters']
                    if not (item[1] == user and
                           item[2] == channel and
                           item[3] == mode
                           )]
            self.config.save()


    def stop(self):
        super(IRCAdmin, self).stop()

        for timer in self.later_timers.values():
            timer.cancel()

        # if the plugin is stopped (happens on module reload) before the
        # initial call to _set_all_timers, then make sure to cancel that timer!
        if self.initial_setalltimers_timer:
            self.initial_setalltimers_timer.cancel()

    def start(self):
        super(IRCAdmin, self).start()

        self.started = True
        
        # Start the timers for modes we need to set later. Don't start the
        # timers right away, though, because we're likely still connecting to
        # the server. This is a bit of a hack; a better way would be to detect
        # when we're connected, or have the framework automatically buffer
        # things like mode requests if sent while not connected. This hack won't
        # help, for example, if the bot is started but disconnected due to
        # network or server issues.
        self.initial_setalltimers_timer = reactor.callLater(30, self._set_all_timers)

        self.listen_for_event("irc.on_mode_change")

        self.provides_request("ircadmin.timedquiet")

        # kick command
        self.install_command(
                cmdname="kick",
                cmdmatch="kick|KICK|gtfo",
                cmdusage="<nickname> [reason]",
                argmatch = "(?P<nick>[^ ]+)( (?P<reason>.*))?$",
                permission="irc.op.kick",
                callback=self.kick,
                #deniedcallback=self.kickself,
                helptext="Kicks a user from the current channel")

        # Op commands
        self.install_command(
                cmdname="op",
                cmdusage="[nick] ...",
                argmatch="(?P<nicks>.+)?",
                permission="irc.op.op",
                callback=self.give_op,
                helptext="Gives op to the specified user",
                )
        self.install_command(
                cmdname="deop",
                cmdusage="[nick] ...",
                argmatch="(?P<nicks>.+)?",
                permission="irc.op.op",
                callback=self.take_op,
                helptext="Takes op from the specified user",
                )

        # voice commands
        self.install_command(
                cmdname="voice",
                cmdmatch="voice|VOICE|hat",
                cmdusage="[nick] ...",
                argmatch = "(?P<nicks>.+)?$",
                permission="irc.op.voice",
                callback=self.voice,
                helptext="Grants a user voice in the current channel"
                )

        self.install_command(
                cmdname="devoice",
                cmdmatch="devoice|DEVOICE|dehat|unhat",
                cmdusage="[nick] ...",
                argmatch = "(?P<nicks>.+)?$",
                permission="irc.op.voice",
                callback=self.devoice,
                helptext="Revokes a user's voice in the current channel"
                )

        # Quiet commands
        self.install_command(
                cmdname="quiet",
                cmdmatch="quiet|QUIET|mute",
                cmdusage="<nick or hostmask> [for <duration>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?P<timestr>.+))?$",
                permission="irc.op.quiet",
                callback=self.quiet,
                #deniedcallback=self.quietself,
                helptext="Quiets a user."
                )

        self.install_command(
                cmdname="unquiet",
                cmdmatch="unquiet|UNQUIET|unmute|dequiet",
                cmdusage="<nick or hostmask> [in <delay>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?P<timestr>.+))?$",
                permission="irc.op.quiet",
                callback=self.unquiet,
                helptext="Un-quiets a user"
                )

        # Ban commands
        self.install_command(
                cmdname="ban",
                cmdmatch="ban|BAN|kban|kb|kickban",
                cmdusage="<nick or hostmask> [for <duration>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?P<timestr>.+))?$",
                permission="irc.op.ban",
                callback=self.ban,
                helptext="Bans a user."
                )

        self.install_command(
                cmdname="unban",
                cmdmatch="unban|UNBAN",
                cmdusage="<nick or hostmask> [in <delay>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?P<timestr>.+))?$",
                permission="irc.op.ban",
                callback=self.unban,
                helptext="Un-bans a user"
                )

        self.install_command(
                cmdname="redirect",
                cmdmatch="redirect|fixurshit|fixurconnection|fixyourshit|fixyourconnection",
                cmdusage="<nick or hostmask> [#channel]",
                argmatch = "(?P<nick>[^ ]+)(?: (?P<channel>#[^ ]+))?",
                permission="irc.op.ban",
                callback=self.redirect,
                helptext="Redirects a user to ##FIX_YOUR_CONNECTION or the given channel for a hard-coded length of time (2 hours)"
                )

        self.install_command(
                cmdname="holdop",
                cmdusage="<time>",
                argmatch="(?P<time>.+)$",
                permission="irc.op.holdop",
                callback=self.holdop,
                helptext="Tells the bot to hold op for the given amount of time",
                )

        self.install_command(
                cmdname="mode",
                cmdusage="[+-]<mode_letter> [param] (for|until|in|at <time>)",
                argmatch="(?P<mode>[+-][a-zA-Z])(?: (?P<param>[^ ]+))?(?: (?P<timespec>(?:for|until|in|at) .+))?$",
                permission="irc.op.mode",
                callback=self.mode,
                helptext="Sets a channel mode",
                )

        self.install_command(
                cmdname="m",
                permission="irc.op.m",
                callback=self.moderatedmode,
                helptext="FOR EMERGENCY USE ONLY! Sets +m on the channel to quiet it in an emergency",
                )

        self.install_command(
                cmdname="flex",
                permission="irc.op.flex",
                argmatch="(?P<time>.+)?$",
                cmdusage="[time to hold op]",
                callback=self.flex,
                helptext="OPs you for a few seconds, to show off your powah!",
                )

        self.install_command(
                cmdname="bans",
                cmdmatch="bans|listbans",
                cmdusage="<mask> | all",
                permission="irc.op.bans",
                argmatch="(?P<mask>[^ ]+)?",
                callback=self.bans,
                helptext="Lists the time until the given mask is unbanned.",
                )
                

    @defer.inlineCallbacks
    def _nick_to_hostmask(self, nick):
        """Takes a nick or a hostmask and returns a parameter suitable for the
        +b or +q modes.

        If the items given looks like a hostmask (contains a ! and a @) then
        it is returned. If the item is an extban (starts with a $), then that
        is returned. Otherwise, it is assumed the parameter is a nickname and a
        whois is performed and the hostmask is returned with the first two
        fields wildcarded.

        This methed is intended to allow bans and quiets to match any nick!user
        combination by banning/quieting all users from that host.

        If the parameter is a nickname and no such user is found, an
        ircutil.NoSuchNick is raised. If the whois fails, an
        ircutil.WhoisTimedout is raised.

        If a hostmask is given, some syntax checking is performed. If the
        syntax can be corrected, the corrected hostmask is returned. If not, a
        VauleError is raised.

        Returnes a deferred that fires with the answer.

        """
        if nick.startswith("$"):
            # An extban. just return it.
            defer.returnValue(nick)
            return
        elif "!" in nick or "@" in nick:
            # nick is not actually a nick, but already a mask of some sort
            mask = nick

            # Do some syntax checking
            if "@" not in mask:
                raise ValueError("hostmask is missing an @")

            # It's invalid for nicks or usernames to contain ! or @ characters,
            # so we can make sure there's exactly one in the mask and not worry
            # about how to split it (from the left or from the right)
            if mask.count("@") > 1:
                raise ValueError("more than one @ in hostmask")

            if mask.count("!") > 1:
                raise ValueError("more than one ! in hostmask")

            if "!" not in mask:
                # Assume the user typed <nick>@<host> and forgot the username field
                nick, host = mask.split("@", 1)
                mask = "{0}!*@{1}".format(nick, host)

            # Here the mask has exactly one ! and exactly one @
            nick, rest = mask.split("!",1)
            username, host = rest.rsplit("@", 1)

            # I'm pretty sure freenode does the following for us (adds a
            # missing * to an empty field) but let's be complete about it and
            # try to always send a valid mask
            if len(nick) == 0:
                nick = "*"

            if len(username) == 0:
                username = "*"

            if len(host) == 0:
                # Don't assume the host is *. That could lead to easy mistakes
                # that ban/quiet everyone
                raise ValueError("No host given")

            mask = "{0}!{1}@{2}".format(
                    nick,
                    username,
                    host
                    )

            defer.returnValue(mask)
            return

        whois_results = (yield self.transport.issue_request("irc.whois", nick))

        whoisuser = whois_results['RPL_WHOISUSER']

        nick, username, host = whoisuser[0], whoisuser[1], whoisuser[2]

        if host.startswith("gateway/web/freenode/ip."):
            nick = "*"
            username = "*"
            host = host.split("/")[-1][3:]
        elif host.startswith("gateway/"):
            # some other gateway. ban by username.
            nick = "*"
            host = "gateway/*"
        else:
            nick = "*"
            username = "*"

        mask = "{0}!{1}@{2}".format(
                nick,
                username,
                host
                )

        defer.returnValue(mask)

    @require_channel
    @defer.inlineCallbacks
    def kick(self, event, match):
        """A user has issued the kick command.

        """
        groupdict = match.groupdict()
        nick = groupdict['nick']
        reason = groupdict.get("reason", None)
        channel = event.channel

        try:
            yield self.transport.issue_request("ircop.kick", channel=channel,
                target=nick, reason=reason)
        except ircop.OpFailed as e:
            event.reply(str(e))

    @require_channel
    def kickself(self, event, match):
        """A user without permission has tried to issue a kick command

        """
        targetnick = match.groupdict()['nick']
        requestor = event.user.split("!")[0]

        if targetnick == requestor:
            self.transport.issue_request("ircop.kick", channel=event.channel,
                target=requestor, reason="okay, you asked for it")
            return True
        elif random.randint(1,4) == 4:
            self.transport.issue_request("ircop.kick", channel=event.channel,
                target=requestor, reason="woops, my bad!")
            return True

    @require_channel
    @defer.inlineCallbacks
    def voice(self, event, match):
        groupdict = match.groupdict()
        if not groupdict['nicks']:
            nicks = [event.user.split("!",1)[0]]
        else:
            nicks = groupdict['nicks'].split()
        channel = event.channel

        ds = [
                self.transport.issue_request("ircop.voice", channel=channel,
                    target=nick)
                for nick in nicks
                ]
        try:
            for d in ds:
                yield d
        except ircop.OpFailed as e:
            event.reply(str(e))

    @require_channel
    @defer.inlineCallbacks
    def devoice(self, event, match):
        groupdict = match.groupdict()
        if not groupdict['nicks']:
            nicks = [event.user.split("!",1)[0]]
        else:
            nicks = groupdict['nicks'].split()
        channel = event.channel

        ds = [
                self.transport.issue_request("ircop.devoice", channel=channel,
                    target=nick)
                for nick in nicks
                ]
        try:
            for d in ds:
                yield d
        except ircop.OpFailed as e:
            event.reply(str(e))

    @require_channel
    @defer.inlineCallbacks
    def give_op(self, event, match):
        groupdict = match.groupdict()
        if not groupdict['nicks']:
            nicks = [event.user.split("!",1)[0]]
        else:
            nicks = groupdict['nicks'].split()
        channel = event.channel

        ds = [
                self.transport.issue_request("ircop.op", channel=channel,
                    target=nick)
                for nick in nicks
                ]
        try:
            for d in ds:
                yield d
        except ircop.OpFailed as e:
            event.reply(str(e))

    @require_channel
    @defer.inlineCallbacks
    def flex(self, event, match):
        nick = event.user.split("!",1)[0]
        channel = event.channel
        duration = match.groupdict()['time']
        if duration:
            try:
                duration = parse_time(duration)
            except ValueError:
                duration = 10
        else:
            duration = 10
        yield self.transport.issue_request("ircop.op", channel=channel,
                target=nick)
        yield self.wait_for(timeout=duration)
        yield self.transport.issue_request("ircop.deop", channel=channel,
                target=nick)


    @require_channel
    @defer.inlineCallbacks
    def take_op(self, event, match):
        groupdict = match.groupdict()
        if not groupdict['nicks']:
            nicks = [event.user.split("!",1)[0]]
        else:
            nicks = groupdict['nicks'].split()
        channel = event.channel

        ds = [
                self.transport.issue_request("ircop.deop", channel=channel,
                    target=nick)
                for nick in nicks
                ]
        try:
            for d in ds:
                yield d
        except ircop.OpFailed as e:
            event.reply(str(e))

    @require_channel
    @defer.inlineCallbacks
    def quiet(self, event, match):
        groupdict = match.groupdict()
        target = groupdict['nick']
        duration = groupdict['timestr']
        channel = event.channel

        if duration:
            try:
                duration = parse_time(duration)
            except ValueError as e:
                # use the default (set below)
                duration = None

        if not duration and self.config['defaulttime']:
            duration = self.config['defaulttime']

        try:
            hostmask = (yield self._nick_to_hostmask(target))
        except ircutil.NoSuchNick:
            event.reply("There is no user by that nick on the network. "
                        "Try {0}!*@* to quiet anyone with that nick, or specify a full hostmask.".format(
                        target,
                        ))
            return
        except ValueError:
            event.reply("Malformed hostmask. Hostmask syntax is <nick>!<username>@<host>", direct=True, notice=True)
            return

        try:
            yield self._do_moderequest(channel, 'q', hostmask, duration)
        except ircop.OpFailed as e:
            event.reply(str(e))

        if duration:
            event.reply("Will unquiet {} {}".format(
                    hostmask,
                    # bug: pretty.date doesn't accept floats as timestamps
                    pretty.date(int(time.time()+duration))
                ), direct=True, notice=True)

    @require_channel
    @defer.inlineCallbacks
    def quietself(self, event, match):
        groupdict = match.groupdict()
        nick = event.user.split("!")[0]
        if random.randint(1,3) == 3 or nick == groupdict['nick']:
            try:
                yield self._do_moderequest(
                        event.channel,
                        "q",
                        nick,
                        duration=10,
                        )
            except ircop.OpFailed:
                defer.returnValue(False)
            if nick != groupdict['nick']:
                reactor.callLater(7,
                        event.reply,
                        "Woops, my bad!",
                        )
            defer.returnValue(True)

    @require_channel
    @defer.inlineCallbacks
    def redirect(self, event, match):
        groupdict = match.groupdict()
        # nick here could be a nick, a hostmask (with possible wildcards), or
        # an extban.
        nick = groupdict['nick']
        channel = event.channel
        destchan = groupdict["channel"]
        if not destchan:
            destchan = "##FIX_YOUR_CONNECTION"

        if "!" in nick or "@" in nick:
            # given a mask, not a nick.
            hostmask = nick
            if "$" not in nick:
                hostmask += "$" + destchan
        else:
            try:
                # Try to do a whois. if successful, we ban by username, not
                # nick, since sometimes bouncy people rejoin with underscores
                # or whatever
                whois_results = (yield self.transport.issue_request("irc.whois", nick))
                whoisuser = whois_results['RPL_WHOISUSER']
                nick = whoisuser[0]
                username = whoisuser[1]
                #hostname = whoisuser[2]
                hostmask = "*!{0}@*".format(username)
            except ircutil.NoSuchNick:
                # just ban by nick
                hostmask = "{0}!*@*".format(nick)

            hostmask += "$" + destchan

        ban_d  = self._do_moderequest(channel, 'b', hostmask, 60*60*2)
        kick_d = self.transport.issue_request("ircop.kick",
                channel=channel,
                target=nick,
                reason="Redirected to {0}".format(destchan),
                )
        try:
            yield ban_d
            yield kick_d
        except ircop.OpFailed as e:
            event.reply(str(e))
        event.reply("Redirected {0} to {1} for 2 hours".format(nick, destchan))

    @require_channel
    @defer.inlineCallbacks
    def ban(self, event, match):
        groupdict = match.groupdict()
        # nick here could be a nick, a hostmask (with possible wildcards), or
        # an extban.
        target = groupdict['nick']
        duration = groupdict['timestr']
        channel = event.channel
        reason = "Banned by " + event.user.split("!")[0]

        if duration:
            try:
                duration = parse_time(duration)
            except ValueError as e:
                # Not parsable as a time? Use the string as a reason and use
                # the default time instead.
                reason = duration
                duration = None

        if not duration and self.config['defaulttime']:
            duration = self.config['defaulttime']

        if "@" not in target and "!" not in target and "$" not in target:
            # Just a nick was given. Do a kick on the target as given, but
            # still lookup the mask and do a ban on that.
            nick = target
            do_kick = True
        else:
            do_kick = False

        # Unconditionally pass the target through _nick_to_hostmask. If target
        # is a nick, we get a hostmask out. If target is already a mask of some
        # sort or an extban, we get the result passed through, but with some
        # syntax checking.
        try:
            hostmask = (yield self._nick_to_hostmask(target))
        except ircutil.NoSuchNick:
            event.reply("There is no user by that nick on the network. "
                        "Try {0}!*@* to ban anyone with that nick, or specify a full hostmask.".format(
                        nick,
                        ))
            return
        except ircutil.WhoisTimedout:
            event.reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
            return
        except ValueError:
            event.reply("Malformed hostmask. Hostmask syntax is <nick>!<username>@<host>", direct=True, notice=True)
            return

        log.msg("issuing ban for {0}".format(hostmask))
        ban_d = self._do_moderequest(channel, 'b', hostmask, duration)

        if do_kick:
            log.msg("issuing kick for {0} to go with the ban".format(nick))
            kick_d = self.transport.issue_request("ircop.kick",
                    channel=channel,
                    target=nick,
                    reason=reason,
                    )
        try:
            yield ban_d
            if do_kick:
                yield kick_d
        except ircop.OpFailed as e:
            event.reply(str(e))

        if duration:
            event.reply("Will unban {} {}".format(
                    hostmask,
                    # bug: pretty.date doesn't accept floats as timestamps
                    pretty.date(int(time.time()+duration))
                ), direct=True, notice=True)

    def _do_moderequest(self, channel, mode, hostmask, duration):
        """Sets a ban or quiet on the given hostmask in a channel for an
        optional duration. If duration is None, we will not set it back after
        any length of time. (a default time should be set by the caller)

        This method returns a deferred that fires when the mode request has
        been completed. It may error with an ircop.OpFailed exception

        """
        if duration:
            log.msg("+%s for %s in %s %s" % (mode, hostmask, channel, duration))
        else:
            log.msg("+%s for %s in %s" % (mode, hostmask, channel, ))

        # Don't yield for the mode request. We want to return control to the
        # caller as soon as possible, and errors still get sent to the
        # passed-in reply() function
        req = self.transport.issue_request("ircop.{0}".format(
                    {"b":"ban","q":"quiet"}[mode]
                    ),
                    channel=channel,
                    target=hostmask,
                    )
        if duration:
            # only set the timer if the request succeeds
            def s(a):
                self._set_timer(duration, hostmask, channel, "-"+mode)
                return a
            req.addCallback(s)

        return req

    @require_channel
    @defer.inlineCallbacks
    def unquiet(self, event, match):
        groupdict = match.groupdict()
        target = groupdict['nick']
        delay = groupdict['timestr']
        channel = event.channel

        if delay:
            try:
                delay = parse_time(delay)
            except ValueError as e:
                event.reply(str(e))
                return

        try:
            hostmask = (yield self._nick_to_hostmask(target))
        except ircutil.NoSuchNick:
            event.reply("There is no user by that nick on the network. "
                        "Try specifying a full hostmask. Use “/mode +q” to see the channel quiet list".format(
                        target,
                        ))
            return
        except ValueError:
            event.reply("Malformed hostmask. Hostmask syntax is <nick>!<username>@<host>", direct=True, notice=True)
            return

        try:
            yield self._do_modederequest(channel, 'q', hostmask, delay)
        except ircop.OpFailed as e:
            event.reply(str(e))
        if delay:
            event.reply("Unquieting {}".format(pretty.date(int(time.time()+delay))))

    @require_channel
    @defer.inlineCallbacks
    def unban(self, event, match):
        groupdict = match.groupdict()
        target = groupdict['nick']
        delay = groupdict['timestr']
        channel = event.channel

        if delay:
            try:
                delay = parse_time(delay)
            except ValueError as e:
                event.reply(str(e))
                return

        try:
            hostmask = (yield self._nick_to_hostmask(target))
        except ircutil.NoSuchNick:
            event.reply("There is no user by that nick on the network. "
                        "Try specifying a full hostmask. Use “/mode +b” to see the channel ban list".format(
                        target,
                        ))
            return
        except ValueError:
            event.reply("Malformed hostmask. Hostmask syntax is <nick>!<username>@<host>", direct=True, notice=True)
            return

        try:
            yield self._do_modederequest(channel, 'b', hostmask, delay)
        except ircop.OpFailed as e:
            event.reply(str(e))
        if delay:
            event.reply("Unbanning {}".format(pretty.date(int(time.time()+delay))))

    def _do_modederequest(self, channel, mode, hostmask, delay):
        """Note the *de* in _do_mode*de*request.
        For info see _do_moderequest()
        
        Difference between this method and _do_moderequest: do_moderequest
        takes a *duration*; it always applies the requested mode and then
        un-does that after the duration.

        This method on the other hand takes a *delay*; it always applies the
        requested mode but may wait to do so.
        """
        if delay:
            self._set_timer(delay, hostmask, channel, "-"+mode)
            return defer.succeed(None)

        log.msg("-%s for %s in %s" % (mode, hostmask, channel))
        return self.transport.issue_request("ircop.{0}".format(
                    {"b":"unban","q":"unquiet"}[mode]
                    ),
                    channel=channel,
                    target=hostmask,
                    )

    @require_channel
    @defer.inlineCallbacks
    def holdop(self, event, match):
        channel = event.channel
        seconds = parse_time(match.groupdict()['time'])

        try:
            yield self.transport.issue_request("ircop.become_op", channel, seconds)
        except ircop.OpFailed as e:
            event.reply(str(e))

    @require_channel
    @defer.inlineCallbacks
    def mode(self, event, match):
        channel = event.channel
        gd = match.groupdict()
        mode = gd['mode']
        param = gd['param']
        timespec = gd['timespec']

        if timespec:
            try:
                time_to_wait = parse_time(timespec)
            except ValueError as e:
                event.reply(e)

            if timespec.startswith("in") or timespec.startswith("at"):
                # 'in' or 'at' indicate this is when we want to do the mode. as
                # opposed to 'do the mode until/for <time> and then revert it'
                self._set_timer(time_to_wait, param, channel, mode)
                event.reply(u"Doing a {0}{1} {2}".format(
                    mode, u" "+param if param else "",
                    pretty.date(int(time.time()+time_to_wait))
                    ))
                return

        try:
            yield self.transport.issue_request("ircop.mode", channel, mode, param)
        except (ircop.OpFailed, ValueError) as e:
            event.reply(str(e))
            return

        if timespec:
            reversemode = {"-":"+","+":"-"}[mode[0]] + mode[1]
            self._set_timer(time_to_wait, param, channel, reversemode)
            event.reply(u"Doing a {0}{1} {2}".format(
                reversemode, u" "+param if param else "",
                pretty.date(int(time.time()+time_to_wait))
                ), direct=True, notice=True)

    @require_channel
    @defer.inlineCallbacks
    def moderatedmode(self, event, match):
        channel = event.channel
        nick = event.user.split("!",1)[0]

        if "m" not in (yield self.transport.issue_request("irc.chanmode", channel))[0]:
            log.msg("Setting moderated mode on {0}".format(channel))
            event.reply("Channel is now in LOCKDOWN mode. Only OPs and voiced users may speak.",direct=True, notice=True)
            event.reply("Helpful tips for channel problems:",direct=True, notice=True)
            event.reply("Use !m again to undo and revert to normal", direct=True, notice=True)
            event.reply("To quiet all unregistered users: !quiet $~a", direct=True, notice=True)
            event.reply("To prevent unregistered users from joining: !mode +r", direct=True, notice=True)
            req1 = self.transport.issue_request("ircop.mode", channel, "+m")
            req2 = self.transport.issue_request("ircop.op", channel, nick)
        else:
            log.msg("Un-setting moderated mode on {0}".format(channel))
            req1 = self.transport.issue_request("ircop.mode", channel, "-m")
            req2 = self.transport.issue_request("ircop.deop", channel, nick)

        try:
            yield req1
            yield req2
        except ircop.OpFailed as e:
            event.reply(str(e))

    @defer.inlineCallbacks
    def on_request_ircadmin_timedquiet(self, channel, target, duration):
        """Puts in a request for a timed quiet. user is interpreted as either a
        nick or a hostmask. If it is a nick and the user is not on the network,
        an ircutil.NoSuchNick is raised.

        duration is either an integer, or a string. If it is a string, it is
        parsed for the time. If no time can be parsed, a ValueError is raised.

        If there was a problem acquiring OP to complete this function, an
        ircop.OpFailed is raised.

        """
        if not isinstance(duration, int):
            duration = parse_time(duration)

        hostmask = (yield self._nick_to_hostmask(target))

        yield self._do_moderequest(channel, 'q', hostmask, duration)

    def bans(self, event, match):
        """A user has issued the bans command, a query to see who is banned and
        for how long.
        
        """
        reply = event.reply

        groupdict =  match.groupdict()
        mask = groupdict['mask']
        if not mask:
            mask = "all"

        LaterItem = namedtuple("LaterItem", ["channel", "time", "mask", "mode"])

        # Filter out all but bans and quiets. also put in a list of namedtuples
        # so it's easier to work with
        laters = [
                LaterItem(
                    time=int(x[0]),
                    mask=x[1],
                    channel=x[2],
                    mode=x[3])
                for x in self.config['laters'] if x[3][1] in "bq"
        ]

        # Sort in the order of the namedtuple parameters: by channel and then by time
        laters.sort()

        if mask != "all":
            laters = [x for x in laters if x.mask == mask]
            if not laters:
                reply("No pending events for {0}".format(mask))
                return


        for later in laters:
            when = pretty.date(later.time)
            reply("In {channel} setting {mode} {mask} {when}".format(
                channel=later.channel,
                mode=later.mode,
                mask=later.mask,
                when=when))
        


class IRCTopic(CommandPluginSuperclass):
    """Topic manipulation commands.

    """
    REQUIRES=["ircutil.ChanMode", "ircop.OpProvider"]
    def start(self):
        super(IRCTopic, self).start()

        # Topic commands
        topicgroup = self.install_cmdgroup(
                grpname="topic",
                prefix=None,
                permission="irc.op.topic",
                helptext="Topic manipulation commands",
                )

        topicgroup.install_command(
                cmdname="append",
                cmdmatch="append|push|add",
                cmdusage="<text>",
                argmatch="(?P<text>.+)$",
                permission=None, # Inherits permissions from the group
                callback=self.topicappend,
                helptext="Appends text to the end of the channel topic",
                )
        topicgroup.install_command(
                cmdname="insert",
                cmdmatch=None,
                cmdusage="<pos> <text>",
                argmatch=r"(?P<pos>-?\d+) (?P<text>.+)$",
                callback=self.topicinsert,
                helptext="Inserts text into the topic at the given position",
                )

        topicgroup.install_command(
                cmdname="replace",
                cmdmatch="set|replace",
                cmdusage="<pos> <text>",
                argmatch=r"(?P<pos>-?\d+) (?P<text>.+)$",
                callback=self.topicreplace,
                helptext="Replaces the given section with the given text",
                )

        topicgroup.install_command(
                cmdname="remove",
                cmdmatch=None,
                cmdusage="<pos>",
                argmatch=r"(?P<pos>-?\d+)$",
                callback=self.topicremove,
                helptext="Removes the pos'th topic selection",
                )
        topicgroup.install_command(
                cmdname="pop",
                callback=self.topicpop,
                helptext="Removes the last topic item",
                )

        topicgroup.install_command(
                cmdname="undo",
                callback=self.topic_undo,
                helptext="Reverts the topic to the last known channel topic",
                )

        # Maps channel names to the last so many topics
        # (The top most item on the stack should be the current topic. But the
        # handlers should handle the case that the stack is empty!)
        self.topic_stack = defaultdict(lambda: deque(maxlen=10))
        self.listen_for_event("irc.on_topic_updated")
        # set of deferreds waiting for the current topic response in a channel
        self.topic_waiters = defaultdict(set)

    ### Topic methods
    def on_event_irc_on_topic_updated(self, event):
        channel = event.channel
        newtopic = event.newtopic
        oldtopic = None
        try:
            oldtopic = self.topic_stack[channel][-1]
        except IndexError:
            pass
        if newtopic != oldtopic:
            self.topic_stack[event.channel].append(newtopic)
            log.msg("Topic updated in %s. Now I know about %s past topics (including this one)" % (event.channel,
                len(self.topic_stack[event.channel])))

        for d in self.topic_waiters.pop(channel, set()):
            d.callback(newtopic)

    def _get_current_topic(self, channel):
        """Returns a deferred object with the current topic.
        The callback will be called with the channel topic once it's known. The
        errback will be called if the topic cannot be determined

        """
        topic_stack = self.topic_stack[channel]
        if topic_stack:
            return defer.succeed(topic_stack[-1])

        # We need to ask what the topic is. Go ahead and send off that event.
        log.msg("Sending a request for the current topic since I don't know it")
        topicrequest = Event("irc.do_topic",
                channel=channel)
        self.transport.send_event(topicrequest)

        # Now set up a deferred object that will be called when the topic comes in
        deferreds = self.topic_waiters[channel]
        new_d = defer.Deferred()

        if not deferreds:
            # No current deferreds in the set. Set up a failure callback
            def failure(_):
                log.msg("Topic request timed out. Calling errbacks")
                for d in self.topic_waiters.pop(channel, set()):
                    d.errback()
            c = reactor.callLater(10, failure)
            # Set a success callback to cancel the failure timeout
            def success(result):
                log.msg("Topic result came in")
                c.cancel()
                return result
            new_d.addCallback(success)

        deferreds.add(new_d)
        return new_d

    @require_channel
    def topicappend(self, event, match):
        channel = event.channel
        def callback(currenttopic):
            topic_parts = [x.strip() for x in currenttopic.strip().split("|")]
            topic_parts.append(match.groupdict()['text'])
            self._set_topic(channel, " | ".join(topic_parts), event.reply)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicinsert(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])
            text = gd['text']

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            topic_parts.insert(pos, text)

            newtopic = " | ".join(topic_parts)
            self._set_topic(channel, newtopic, event.reply)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicreplace(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])
            text = gd['text']

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                topic_parts[pos] = text
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return


            newtopic = " | ".join(topic_parts)
            self._set_topic(channel, newtopic, event.reply)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicremove(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            gd = match.groupdict()
            pos = int(gd['pos'])

            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                del topic_parts[pos]
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return

            newtopic = " | ".join(topic_parts)
            self._set_topic(channel, newtopic, event.reply)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topicpop(self, event, match):
        channel = event.channel

        def callback(currenttopic):
            topic_parts = [x.strip() for x in currenttopic.split("|")]
            try:
                del topic_parts[-1]
            except IndexError:
                event.reply("There are only %s topic parts. Remember indexes start at 0" % len(topic_parts))
                return

            newtopic = " | ".join(topic_parts)
            self._set_topic(channel, newtopic, event.reply)
        self._get_current_topic(channel).addCallbacks(callback,
                lambda _: event.reply("Could not determine current topic"))

    @require_channel
    def topic_undo(self, event, match):
        channel = event.channel

        topicstack = self.topic_stack[channel]
        if len(topicstack) < 2:
            event.reply("I don't know what the topic used to be. Cannot undo =(")
            return
        # Pop the current item off
        topicstack.pop()
        # Now pop the next item, which will be our new topic
        newtopic = topicstack.pop()

        self._set_topic(channel, newtopic, event.reply)

    def _set_topic(self, channel, topic, reply):
        try:
            self.transport.issue_request("ircop.topic", channel, topic)
        except ircop.OpFailed as e:
            reply("Channel is +t and I can't acquire op! Reason: {0}".format(e))
