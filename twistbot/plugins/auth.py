from collections import defaultdict
import functools

from ..pluginbase import BotPlugin

from .command import CommandPlugin

class Auth(CommandPlugin):
    """Auth plugin.

    Provides a reliable set of permissions other plugins can rely on. For
    certain irc events, installs a get_permissions() callback which can be used
    to query for permissions the user has.
    
    Depends on the command plugin for online permission tweaking.
    
    """
    def start(self):

        # Install a middleware hook for all irc events
        self.install_middleware("irc.on_*")

        # Install a few commands. These will call the given callback. This
        # functionality is provided by the CommandPlugin class, from which this
        # class inherits. The CommandPlugin will also check permissions.
        self.install_command("permission add (?P<name>\w+) (?<perm>\w+)",
                "auth.change_permissions",
                self.permisson_add)

        self.install_command("permission revoke (?P<name>\w+) (?<perm>\w+)",
                "auth.change_permissiosn",
                self.permission_revoke)

    def received_middleware_event(self, event):
        """For events that are applicable, install a handler one can call to
        see if a user has a particular permission.

        This way, auth permissions aren't checked until a plugin actually wants
        to verify identity.

        """

        if event.eventtype in [
                "irc.on_privmsg",
                "irc.on_notice",
                "irc.on_mode_changed",
                "irc.on_user_joined",
                "irc.on_action",
                "irc.on_topic_updated",
                ]:

            event.get_permissions = functools.partial(self._get_permissions, event.user, event.channel)

        return event


    def _get_permissions(self, user, channel):
        """Send a whois to the server or a AAC to nickserv to get the account
        that "user" is using, then lookup permissions based on that

        """
        raise NotImplementedError("Permission lookups are a TODO")


    def _save(self):
        # Make a copy... don't store the defaultdict (probably wouldn't matter though)
        self.config['permissions'] = dict(self.permissions)
        self.pluginboss.save()

    ### The command plugin callbacks, installed above

    def permission_add(self, event, name, perm):
        self.permissions[(name, event.channel)].add(perm)
        self._save()
        event.reply("Permission %s granted for user %s in %s" % (perm, name, event.channel))

    def permission_revoke(self, event, name, perm):
        try:
            self.permissions[(name, event.channel)].remove(perm)
        except KeyError:
            event.reply("User %s doesn't have permission %s in channel %s!" % (name, perm, event.channel))
        else:
            self._save()
            event.reply("Permission %s revoked for user %s in channel %s" % (perm, name, event.channel))

    ### Reload event
    def reload(self):
        super(Auth, self).reload()
        self.permissions = defaultdict(set)
        self.permissions.update(self.config.get('permissiosn', {}))
