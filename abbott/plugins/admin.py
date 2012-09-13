from collections import defaultdict, deque
from functools import wraps
import glob, os.path
import heapq
import time
import re

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
            elif self.have_op[channel]:
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
    """Provides a command interface to IRC operator tasks. Uses one of the
    above chanserv plugins as an interface to do the tasks. Since the bot must
    aquire OP itself for some operations like kick, this plugin relies on the
    above OpSelf plugin to function.

    """

    def __init__(self, *args):
        self.later_timer = None
        self.started = False

        super(IRCAdmin, self).__init__(*args)

    def reload(self):
        super(IRCAdmin, self).reload()
        if "dolater" not in self.config:
            self.config['dolater'] = []
            self.config.save()

        heapq.heapify(self.config['dolater'])

        if self.later_timer:
            self.later_timer.cancel()
            self.later_timer = None
        self._set_later_timer()


    def _set_later_timer(self):
        """Looks at the tasks we have to do later, and sets a callLater timer
        for the next one
        """
        if not self.started:
            return
        if self.later_timer:
            self.later_timer.cancel()
            self.later_timer = None

        if not self.config['dolater']:
            self.later_timer = None
            return
        next_event = self.config['dolater'][0]

        log.msg("Next event is %r. Setting timer" % (next_event,))
        delay = max(next_event[0]-time.time(), 1)
        self.later_timer = reactor.callLater(delay, self._process_laters)

    def _process_laters(self):

        self.later_timer = None
        now = time.time()
        later_items = self.config['dolater']

        try:
            while later_items and later_items[0][0] <= now:
                event_info = heapq.heappop(later_items)[1]
                log.msg("Processing later event %r" % (event_info,))
                
                channel = event_info['channel']
                user = event_info['user']
                mode = event_info['mode']
                def reply(s):
                    s = "I was about to un-{0} {1}, but {2}".format(
                            {'q':'quiet','b':'ban'}[mode],
                            user,
                            s,
                            )
                    self.transport.send_event(Event("irc.do_msg",
                        user=channel,
                        message=s,
                        ))
                self._send_as_op(Event("irc.do_mode",
                    channel=channel,
                    set=False,
                    modes=mode,
                    user=user),
                    reply,
                    )
        finally:
            self.config.save()
            self._set_later_timer()

    def stop(self):
        super(IRCAdmin, self).stop()
        if self.later_timer:
            self.later_timer.cancel()
            self.later_timer = None

    def start(self):
        super(IRCAdmin, self).start()

        self.started = True
        self._set_later_timer()

        # kick command
        self.install_command(
                cmdname="kick",
                cmdmatch="kick|KICK",
                cmdusage="<nickname> [reason]",
                argmatch = "(?P<nick>[^ ]+)( (?P<reason>.*))?$",
                permission="irc.op.kick",
                prefix=".",
                callback=self.kick,
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
                cmdmatch="voice|VOICE",
                cmdusage="[nick]",
                argmatch = "(?P<nick>[^ ]+)?$",
                permission="irc.op.voice",
                prefix=".",
                callback=self.voice,
                helptext="Grants a user voice in the current channel"
                )

        self.install_command(
                cmdname="devoice",
                cmdmatch="devoice|DEVOICE",
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
                cmdmatch="quiet|QUIET",
                cmdusage="<nick or hostmask> [for <duration>]",
                argmatch = "(?P<nick>[^ ]+)(?: (?:for )?{0}+)?$".format(duration_match),
                prefix=".",
                permission="irc.op.quiet",
                callback=self.quiet,
                helptext="Quiets a user."
                )

        self.install_command(
                cmdname="unquiet",
                cmdmatch="unquiet|UNQUIET",
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
        for this channel and set a callback to issue a kick event

        """
        groupdict = match.groupdict()
        nick = groupdict['nick']
        reason = groupdict.get("reason", None)
        channel = event.channel

        kickevent = Event("irc.do_kick", channel=channel,
                user=nick, reason=reason)
        self._send_as_op(kickevent, event.reply)


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

        self._do_moderequest('q', event, nick, duration, channel)

    @require_channel
    @defer.inlineCallbacks
    def ban(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        reason = groupdict['reason']

        yield self._do_moderequest('b', event, nick, duration, channel)

        # nick could also be a hostmask or extban. Do a simple check to see if
        # it looks like a nick
        if "@" not in nick and "!" not in nick and "$" not in nick:
            self._send_as_op(Event("irc.do_kick",
                channel=channel,
                user=nick,
                reason=reason or ("Requested by " + event.user.split("!")[0]),
                ))


    @defer.inlineCallbacks
    def _do_moderequest(self, mode, event, nick, duration, channel):
        try:
            mask = (yield self._nick_to_hostmask(nick))
        except ircutil.NoSuchNick:
            event.reply("There is no user by than nick on the network. Check the username or try specifying a full hostmask")
            return
        except ircutil.WhoisTimedout:
            event.reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
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

        if not (yield self._send_as_op(newevent, event.reply)):
            return

        if duration:
            duration = parse_time(duration)
            endtime = time.time()+duration
            heapq.heappush(self.config['dolater'],
                    [endtime,
                        {
                            'channel': channel,
                            'user': mask,
                            'mode': mode,
                        },
                    ]
                )
            self.config.save()
            self._set_later_timer()

    @require_channel
    def unquiet(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        
        self._do_modederequest('q', event, nick, duration, channel)

    @require_channel
    def unban(self, event, match):
        groupdict = match.groupdict()
        nick = groupdict['nick']
        duration = groupdict['duration']
        channel = event.channel
        
        self._do_modederequest('b', event, nick, duration, channel)

    @defer.inlineCallbacks
    def _do_modederequest(self, mode, event, nick, duration, channel):
        try:
            mask = (yield self._nick_to_hostmask(nick))
        except ircutil.NoSuchNick:
            event.reply("There is no user by than nick on the network. Check the username or try specifying a full hostmask")
            return
        except ircutil.WhoisTimedout:
            event.reply("That's odd, the whois I did on %s didn't work. Sorry." % nick)
            return

        if duration:
            duration = parse_time(duration)
            endtime = time.time()+duration
            heapq.heappush(self.config['dolater'],
                    [endtime,
                        {
                            'channel': channel,
                            'user': mask,
                            'mode': mode,
                        },
                    ]
                )
            self.config.save()
            self._set_later_timer()
            event.reply("It shall be done")
            return

        newevent = Event("irc.do_mode",
                channel=channel,
                set=False,
                modes=mode,
                user=mask,
                )
        log.msg("-%s for %s in %s" % (mode, mask, channel))
        self._send_as_op(newevent, event.reply)


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


