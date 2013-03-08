# encoding: UTF-8
from collections import defaultdict
import functools
from itertools import chain

from twisted.python import log
from twisted.internet import defer, reactor

from .. import command
from . import ircutil

"""

The auth module provides the Auth plugin, which handles authorization of irc
users. It identifies irc users by the response code 330 from a whois, commonly
used to supply the username the user is logged in with.

The plugin hooks incoming irc events and adds a function to the event object:
has_permission(). This function takes two parameters: a permission string, and
a channel, and returns a deferred which fires with a boolean value indicating
if the user that initiated the event has that permission in that channel or
not. (if channel is irrelevant for the permission, it should be None,
indicating a global permission)

Permission strings are heirarchical strings delimited by dots. If a user has a
permission, they also have all sub-permissions of that permission. For example,
if a user has permission "foo.bar", these calls return true:
    has_permission("foo.bar")
    has_permission("foo.bar.baz")

Groups are also implemented and are fairly simple. Group names start with a %,
and groups are assigned permissions just the same as users.  Users are then
assigned to groups, and the users inherit all the permissions of that group, as
well as their individually assigned permissions. Pretty standard.

For example, a group has permission irc.op, then all users of that group are
granted irc.op. Groups are assigned permissions, and users are assigned
membership in a group.

Groups do not cascade. You cannot assign a group to another group.

Permission commands fall under the !permission command. Group commands are
under the !group command. Most permission manipulation commands require the
permission auth.edit.  The group manipulation commands are different. They
require the auth.edit.group.<groupname> permission (which is auto granted if
you have auth.edit). This is so you can grant permission to edit a specific
group, while not requiring having access to edit all groups or all permissions.

"""

def satisfies(user_perm, auth_perm):
    """Does the user permission satisfy the required auth_perm?

    If auth_perm is some permission required by a command, and user_perm is
    some permission that a user has, this function returns True if user_perm
    grants access to auth_perm

    Permission strings are hierarchical. Granting admin will grant admin,
    admin.op1, admin.op2, etc.

    Simple globs are supported, and do not transcend dots.  Granting
    admin.*.foo will allow admin.bar.foo and admin.baz.foo, but not
    admin.bar.baz.foo

    Globs at the end of permissions match as expected from the above rules.
    Granting admin.* will allow admin.foo, admin.bar, admin.foo.baz, etc. (but
    NOT 'admin' by itself!)

    The super-user's permission is simply *

    Partial globbing is not allowed. Only entire permission elements may be
    globs. "admin.foo*bar" is not allowed. Instead, split it up to
    "admin.foo.*.bar"

    """
    user_parts = user_perm.split(".")
    auth_parts = auth_perm.split(".")

    if len(user_parts) > len(auth_parts):
        # The auth required is more general than the user permission. There's
        # no way for this to be satisfied.
        return False

    # Check all the corresponding elements match.
    for userelem, authelem in zip(user_parts, auth_parts):
        if userelem == "*" or authelem == "*":
            continue

        if userelem != authelem:
            return False

    # There may be more elements of auth_parts, but that's okay. It just means
    # the user has a more general (more powerful) permission than is required.
    return True

