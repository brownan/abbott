from collections import defaultdict, deque
from functools import wraps
import glob, os.path
import heapq
import time
import re
import random

from twisted.internet import reactor
from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass
from ..transport import Event
from . import ircutil

def require_channel(func):
    """Wraps command callbacks and requires them to be in response to a channel
    message, not a private message directed to the bot.

    """
    @wraps(func)
    def newfunc(self, event, match):
        if event.direct:
            event.reply("Hey, you can't do that in here!")
        else:
            return func(self, event, match)
    return newfunc

class OpError(Exception):
    pass
class OpTimedOut(OpError):
    pass
class NoOpMethod(OpError):
    pass

class IRCOpProvider(CommandPluginSuperclass):
    """This plugin provides three things: it provides the requests
    ircadmin.op and ircadmin.deop to grant op and deop requests for arbitrary
    nicks on arbitrary channels, and the request ircadmin.opself, which
    returns a deferred that fires when the bot gains or has OP.
    
    This plugin can be configured to grant op in one of two ways, on a
    per-channel basis. It can also be configured to hold op for a specified
    amount of time and then relinquish it.

    The ircadmin.opself request may errback with an OpError exception. The op
    and deop requests may error with a NoOpMethod exception if op is reqeusted
    but no method of gaining op is defined (and we are not op so it can't be
    done directly)

    """
    def start(self):
        super(IRCOpProvider, self).start()

        # Things this plugin provides to other plugins
        self.provides_request("ircadmin.op")
        self.provides_request("ircadmin.deop")
        self.provides_request("ircadmin.opself")

        # Things this plugin listens to from other plugins
        self.listen_for_event("irc.on_mode_change")
        self.listen_for_event("irc.on_join")

        # Maps channel names to a boolean indicating whether we currently hold
        # op there or not
        self.have_op = {}

        # Maps channel names to a set of (deferred, IDelayedCall) tuples where
        # deferred is to be called when we get op, and the delayed call will
        # call the errback of the deferred after a timeout has occurred.
        self.waiting_for_op = defaultdict(set)

        # Maps channel names to timeout calls
        # (twisted.internet.interfaces.IDelayedCall) to relinquish op, if we
        # have it
        self.op_timeout_event = {}

        # Initialize the conifig directive
        if "optimeout" not in self.config or \
                not isinstance(self.config['optimeout'], dict):
            self.config['optimeout'] = {}
            self.config.save()

        self.install_command(
                cmdname="optimeout",
                argmatch=r"(?P<timeout>-?\d+)( (?P<channel>[^ ]+))?$",
                callback=self.set_op_timeout,
                cmdusage="<timeout> [channel]",
                helptext="Sets how long I'll keep OP before I give it up. -1 for forever",
                permission="irc.op",
                )

        self.install_command(
                cmdname="opmethod",
                argmatch=r"(?P<mode>\w+)( (?P<channel>[^ ]+))?$",
                callback=self.set_op_mode,
                cmdusage="<mode> [channel]",
                helptext="Sets the op mode. One of: 'none', 'weechat', or 'chanserv'",
                permission="irc.op",
                )

        # Op methods. This dict maps channel names to a string
        # empty string or "none": no op method. The bot does not have a way
        # to op itself if it is not opped already.
        # "weechat" - uses a weechat fifo connector to message chanserv
        # "chanserv" - messages chanserv
        if not "opmethod" in self.config:
            self.config['opmethod'] = {}
            self.config.save()

    def stop(self):
        # Cancel all pending timers
        # Let items in waiting_for_op expire on their own
        
        for delayedcall in self.op_timeout_event.itervalues():
            delayedcall.cancel()

    @defer.inlineCallbacks
    def _has_op(self, channel):
        """Returns a deferred that fires with True if we have op on the
        channel, or False if we don't or it can't be determined.

        If there is no entry in have_op, we do a NAMES lookup on the channel to
        try and determine if we are OP or not.

        """
        try:
            defer.returnValue( self.have_op[channel] )
        except KeyError:
            # determine if we have OP
            names_list = (yield self.transport.issue_request("irc.names",channel))

            nick = (yield self.transport.issue_request("irc.getnick"))

            has_op = "@"+nick in names_list
            self.have_op[channel] = has_op
            defer.returnValue(has_op)


    def _set_op_timeout(self, channel):
        """Sets the op timeout for the given channel. If there is not currently
        an op timeout, makes one. If there is currently a timeout set, resets
        the timer.

        """
        try:
            timeout = self.config['optimeout'][channel]
        except KeyError:
            self.config['optimeout'][channel] = 60
            self.config.save()
            timeout = 60

        try:
            method = self.config['opmethod'][channel]
        except KeyError:
            # no op method, we need to keep op
            timeout = 0
        else:
            if method not in ('weechat', 'chanserv'):
                timeout = 0

        delayedcall = self.op_timeout_event.get(channel, None)
        if timeout < 1:
            # This channel has no timeout set. Remove one if there is one set.
            if delayedcall:
                delayedcall.cancel()

        else:
            # Set a timeout if not set, or reset the timer otherwise
            if delayedcall:
                delayedcall.reset(timeout)
            else:
                # Set a callback to relinquish op
                @defer.inlineCallbacks
                def relinquish():
                    mynick = (yield self.transport.issue_request("irc.getnick"))
                    event = Event("irc.do_mode",
                            channel=channel,
                            set=False,
                            modes="o",
                            user=mynick,
                            )
                    self.transport.send_event(event)
                    log.msg("Relinquishing op")
                    # Go ahead and set this to false here, to avoid race
                    # conditions if a plugin requests op right now after we've
                    # sent the -o mode but before it's granted.
                    self.have_op[channel] = False
                    
                    del self.op_timeout_event[channel]
                self.op_timeout_event[channel] = reactor.callLater(timeout, relinquish)

    @defer.inlineCallbacks
    def on_request_ircadmin_opself(self, channel):
        """This request will cause the bot to acquire OP on the channel
        specified, and set or reset the timer for how long to keep op before
        relinquishing it. The bot will not relinquish OP if there is no
        specified way for it to acquire it at will. This request's returned
        deferred will fire once OP is acquired, with the parameter specifying
        the channel. The deferred will errback in the event that OP was not
        required, with one of the OpError errors, either timed out or no method
        configured.

        """
        if (yield self._has_op(channel)):
            # Already has op. Reset the timeout timer and return success
            self._set_op_timeout(channel)
            defer.returnValue(channel)

        else:
            nick = (yield self.transport.issue_request("irc.getnick"))

            # Submit an OP request for ourselves
            self._do_op(channel, nick, True)

            # Now create a new deferred object
            d = defer.Deferred()

            # Create a timeout that will cancel this call
            def timedout():
                # Remove the deferred from the waiters. timeout is from the
                # enclosing scope created below
                self.waiting_for_op[channel].remove((d, timeout))
                # call the errback
                d.errback(OpTimedOut("Op request timed out"))
            timeout = reactor.callLater(20, timedout)

            # This puts the deferred in the appropriate place where it'll be
            # called when we get op.
            self.waiting_for_op[channel].add((d, timeout))

            # Wait for the above deferred to be called, then return the
            # result
            defer.returnValue((yield d))

    @defer.inlineCallbacks
    def on_event_irc_on_mode_change(self, event):
        """Called when we observe a mode change. Check to see if it was an op
        operation on ourselves

        """
        mynick = (yield self.transport.issue_request("irc.getnick"))

        if (event.set == True and "o" == event.mode and
                event.arg == mynick):
            # Op acquired. Make a note of it
            self.have_op[event.channel] = True

            # Now that we have op, call deferreds on anything that was waiting
            log.msg("I was given op. Calling op callbacks on %s" % event.channel)
            for deferred, timeout in self.waiting_for_op.pop(event.channel, set()):
                timeout.cancel()
                deferred.callback(event.channel)

            # Now that we have op, set a timeout to relinquish it
            self._set_op_timeout(event.channel)

        elif (event.set == False and "o" == event.mode and
                event.arg == mynick):
            # Op gone
            log.msg("I am no longer OP on %s" % event.channel)
            self.have_op[event.channel] = False
            try:
                timeoutevent = self.op_timeout_event[event.channel]
            except KeyError:
                pass
            else:
                timeoutevent.cancel()
                del self.op_timeout_event[event.channel]

    def on_event_irc_on_join(self, event):
        """If we find ourself joining a channel that we thought we had op, then
        we actually don't anymore. This happens on disconnects/reconnects or on
        a manual part/join. Anything that doesn't involve this plugin being
        restarted.

        """
        self.have_op[event.channel] = False
        try:
            timeoutevent = self.op_timeout_event[event.channel]
        except KeyError:
            pass
        else:
            timeoutevent.cancel()
            del self.op_timeout_event[event.channel]

    def set_op_timeout(self, event, match):
        """Configures the op timeout for a channel. This is the event handler
        for the command, not to be confused by the other method of a similar
        name that sets a timeout call to relinquish op. This one just
        configures the timeout value.

        """
        gd = match.groupdict()
        channel = gd['channel']
        timeout = int(gd['timeout'])

        if not channel:
            if event.direct:
                event.reply("on what channel?")
                return
            channel = event.channel

        self.config['optimeout'][channel] = timeout
        self.config.save()

        timeoutevent = self.op_timeout_event.get(channel, None)
        if timeout < 1:
            event.reply("Done. I'll never give up my op")
            if timeoutevent:
                timeoutevent.cancel()
                del self.op_timeout_event[channel]
        else:
            event.reply("Done. I'll hold op for %s seconds after I get it" % timeout)
            if timeoutevent:
                timeoutevent.reset(timeout)
            elif self.have_op.get(channel):
                self._set_op_timeout(channel)

    def set_op_mode(self, event, match):
        gd = match.groupdict()
        channel = gd['channel']
        newmode = gd['mode']

        if not channel:
            if event.direct:
                event.reply("on what channel?")
                return
            channel = event.channel

        self.config['opmethod'][channel] = newmode
        self.config.save()
        event.reply("Okay set.")

    ### Handlers for op and deop requests for arbitrary users from other
    ### plugins

    @defer.inlineCallbacks
    def on_request_ircadmin_op(self, channel, nick):
        if (yield self._has_op(channel)):
            self._set_op_timeout(channel)
            self.transport.send_event(Event("irc.do_mode",
                channel=channel,
                set=True,
                modes='o',
                user=nick,
                ))
        else:
            self._do_op(channel, nick, True)
        return
    @defer.inlineCallbacks
    def on_request_ircadmin_deop(self, channel, nick):
        if (yield self._has_op(channel)):
            self._set_op_timeout(channel)
            self.transport.send_event(Event("irc.do_mode",
                channel=channel,
                set=False,
                modes='o',
                user=nick,
                ))
        else:
            self._do_op(channel, nick, False)
        return

    def _do_op(self, channel, nick, opset):
        """Issues an op request with a method appropriate for the given
        channel. If no op method is defined, raises a NoOpMethod exception

        """
        method = self.config['opmethod'].get(channel, None)
        if method == "weechat":
            log.msg("Sending OP request to chanserv via weechat...")
            path = glob.glob(os.path.expanduser("~/.weechat/weechat_fifo_*"))[0]
            with open(path, 'w') as out:
                out.write("irc.server.freenode */msg ChanServ {op} {channel} {nick}\n".format(
                    op="op" if opset else "deop",
                    channel=channel,
                    nick=nick,
                    ))

        elif method == "chanserv":
            log.msg("Sending OP request to chanserv")
            self.transport.send_event(Event("irc.do_msg",
                user="ChanServ",
                message="{op} {channel} {nick}",
                ))

        else:
            raise NoOpMethod("I have no way to acquire or give OP on %s" % channel)
        
