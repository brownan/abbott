from collections import defaultdict
import functools
import re
from itertools import chain

from twisted.python import log
from twisted.internet import defer, reactor

from .. import command
from ..transport import Event

def satisfies(user_perm, auth_perm):
    """Does the user permission satisfy the required auth_perm?

    If auth_perm is some permission required by a command, and user_perm is
    some permission that a user has, this function returns True if user_perm
    grants access to auth_perm

    Permission strings are hierarchical. Granting admin will grant admin,
    admin.op1, admin.op2, etc.

    Globs are supported, and do not transcend dots. Granting admin.*.foo will
    allow admin.bar.foo and admin.baz.foo, but not admin.bar.baz.foo

    Globs at the end of permissions match as expected from the above rules.
    Granting admin.* will allow admin.foo, admin.bar, admin.foo.baz, etc. (but
    NOT 'admin' by itself!)

    The super-user's permission is simply *

    """
    # Expand the *s to [^.]* and re.escape everything else
    user_perm = "[^.]*".join(
            re.escape(x) for x in user_perm.split("*")
            )
    # The match should conclude with ($|\.) to indicate it must
    # either match exactly or any sub-permission
    # for example, the permission
    #   irc.op
    # should match
    #   irc.op
    #   irc.op.kick
    #   irc.op.etc
    # but it should NOT match something like
    #   irc.open
    user_perm += r"($|\.)"

    if re.match(user_perm, auth_perm):
        return True
    return False