class Auth(command.CommandPluginSuperclass):
    """Auth plugin.

    Provides a reliable set of permissions other plugins can rely on. For
    certain irc events, installs a has_permission() callback which can be used
    to query if a user has a particular permission.
    
    """
    REQUIRES = ["ircutil.IRCWhois"]
    DEFAULT_CONFIG = {
            # Maps authnames to a list of (channel, perm string) tuples
            "perms": {},
            # A list of (channel, perm string) tuples
            "defaultperms": [],
            # Maps authnames to a list of groups they're in
            "groups": {},
            }

    def start(self):
        super(Auth, self).start()

        # Install a middleware hook for all irc events
        self.install_middleware("irc.on_*")

        # maps hostmasks to authenticated usernames, or None to indicate the
        # user doesn't have any auth information
        self.authd_users = {}

        permgroup = self.install_cmdgroup(
                grpname="permission",
                permission="auth.edit",
                helptext="Permission manipulation commands",
                )

        permgroup.install_command(
                cmdname="grant",
                cmdmatch="add|grant",
                argmatch=r"(?P<name>%?\w+) (?P<perm>[^ ]+)(?: (?P<channel>[^ ]+))?$",
                callback=self.permission_add,
                cmdusage="<authname | %groupname> <permission> [channel]",
                helptext="Grants a user the specified permission, either globally or in the specified channel. If authname starts with an %, it indicates a group",
                )

        permgroup.install_command(
                cmdname="revoke",
                cmdmatch="revoke|remove",
                argmatch=r"(?P<name>%?\w+) (?P<perm>[^ ]+)(?: (?P<channel>[^ ]+))?$",
                callback=self.permission_revoke,
                cmdusage="<authname | %groupname> <permission> [channel]",
                helptext="Revokes the specified permission from the user, either globally or in the specifed channel. If authname starts with an %, it indicates a group",
                )

        permgroup.install_command(
                cmdname="list",
                argmatch=r"(?P<name>%?[\w.]+)?$",
                callback=self.permission_list,
                cmdusage="[authname | %groupname]",
                helptext="Lists the permissions granted to the given or current user. If authname starts with an %, it indicates a group",
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

        ### Install group commands

        groupgroup = self.install_cmdgroup(
                grpname="group",
                permission="auth.edit.group.*",
                helptext="Authentication group manipulation commands",
                )

        groupgroup.install_command(
                cmdname="add",
                argmatch=r"(?P<user>[^ ]+) (?P<group>%\w+)$",
                callback=self.group_add,
                cmdusage="<user> <%group>",
                helptext="Adds a user to the named group. Group names start with a %",
                )

        groupgroup.install_command(
                cmdname="remove",
                argmatch=r"(?P<user>[^ ]+) (?P<group>%\w+)$",
                callback=self.group_remove,
                cmdusage="<user> <%group>",
                helptext="Removes a user from the named group. Group names start with a %",
                )

        groupgroup.install_command(
                cmdname="list",
                argmatch=r"(?P<group>%\w+)?$",
                callback=self.group_list,
                cmdusage="[%group]",
                helptext="Lists the members of the specified group, or list all the groups. Group names start with a %",
                )

        # Top level command
        self.install_command(
                cmdname="whoami",
                permission=None,
                callback=self.permission_list,
                helptext="Tells you who you're auth'd as and lists your permissions.",
                )

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


    @defer.inlineCallbacks
    def _get_permissions(self, hostmask):
        """This function returns the permissions granted to the given user,
        identifying them in the process by doing a whois lookup if necessary.

        It returns a deferred object which fires with an iterable over
        (channel, permissionstr) tuples the user has, or an empty list of the
        user does not have any permissions or the user could not be identified.
        It does NOT include any default permissions, only permissions
        explicitly granted to the user (along with any groups the user is in).
        
        This method may send a whois to the server, in which case it looks for
        an IRC 330 command back from the server indicating the user's authname

        """
        # Check if the user is already identified by a previous whois
        if hostmask in self.authd_users:
            authname = self.authd_users[hostmask]

        else:
            # No cached entry for that hostmask in authd_users. Do a whois and look
            # it up.
            log.msg("Permission request for %s, but I don't know the authname. Doing a whois" % (hostmask,))
            nick = hostmask.split("!")[0]
            try:
                whois_info = (yield self.transport.issue_request("irc.whois", nick))
            except ircutil.WhoisError as e:
                log.msg("Whois failed: %s" % e)
                whois_info = {}

            if "330" not in whois_info:
                # No auth information. Cache this value for one minute
                authname = None
                self.authd_users[hostmask] = None
                def cacheprune():
                    if hostmask in self.authd_users and self.authd_users[hostmask] == None:
                        del self.authd_users[hostmask]
                reactor.callLater(60, cacheprune)

            else:
                authname = self.authd_users[hostmask] = whois_info["330"][1]

        # if authname is none at this point, it indicates the whois didn't
        # return any auth info. Remember this method does not account for
        # default permissions, so just return an empty set
        perms = set()
        if authname:
            perms.update(tuple(x) for x in self.permissions[authname])

            # Now dereference perms from any groups the user is in
            for group in self.config['groups'].get(authname, []):
                perms.update(tuple(x) for x in self.permissions[group])

        defer.returnValue(perms)

    @defer.inlineCallbacks
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
            defer.returnValue(True)
            return

        user_perms = (yield self._get_permissions(hostmask))

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
                defer.returnValue(True)
                return
        defer.returnValue(False)

    @defer.inlineCallbacks
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
            defer.returnValue([None])
            return

        user_perms = (yield self._get_permissions(hostmask))

        channels = set()
        for perm_channel, user_perm in chain(user_perms, self.config['defaultperms']):

            # If the user's permission user_perm grants `permission`, add
            # `perm_channel` to the channel set
            if satisfies(user_perm, permission):
                channels.add(perm_channel)

        defer.returnValue(channels)

    ### Reload event
    def reload(self):
        super(Auth, self).reload()
        self.config['perms'] = defaultdict(list, self.config['perms'])
        self.permissions = self.config['perms']

        # Also turn groups into a defaultdict
        self.config['groups'] = defaultdict(list, self.config['groups'])

    ### The command plugin callbacks, installed above

    def permission_add(self, event, match):
        groupdict = match.groupdict()
        name = groupdict['name']
        perm = groupdict['perm']
        channel = groupdict.get("channel", None)

        # This must be a list, even though a tuple is more appropriate, because
        # they come back from json as a list. If it's changed to a tuple, you
        # must convert them on reload and also change the .remove() method in
        # permission_revoke()
        self.permissions[name].append([channel, perm])
        self.config.save()

        if channel:
            event.reply("Permission {0} granted for {usergroup} {1} in channel {2}".format(
                perm, name, channel,
                usergroup = "group" if name.startswith("%") else "user",
                ))
        else:
            event.reply("Permission {0} granted globally for {usergroup} {1}".format(perm, name,
                usergroup = "group" if name.startswith("%") else "user",
                ))

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
                event.reply("{usergroup} {0} doesn't have permission {1} in channel {2}!".format(
                    name, perm, channel,
                    usergroup = "Group" if name.startswith("%") else "User",
                    ))
            else:
                event.reply("{usergroup} {0} doesn't have the global permission {1}!".format(
                    name, perm,
                    usergroup = "Group" if name.startswith("%") else "User",
                    ))
        else:
            self.config.save()
            if channel:
                event.reply("Permission {0} revoked for {usergroup} {1} in channel {2}".format(
                    perm, name, channel,
                    usergroup = "group" if name.startswith("%") else "user",
                    ))
            else:
                event.reply("Global permission {0} revoked for user {1}".format(perm, name,
                    usergroup = "group" if name.startswith("%") else "user",
                    ))

    @defer.inlineCallbacks
    def permission_list(self, event, match):
        name = match.groupdict().get('name', None)
        if name:
            perms = set(tuple(x) for x in self.permissions[name])
            if name.startswith("%"):
                msgstr = "group {0} has".format(name)
            else:
                msgstr = "user {0} has".format(name)
            groups = self.config['groups'][name]
        else:
            # Get info about the current user
            perms = set((yield self._get_permissions(event.user)))
            if self.authd_users.get(event.user, None):
                event.reply("You are identified as %s" % self.authd_users[event.user])
                groups = self.config['groups'][self.authd_users[event.user]]
            else:
                event.reply("I don't know who you are")
                groups = []
            msgstr = "you have"

        # dereference groups
        for group in groups:
            perms.update(tuple(x) for x in self.permissions[group])

        # Maps channels to the permissions `user` holds in that channel
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
            for perm_chan, perms in perms_map.items():
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
            self.config.save()
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
            self.config.save()
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

        for perm_chan, perms in perms_map.items():
            event.reply("Default permissions for channel %s: %s" % (
                perm_chan,
                ", ".join(perms)))

    @defer.inlineCallbacks
    def group_add(self, event, match):
        gd = match.groupdict()
        user = gd['user']
        group = gd['group']

        if not (yield self._has_permission(event.user,
                "auth.edit.group.{0}".format(group), None)):
            event.reply("You do not have permissions to modify that group")
            return
        
        permlist = self.config['groups'][user]
        if group in permlist:
            event.reply("User {0} is already a member of group {1}".format(
                user, group))
        else:
            permlist.append(group)
            self.config.save()
            event.reply("User {0} added as a member of group {1}".format(
                user, group))

    @defer.inlineCallbacks
    def group_remove(self, event, match):
        gd = match.groupdict()
        user = gd['user']
        group = gd['group']
        
        if not (yield self._has_permission(event.user,
                "auth.edit.group.{0}".format(group), None)):
            event.reply("You do not have permissions to modify that group")
            return

        permlist = self.config['groups'][user]
        if group not in permlist:
            event.reply("User {0} is not a member of group {1}".format(
                user, group))
        else:
            permlist.remove(group)
            self.config.save()
            event.reply("User {0} removed from group {1}".format(
                user, group))

    @defer.inlineCallbacks
    def group_list(self, event, match):
        gd = match.groupdict()
        group = gd['group']

        if group:
            # Request to list a group. First make sure the user has access to the group.
            if not (yield self._has_permission(event.user,
                    "auth.edit.group.{0}".format(group), None)):
                event.reply("You do not have permissions to view that group")
                return
            
            members = set()
            for user, groups in self.config['groups'].items():
                if group in groups:
                    members.add(user)

            members = sorted(members)
            event.reply("Members in group {0}: {1}".format(group, ", ".join(members)),
                    notice=True, direct=True)

        else:
            # List all groups the user has access to.
            allgroups = set()
            for user, groups in self.config['groups'].items():
                allgroups.update(groups)

            # Filter the groups the user has access to
            access_groups = []
            for g in allgroups:
                if (yield self._has_permission(event.user,
                    "auth.edit.group.{0}".format(g), None)):
                    access_groups.append(g)

            access_groups.sort()

            log.msg(access_groups)
            event.reply("Groups you have access to: {0}".format(", ".join(access_groups)))
