from collections import defaultdict
import functools
import re

from twisted.python import log
from twisted.internet import defer, reactor

from ..pluginbase import BotPlugin
from .. import command
from ..transport import Event

class Auth(command.CommandPluginSuperclass):
    """Auth plugin.

    Provides a reliable set of permissions other plugins can rely on. For
    certain irc events, installs a get_permissions() callback which can be used
    to query for permissions the user has.
    
    """
    def start(self):
        super(Auth, self).start()

        # Install a middleware hook for all irc events
        self.install_middleware("irc.on_*")
        self.listen_for_event("irc.on_unknown")

        # maps hostmasks to authenticated usernames
        self.authd_users = {}

        # maps nicks to hostmasks. This is used temporarily to correlated
        # separate messages by the server
        self.nick_to_hostmask = {}

        # Maps hostmasks to sets of deferred objects that need calling
        self.waiting = defaultdict(set)

        return
        # Install a few commands. These will call the given callback. This
        # functionality is provided by the CommandPlugin class, from which this
        # class inherits. The CommandPlugin will also check permissions.
        self.install_command(r"permissions? add (?P<name>\w+) (?P<perm>[\w.\*]+)$",
                "auth.edit_permissions",
                self.permission_add)
        self.help_msg("permissions? add",
                "auth.edit_permissions",
                "'permission add <authname> <permission>' Grants a user the specified permission")

        self.install_command(r"permissions? revoke (?P<name>\w+) (?P<perm>[\w.\*]+)$",
                "auth.edit_permissions",
                self.permission_revoke)
        self.help_msg("permissions? revoke",
                "auth.edit_permissions",
                "'permission revoke <authname> <permission>' Revokes the specified permission from the user")

        self.install_command(r"permissions? list( (?P<name>[\w.]+))?$",
                None,
                self.permission_list)
        self.install_command(r"whoami$",
                None,
                self.permission_list)
        self.help_msg("permissions? list|whoami",
                None,
                "'permission list [authname]' Lists the permissions granted to the given or current user")

        self.help_msg("permissions?",
                None,
                "'permission <command> [options]' Possible permission commands: add, revoke, list")

        self.define_command('permission')

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
            event.get_permissions = functools.partial(self._get_permissions, event.user)
            event.has_permission = functools.partial(self._has_permission, event.user)

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

        # Get the permissions
        perms = self.permissions[authname]

        for deferred in deferreds:
            deferred.callback(perms)

        del self.waiting[hostmask]

    def _fail_request(self, hostmask):
        """This is called 5 seconds after a whois request is issued. If we
        don't get a server response in time, we assume the user is not
        authenticated. If the deferred is still in self.waiting, then that is
        the case, we need to respond to it

        """
        deferreds = self.waiting[hostmask]
        if deferreds:
            log.msg("No identity information returned for %s. Returning no permissions" % (hostmask,))

            for deferred in deferreds:
                deferred.callback([])

            del self.waiting[hostmask]


    def _get_permissions(self, hostmask):
        """This function is installed on supported events as
        event.get_permissions(). It is partially evaluated with the hostmask,
        so you don't need to provide the hostmask when you call it from the
        event object.

        It returns a deferred object. The parameter to the deferred callback is
        a list of permissions the user has, or an empty list of the user does
        not have any permissions or the user could not be identified.
        
        This method sends a whois to the server and looks for an IRC 330
        message indicating the user's authname

        """
        if hostmask in self.authd_users:
            authname = self.authd_users[hostmask]
            perms = self.config['perms'].get(authname, [])
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

    def _has_permission(self, hostmask, permission):
        """Asks if the user identified by hostmask has the given permission
        string `permission`.

        This function is installed as event.has_permission() by the Auth
        plugin, and is partially evaluated with the hostname, so it takes one
        parameter: the permission string.

        It returns a deferred object which passes to its callback a boolean
        value: True for the user has access, and False for the user does not.

        """
        if permission == None:
            return defer.succeed(True)

        d = self._get_permissions(hostmask)

        def check_permission(user_perms):
            for user_perm in user_perms:
                # Does user_perm satisfy `permission`?

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

                if re.match(user_perm, permission):
                    return True
            return False

        # I think I'm finally "getting" twisted deferrs!
        d.addCallback(check_permission)
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
        self.permissions[name].append(perm)
        self._save()
        event.reply("Permission %s granted for user %s" % (perm, name))

    def permission_revoke(self, event, match):
        groupdict = match.groupdict()
        name = groupdict['name']
        perm = groupdict['perm']
        try:
            self.permissions[name].remove(perm)
        except ValueError:
            # keyerror if the user doesn't have any, valueerror if the user has
            # some but not this one
            event.reply("User %s doesn't have permission %s!" % (name, perm))
        else:
            self._save()
            event.reply("Permission %s revoked for user %s" % (perm, name))

    def permission_list(self, event, match):
        name = match.groupdict().get('name', None)
        if name:
            event.reply("User %s has permissions %s" % (name, self.config['perms'].get(name, [])))
        else:
            # Get info about the current user
            def callback(perms):
                # At this point a whois has been performed. If the user is
                # identified, they should be in the list
                if event.user in self.authd_users:
                    msg = "You are identified as %s " % self.authd_users[event.user]
                    if perms:
                        msg += "and have the following permissions: %s" % ", ".join(perms)
                    else:
                        msg += "but don't have any special permissions =("
                else:
                    msg = "You are not identified. Try logging in to NickServ"
                event.reply(msg)

            deferred = event.get_permissions()
            deferred.addCallback(callback)

    ### Reload event
    def reload(self):
        super(Auth, self).reload()
        self.permissions = defaultdict(list)
        self.permissions.update(self.config.get('perms', {}))