duration_match = r"(?P<duration>\d+[dhmsw])"
def parse_time(timestr):
    duration = 0
    multipliers = {
            's': 1,
            'm': 60,
            'h': 60*60,
            'd': 60*60*24,
            'w': 60*60*24*7,
            }
    for component in re.findall(duration_match, timestr):
        time, unit = component[:-1], component[-1]
        duration += int(time) * multipliers[unit]

    return duration

class IRCAdmin(CommandPluginSuperclass):
    """Provides a command interface to IRC operator tasks. Uses the above
    opprovider plugin as an interface to acquire op and do op related things.

    """

    def __init__(self, *args):
        self.started = False

        # This dictionary maps tuples of (hostmask, channel, mode) to twisted timer
        # objects. When the timer fires, the mode is unset on the given channel
        # for the given hostmask
        self.later_timers = {}


        super(IRCAdmin, self).__init__(*args)

    def reload(self):
        super(IRCAdmin, self).reload()

        if "laters" not in self.config:
            self.config['laters'] = []

        if self.started:
            self._set_all_timers()
        
    def _set_all_timers(self):
        """Reads from the config and syncs the twisted timers with that"""

        for timer in self.later_timers.itervalues():
            timer.cancel()

        for activatetime, hostmask, channel, mode in self.config['laters']:
            self._set_timer(activatetime - time.time(), hostmask, channel, mode)


    def _set_timer(self, delay, hostmask, channel, mode):
        """In delay seconds, issue a -mode request for hostmask on channel
        
        mode is either 'q' or 'b'
        
        """
        # First, cancel any existing timers and remove any existing saved
        # laters from the config
        if (hostmask, channel, mode) in self.later_timers:
            timer = self.later_timers.pop((hostmask, channel, mode))
            timer.cancel()

        # Filter out any events that match this one from the persistent config
        self.config['laters'] = [item for item in self.config['laters']
                if not (item[1] == hostmask and
                       item[2] == channel and
                       item[3] == mode
                       )]

        # This function will be run later
        def do_later():
            log.msg("timed request: -%s for %s in %s" % (mode, hostmask, channel))
            # First, take this item out of the mapping
            del self.later_timers[(hostmask, channel, mode)]

            # And the persistent config
            self.config['laters'] = [item for item in self.config['laters']
                    if not (item[1] == hostmask and
                           item[2] == channel and
                           item[3] == mode
                           )]
            self.config.save()

            # prepare an error reply function
            def reply(s):
                s = "I was about to un-{0} {1}, but {2}".format(
                        {'q':'quiet','b':'ban'}[mode],
                        hostmask,
                        s,
                        )
                self.transport.send_event(Event("irc.do_msg",
                    user=channel,
                    message=s,
                    ))
            # Now send the event
            self._send_as_op(
                    Event(
                        "irc.do_mode",
                        channel=channel,
                        set=False,
                        modes=mode,
                        user=hostmask
                        ),
                    reply,
                    )

        # Now submit the do_later() function to twisted to call it later
        timer = reactor.callLater(max(1,delay), do_later)

        log.msg("Setting -{0} on {1} in {2} in {3} seconds".format(
            mode,
            hostmask,
            channel,
            max(1,delay),
            ))

        # and file this timer away:
        self.later_timers[(hostmask, channel, mode)] = timer

        # Save to the persistent config
        self.config['laters'].append(
                (time.time()+delay, hostmask, channel, mode)
                )
        self.config.save()
        
    def on_event_irc_on_mode_change(self, event):
        if event.set == False:
            mode = event.mode
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

        for timer in self.later_timers.itervalues():
            timer.cancel()
        
    def start(self):
        super(IRCAdmin, self).start()

        self.started = True
        self._set_all_timers()

        self.listen_for_event("irc.on_mode_change")

        # kick command
        self.install_command(
                cmdname="kick",
                cmdmatch="kick|KICK",
                cmdusage="<nickname> [reason]",
                argmatch = "(?P<nick>[^ ]+)( (?P<reason>.*))?$",
                permission="irc.op.kick",
                prefix=".",
                callback=self.kick,
                deniedcallback=self.kickself,
                helptext="Kicks a user from the current channel")

        # Op commands
        self.install_command(
                cmdname="op",
                prefix=".",
                cmdusage="[nick]",
                argmatch="(?P<nick>[^ ]+)?",
                permission="irc.op.op",
                callback=self.give_op,
                helptext="Gives op to the specified user",
                )
        self.install_command(
                cmdname="deop",
                prefix=".",
                cmdusage="[nick]",
                argmatch="(?P<nick>[^ ]+)?",
                permission="irc.op.op",
                callback=self.take_op,
                helptext="Takes op from the specified user",
                )

        # voice commands
        self.install_command(
                cmdname="voice",
                cmdmatch="voice|VOICE|hat",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.voice,
                helptext="Grants a user voice in the current channel"
                )

        self.install_command(
                cmdname="devoice",
                cmdmatch="devoice|DEVOICE|dehat|unhat",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.devoice,
                helptext="Revokes a user's voice in the current channel"
                )

        # Quiet commands
        self.install_command(
                cmdname="quiet",
                cmdmatch="quiet|QUIET|mute",
                cmdusage="<nick or hostmask> [for <duration>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:for )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.quiet",
                callback=self.quiet,
                deniedcallback=self.quietself,
                helptext="Quiets a user."
                )

        self.install_command(
                cmdname="unquiet",
                cmdmatch="unquiet|UNQUIET|unmute",
                cmdusage="<nick or hostmask> [in <delay>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:in )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.quiet",
                callback=self.unquiet,
                helptext="Un-quiets a user"
                )

        # Ban commands
        self.install_command(
                cmdname="ban",
                cmdmatch="ban|BAN",
                cmdusage="<nick or hostmask> [for <duration>] [reason]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:for )?{0}+)?(?: (?P<reason>.+))?$".format(duration_match),
                prefix=".",
                permission="irc.op.ban",
                callback=self.ban,
                helptext="Bans a user."
                )

        self.install_command(
                cmdname="unban",
                cmdmatch="unban|UNBAN",
                cmdusage="<nick or hostmask> [in <delay>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:in )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.ban",
                callback=self.unban,
                helptext="Un-bans a user"
                )

    @defer.inlineCallbacks
    def _send_as_op(self, event, reply=lambda s: None):
        """Issues an ircadmin.opself request, then sends the event. If the
        opself fails, sends an error to the reply function provided

        """
        try:
            yield self.transport.issue_request("ircadmin.opself", event.channel)
        except OpTimedOut:
            reply("I could not become OP. Check the error log, configuration, etc.")
            defer.returnValue(False)
        except NoOpMethod:
            reply("I can't do that in %s, I don't have OP and have no way to acquire it!" % event.channel)
            defer.returnValue(False)
        else:
            self.transport.send_event(event)
            defer.returnValue(True)

    @defer.inlineCallbacks
    def _nick_to_hostmask(self, nick):
        """Takes a nick or a hostmask and returns a parameter suitable for the
        +b or +q modes. If the items given looks like a hostmask (contains a !
        and a @) then it is returned. If the item is an extban (starts with a
        $), then that is returned. Otherwise, it is assumed the parameter is a
        nickname and a whois is performed and the hostmask is returned with the
        first two fields wildcarded.

        This methed is intended to allow bans and quiets to match any nick!user
        combination by banning/quieting all users from that host.

        If no such user is found, an ircutil.NoSuchNick is raised. If the whois
        fails, an ircutil.WhoisTimedout is raised.

        Returnes a deferred that fires with the answer.

        """
        if ("!" in nick and "@" in nick) or (nick.startswith("$")):
            defer.returnValue(nick)
            return

        whois_results = (yield self.transport.issue_request("irc.whois", nick))

        whoisuser = whois_results['RPL_WHOISUSER']

        mask = "{0}!{1}@{2}".format(
                '*',
                '*',
                whoisuser[2],
                )

        defer.returnValue(mask)

    @require_channel
    def kick(self, event, match):
        """A user has issued the kick command. Our job here is to acquire OP
        for this channel and issue a kick event

        """
        groupdict = match.groupdict()
        nick = groupdict['nick']
        reason = groupdict.get("reason", None)
        channel = event.channel

        kickevent = Event("irc.do_kick", channel=channel,
                user=nick, reason=reason)
        self._send_as_op(kickevent, event.reply)

    @require_channel
    def kickself(self, event, match):
        targetnick = match.groupdict()['nick']
        requestor = event.user.split("!")[0]

        if targetnick == requestor:
            kickevent = Event("irc.do_kick", channel=event.channel,
                    user=requestor,
                    reason="okay, you asked for it",
                    )
        elif random.randint(1,4) == 4:
            kickevent = Event("irc.do_kick", channel=event.channel,
                    user=requestor,
                    reason="woops, my bad!",
                    )
        else:
            kickevent = None

        if kickevent:
            def r(s):
                event.reply("naa, I don't feel like it right now", userprefix=False)
                log.msg(s)
            self._send_as_op(kickevent,
                    r,
                    )
            return True

    @require_channel
    def voice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        voiceevent = Event("irc.do_mode",
                channel=channel,
                set=True,
                modes="v",
                user=nick,
                )
        log.msg("Voicing %s in %s" % (nick, channel))
        self._send_as_op(voiceevent, event.reply)

    @require_channel
    def devoice(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel

        voiceevent = Event("irc.do_mode",
                channel=channel,
                set=False,
                modes="v",
                user=nick,
                )
        log.msg("De-voicing %s in %s" % (nick, channel))
        self._send_as_op(voiceevent, event.reply)

    @require_channel
    @defer.inlineCallbacks
    def give_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        
        log.msg("Opping %s in %s" % (nick, channel))
        try:
            yield self.transport.issue_request("ircadmin.op",channel,nick)
        except NoOpMethod:
            event.reply("I cannot issue OP in this channel")

    @require_channel
    @defer.inlineCallbacks
    def take_op(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        if not nick:
            nick = event.user.split("!",1)[0]
        channel = event.channel
        log.msg("Deopping %s in %s" % (nick, channel))
        try:
            yield self.transport.issue_request("ircadmin.deop",channel,nick)
        except NoOpMethod:
            event.reply("I cannot issue OP in this channel")

    @require_channel
    def quiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel

        self._do_moderequest('q', event.reply, nick, duration, channel)

    @require_channel
    def quietself(self, event, match):
        groupdict = match.groupdict()
        nick = event.user.split("!")[0]
        if random.randint(1,3) == 3 or nick == groupdict['nick']:
            duration = 10
            channel = event.channel
            def r(s):
                event.reply("naa, I don't feel like it right now", userprefix=False)
                log.msg(s)
            self._do_moderequest("q",
                    r,
                    nick,
                    duration,
                    channel,
                    )
            if nick != groupdict['nick']:
                reactor.callLater(7,
                        event.reply,
                        "Woops, my bad!",
                        )
            return True

    @require_channel
    @defer.inlineCallbacks
    def ban(self, event, match):
        groupdict = match.groupdict()
        # nick here could be a nick, a hostmask (with possible wildcards), or
        # an extban
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        reason = groupdict['reason']

        yield self._do_moderequest('b', event.reply, nick, duration, channel)

        def do_kick(nick):
            self._send_as_op(Event("irc.do_kick",
                channel=channel,
                user=nick,
                reason=reason or ("Requested by " + event.user.split("!")[0]),
                ))
        if "@" in nick and "!" in nick and not "$" in nick:
            # A mask was given. Kick if the nick section doesn't have any
            # wildcards
            nick = nick.split("!")[0]
            if "*" not in nick:
                do_kick(nick)
        elif "@" not in nick and "!" not in nick and "$" not in nick:
            # Just a nick was given.
            do_kick(nick)


    @defer.inlineCallbacks
    def _do_moderequest(self, mode, reply, nick, duration, channel):
        """Does the work to set a mode on a nick (or hostmask) in a channel for
        an optional duration. If duration is None, we will not set it back
        after any length of time.

        reply is used to send error messages. It should take a string.

        """
        try:
            mask = (yield self._nick_to_hostmask(nick))
        except ircutil.NoSuchNick:
            reply("There is no user by that nick on the network. Try {0}!*@* to {1} anyone with that nick, or specify your own hostmask.".format(
                nick,
                {"q":"quiet","b":"ban"}.get(mode, "apply to"),
                ))
            return
        except ircutil.WhoisTimedout:
            reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
            return

        newevent = Event("irc.do_mode",
                channel=channel,
                set=True,
                modes=mode,
                user=mask,
                )
        if duration:
            log.msg("+%s for %s in %s for %s" % (mode, mask, channel, duration))
        else:
            log.msg("+%s for %s in %s" % (mode, mask, channel, ))

        if not (yield self._send_as_op(newevent, reply)):
            return

        if duration:
            if isinstance(duration, basestring):
                duration = parse_time(duration)
            self._set_timer(duration, mask, channel, mode)

    @require_channel
    def unquiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        
        self._do_modederequest('q', event.reply, nick, duration, channel)

    @require_channel
    def unban(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        
        self._do_modederequest('b', event.reply, nick, duration, channel)

    @defer.inlineCallbacks
    def _do_modederequest(self, mode, reply, nick, duration, channel):
        try:
            mask = (yield self._nick_to_hostmask(nick))
        except ircutil.NoSuchNick:
            reply("There is no user by than nick on the network. Check the username or try specifying a full hostmask")
            return
        except ircutil.WhoisTimedout:
            reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
            return

        if duration:
            if isinstance(duration, basestring):
                duration = parse_time(duration)
            self._set_timer(duration, mask, channel, mode)
            reply("It shall be done")
            return

        newevent = Event("irc.do_mode",
                channel=channel,
                set=False,
                modes=mode,
                user=mask,
                )
        log.msg("-%s for %s in %s" % (mode, mask, channel))
        self._send_as_op(newevent, reply)


class IRCTopic(CommandPluginSuperclass):
    """Topic manipulation commands. For now assumes the channel is not +t (or
    the bot is OP)

    """
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
                cmdmatch="append|push",
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

    def _get_change_topic_event(self, channel, to):
        topicchange = Event("irc.do_topic",
                channel=channel,
                topic=to)
        return topicchange

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
            currenttopic += " | " + match.groupdict()['text']
            topicevent = self._get_change_topic_event(channel, currenttopic)
            self.transport.send_event(topicevent)
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
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
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
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
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
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
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
            topicevent = self._get_change_topic_event(channel, newtopic)

            self.transport.send_event(topicevent)
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

        topicevent = self._get_change_topic_event(channel, newtopic)

        self.transport.send_event(topicevent)