class Auth(command.CommandPluginSuperclass):
    """Auth plugin.

    Provides a reliable set of permissions other plugins can rely on. For
    certain irc events, installs a has_permission() callback which can be used
    to query if a user has a particular permission.
    
    """
    def start(self):
        super(Auth, self).start()

        # Install a middleware hook for all irc events
        self.install_middleware("irc.on_*")
        self.listen_for_event("irc.on_unknown")

        # maps hostmasks to authenticated usernames, or None to indicate the
        # user doesn't have any auth information
        self.authd_users = {}

        # maps nicks to hostmasks. This is used temporarily to correlate
        # separate messages by the server
        self.nick_to_hostmask = {}

        # Maps hostmasks to sets of deferred objects that are waiting for a
        # whois request to come back from the server. They get called by
        # _check_ready() when the response comes in, or by _fail_request() if
        # the request times out.
        self.waiting = defaultdict(set)

        permgroup = self.install_cmdgroup(
                grpname="permission",
                permission="auth.edit_permissions",
                helptext="Permission manipulation commands",
                )

        permgroup.install_command(
                cmdname="add",
                argmatch=r"(?P<name>\w+) (?P<perm>[^ ]+)(?: (?P<channel>[^ ]+))?$",
                callback=self.permission_add,
                cmdusage="<authname> <permission> [channel]",
                helptext="Grants a user the specified permission, either globally or in the specified channel",
                )

        permgroup.install_command(
                cmdname="revoke",
                argmatch=r"(?P<name>\w+) (?P<perm>[^ ]+)(?: (?P<channel>[^ ]+))?$",
                callback=self.permission_revoke,
                cmdusage="<authname> <permission> [channel]",
                helptext="Revokes the specified permission from the user, either globally or in the specifed channel",
                )

        permgroup.install_command(
                cmdname="list",
                argmatch=r"(?P<name>[\w.]+)?$",
                callback=self.permission_list,
                cmdusage="[authname]",
                helptext="Lists the permissions granted to the given or current user",
                )

        permgroup.install_command(
                cmdname="default add",
                argmatch="(?P<perm>[^ ]+)(?: (?P<channel>[^ ]+))?$",
                callback=self.add_default,
                cmdusage="<permission> [channel]",
                helptext="Adds a default permission; a permission that everyone implicitly has, either globally or in the specified channel",
                )
        permgroup.install_command(
                cmdname="default revoke",
                argmatch="(?P<perm>[^ ]+)(?: (?P<channel>[^ ]+))?$",
                callback=self.revoke_default,
                cmdusage="<permission> [channel]",
                helptext="Revokes a default permission, either globally or in the specified channel",
                )
        permgroup.install_command(
                cmdname="default list",
                callback=self.list_default,
                cmdusage="<permission>",
                helptext="Lists the default permissions",
                )

        # Top level command
        self.install_command(
                cmdname="whoami",
                permission=None,
                callback=self.permission_list,
                helptext="Tells you who you're auth'd as and lists your permissions.",
                )

        # Put a few default items in the config if they don't exist
        if "perms" not in self.config:
            self.config['perms'] = {}
            self.pluginboss.save()
        if "defaultperms" not in self.config:
            self.config['defaultperms'] = []
            self.pluginboss.save()

        # Compatibility check for a new schema. Use items() since we're going
        # to mutate the dict in the loop
        for user, permissionlist in self.permissions.items():
            if permissionlist and not isinstance(permissionlist[0], list):
                permissionlist = [[None, x] for x in permissionlist]
                self.permissions[user] = permissionlist
                self._save()

        for perm in list(self.config['defaultperms']):
            if not isinstance(perm, list):
                self.config['defaultperms'].remove(perm)
                self.config['defaultperms'].append([None, perm])
                self._save()

    def received_middleware_event(self, event):
        """For events that are applicable, install a handler one can call to
        see if a user has a particular permission.

        This way, auth permissions aren't checked until a plugin actually wants
        to verify identity.

        """

        if event.eventtype in [
                "irc.on_privmsg",
                "irc.on_mode_changed",
                "irc.on_user_joined",
                "irc.on_action",
                "irc.on_topic_updated",
                ]:
            event.has_permission = functools.partial(self._has_permission, event.user)
            event.where_permission = functools.partial(self._where_permission, event.user)

        return event

    def on_event_irc_on_unknown(self, event):
        if event.command == "RPL_WHOISUSER":
            nick = event.params[1]
            user = event.params[2]
            host = event.params[3]
            self.nick_to_hostmask[nick] = "%s!%s@%s" % (nick,user,host)

        elif event.command == "330":
            # Command 330 is RPL_WHOISACCOUNT
            nick = event.params[1]
            authname = event.params[2]

            try:
                hostmask = self.nick_to_hostmask.pop(nick)
            except KeyError:
                log.err("Got a RPL_WHOISACCOUNT but I don't know the hostmask! This shouldn't happen, but could if the server sends whois messages in a different order or doesn't send a RPL_WHOISACCOUNT line at all")
                return

            # Successfully correlated a hostmask with an authname. Save it here.
            self.authd_users[hostmask] = authname

            self._check_ready(hostmask)

    def _check_ready(self, hostmask):
        """If the user (named by the hostmask) has been verified and there is a
        deferred object waiting, then we need to respond to it.

        If there is no deferred object waiting, there is nothing to do

        If there are deferred waiting, but no result, do nothing. It's not
        ready yet!

        """
        deferreds = self.waiting[hostmask]
        if not deferreds:
            return

        # See if the user has been identified yet
        try:
            authname = self.authd_users[hostmask]
        except KeyError:
            # Not yet ready
            return

        log.msg("user %s is authed as %s. Calling deferred callbacks" % (hostmask, authname))

        # Get the permissions.
        # I don't think authname could be None at this point in the code, but
        # not wanting to analyze it too closely, I'm putting in this check
        perms = self.permissions[authname] if authname else []

        del self.waiting[hostmask]
        for deferred in deferreds:
            deferred.callback(perms)

    def _fail_request(self, hostmask):
        """This is called 5 seconds after a whois request is issued. If we
        don't get a server response in time, we assume the user is not
        authenticated. If the deferred is still in self.waiting, then that is
        the case, we need to respond to it

        """
        deferreds = self.waiting[hostmask]
        if deferreds:
            log.msg("No identity information returned for %s. Returning no permissions" % (hostmask,))

            # Set the authname for this hostmask to None, to avoid doing a
            # bunch of whois requests at once. This acts as a simple cache to
            # remember hostmasks that aren't authenticated.
            self.authd_users[hostmask] = None
            def cacheprune():
                if hostmask in self.authd_users and self.authd_users[hostmask] == None:
                    del self.authd_users[hostmask]
            reactor.callLater(300, cacheprune)

            # Clear the callbacks waiting for this hostmask and call them
            del self.waiting[hostmask]
            for deferred in deferreds:
                deferred.callback([])



    def _get_permissions(self, hostmask):
        """This function returns the permissions granted to the given user,
        identifying them in the process by doing a whois lookup if necessary.

        It returns a deferred object. The parameter to the deferred callback is
        a list of (channel, permissionstr) the user has, or an empty list of the
        user does not have any permissions or the user could not be identified.
        It does NOT include any default permissions, only permissions
        explicitly granted to the user.
        
        This method may send a whois to the server, in which case it looks for
        an IRC 330 command back from the server indicating the user's authname

        """
        # Check if the user is already identified by a previous whois
        if hostmask in self.authd_users:
            authname = self.authd_users[hostmask]
            # authname could be none, indicating a recent whois for that
            # hostmask didn't return any auth info
            perms = self.permissions[authname] if authname else []
            return defer.succeed(perms)

        if hostmask in self.waiting:
            # There is already a pending request for permissions for this
            # hostname. Don't issue another whois, just add another deferred
            # object to this set.
            log.msg("Request for permission for %s, but there is already a pending request" % hostmask)
            deferred = defer.Deferred()
            self.waiting[hostmask].add(deferred)
            return deferred

        deferred = defer.Deferred()
        self.waiting[hostmask].add(deferred)

        log.msg("Permission request for %s, but I don't know the authname. Doing a whois" % (hostmask,))

        # Send the whois to the server
        whois_event = Event("irc.do_whois", nickname=hostmask.split("!",1)[0])
        self.transport.send_event(whois_event)

        # in case no auth mapping returned...
        reactor.callLater(5, self._fail_request, hostmask)

        return deferred

    def _has_permission(self, hostmask, permission, channel):
        """Asks if the user identified by hostmask has the given permission
        string `permission` in the given channel. Channel can be None to
        indicate a global permission is required.

        This function is installed as event.has_permission() by the Auth
        plugin, and is partially evaluated with the hostname already filled in,
        so only the remaining arguments are specified when calling.

        It returns a deferred object which passes to its callback a boolean
        value: True if the user has access, and False if the user does not.

        """
        if permission == None:
            return defer.succeed(True)

        d = self._get_permissions(hostmask)

        def check_permission(user_perms):
            for perm_channel, user_perm in chain(user_perms, self.config['defaultperms']):
                # Does perm_channel apply to `channel`?
                if not (
                        # One of these must be true for this permission to
                        # apply here.
                        perm_channel is None or
                        perm_channel == channel
                        ):
                    continue

                # Does user_perm satisfy `permission`?

                if satisfies(user_perm, permission):
                    return True
            return False

        # I think I'm finally "getting" twisted deferrs!
        d.addCallback(check_permission)
        return d

    def _where_permission(self, hostmask, permission):
        """This is a call made specifically for help-related plugins. It
        returns a list of channels where the given user has the given
        permission.

        This function is installed on event objects as
        event.where_permission(), partially evaluated with the hostname, so it
        only needs the permission.

        This returns a deferred. It produces a set of channels that have
        `permission`, or an empty list if the user doesn't have the permission
        anywhere.

        """
        if permission == None:
            return defer.succeed([None])

        d = self._get_permissions(hostmask)

        def enumerate_channels(user_perms):
            channels = set()
            for perm_channel, user_perm in chain(user_perms, self.config['defaultperms']):

                # If the user's permission user_perm grants `permission`, add
                # `perm_channel` to the channel set
                if satisfies(user_perm, permission):
                    channels.add(perm_channel)

            return channels

        d.addCallback(enumerate_channels)
        return d

    def _save(self):
        # Make a copy... don't store the defaultdict (probably wouldn't matter though)
        self.config['perms'] = dict(self.permissions)
        self.pluginboss.save()

    ### The command plugin callbacks, installed above

    def permission_add(self, event, match):
        groupdict = match.groupdict()
        name = groupdict['name']
        perm = groupdict['perm']
        channel = groupdict.get("channel", None)
        # It should really be a tuple, but tuples are inserted as lists by json
        self.permissions[name].append([channel, perm])
        self._save()
        if channel:
            event.reply("Permission %s granted for user %s in channel %s" % (
                perm, name, channel,
                ))
        else:
            event.reply("Permission %s granted globally for user %s" % (perm, name))

    def permission_revoke(self, event, match):
        groupdict = match.groupdict()
        name = groupdict['name']
        perm = groupdict['perm']
        channel = groupdict.get("channel", None)
        try:
            self.permissions[name].remove([channel, perm])
        except ValueError:
            # keyerror if the user doesn't have any, valueerror if the user has
            # some but not this one
            if channel:
                event.reply("User %s doesn't have permission %s in channel %s!" % (
                    name, perm, channel,
                    ))
            else:
                event.reply("User %s doesn't have the global permission %s!" % (
                    name, perm,
                    ))
        else:
            self._save()
            if channel:
                event.reply("Permission %s revoked for user %s in channel %s" % (
                    perm, name, channel,
                    ))
            else:
                event.reply("Global permission %s revoked for user %s" % (perm, name))

    @defer.inlineCallbacks
    def permission_list(self, event, match):
        name = match.groupdict().get('name', None)
        if name:
            perms = list(self.permissions[name])
            msgstr = "user %s has" % name
        else:
            # Get info about the current user
            perms = list((yield self._get_permissions(event.user)))
            if self.authd_users.get(event.user, None):
                event.reply("You are identified as %s" % self.authd_users[event.user])
            msgstr = "you have"

        perms.extend(self.config['defaultperms'])

        perms_map = defaultdict(set)
        for perm_chan, perm in perms:
            perms_map[perm_chan].add(perm)

        globalperms = perms_map.pop(None, set())
        if globalperms:
            event.reply("%s these global permissions: %s" % (
                msgstr.capitalize(), ", ".join(globalperms)))
        else:
            event.reply("%s no global permissions =(" % (msgstr,))

        # If this isn't a direct message, don't show all the other channels
        if event.direct:
            for perm_chan, perms in perms_map.iteritems():
                event.reply("In channel %s %s: %s" % (
                    perm_chan, msgstr,
                    ", ".join(perms)
                    ))
        elif perms_map:
            this_chan = perms_map.pop(event.channel, None)
            if this_chan:
                event.reply("In channel %s %s: %s" % (
                    event.channel, msgstr,
                    ", ".join(this_chan)
                    ))

            if perms_map:
                event.reply("Also, %s some permissions in other channels. (Ask me in private to see them)" %
                        msgstr)


    ### Default permission callbacks
    def add_default(self, event, match):
        groupdict = match.groupdict()
        permission = groupdict['perm']
        channel = groupdict.get("channel", None)
        if [channel, permission] not in self.config['defaultperms']:
            self.config['defaultperms'].append([channel, permission])
            self.pluginboss.save()
            if channel:
                event.reply("Done! Everybody now has %s in %s!" % (permission, channel))
            else:
                event.reply("Done! Everybody now has %s globally!" % (permission,))
        else:
            event.reply("That's already a default permission. Idiot.")

    def revoke_default(self, event, match):
        groupdict = match.groupdict()
        permission = groupdict['perm']
        channel = groupdict.get("channel", None)
        try:
            self.config['defaultperms'].remove([channel, permission])
        except ValueError:
            event.reply("That permission is not in the default list")
        else:
            self.pluginboss.save()
            event.reply("Done. Revoked.")

    def list_default(self, event, match):
        perms_map = defaultdict(set)
        for perm_chan, perm in self.config['defaultperms']:
            perms_map[perm_chan].add(perm)

        globalperms = perms_map.pop(None, set())
        if globalperms:
            event.reply("Default global permissions: %s" % 
                    ", ".join(globalperms))
        else:
            event.reply("No global permissions")

        for perm_chan, perms in perms_map.iteritems():
            event.reply("Default permissions for channel %s: %s" % (
                perm_chan,
                ", ".join(perms)))

    ### Reload event
    def reload(self):
        super(Auth, self).reload()
        self.permissions = defaultdict(list)
        self.permissions.update(self.config.get('perms', {}))
