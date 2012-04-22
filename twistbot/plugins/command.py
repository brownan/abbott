import re
from collections import namedtuple

from twisted.python import log

from ..pluginbase import BotPlugin
from ..transport import Event

CommandTuple = namedtuple("CommandTuple", ['re', 'permission', 'callback', 'prefix'])

class CommandPluginSuperclass(BotPlugin):
    """This class is meant to be a superclass of plugins that wish to use the
    command abstractions. It is NOT to be installed as a plugin itself.

    It provides several things:

    the install_command() function will listen for commands and dispatch them
    to the given callback functions. This dispatch automatically verifies
    authentication.

    This plugin overrides on_event_irc_on_privmsg() and start(), so if you
    implement either function in a subclass, be sure to call the superclass's
    method!

    Use of the command functionality in derived plugins requires the use of the
    auth.Auth plugin.

    """
    def __init__(self, *args, **kwargs):
        super(CommandPluginSuperclass, self).__init__(*args, **kwargs)

        commandconfig = self.pluginboss.config.get("command", {})
        self.__globalprefix = commandconfig.get("prefix", None)

        # A list of (regular expression object, permission, callback, prefix)
        # tuples
        self.__callbacks = []

    def start(self):
        self.listen_for_event("irc.on_privmsg")

    def install_command(self, formatstr, permission, callback, prefix=None):
        """Install a command.

        formatstr is a regular expression string. Its captured groups will be
        passed in to the callback function as described below.

        permission is a permission string that the user must have. Permission
        checking supports globs, so for example if the required permission is
        "plugin.perm" and the user has "plugin.*", they will be allowed.

        Globs transcend dot separators too, so you can require permissions for
        a particular channel with "plugin.permission.#channel". Some users can
        be given "plugin.permission.#channel" directly, while others could have
        "plugin.permission.*"

        "*" is effectively super-user and will match any given
        permission.

        if the given permission is None, every user matches.

        The callback parameter is a callable that is called upon the command
        when it is issued by a user with appropriate permissions.

        the prefix parameter, if not None, is a prefix to be added to the
        format string, overriding the global default. If prefix is None, the
        global default prefix is required.

        The global prefix is configured in config['command']['prefix']. For
        example, if the global prefix is "!", all commands must be prefixed
        with a "!". But individual commands may override this prefix.

        The callback format takes one fixed parameter: the event object. The
        object has one extra attribute inserted: reply. This is a callable that
        takes a string and will reply to the source of the message.

        The other arguments to the callback are the positional or named
        parameters from the regular expression captured groups. You must use
        either positional OR named capture groups, not both.

        """
        self.__callbacks.append(
                CommandTuple(re.compile(formatstr), permission, callback, prefix)
                )

    def on_event_irc_on_privmsg(self, event):
        # Check for all the different prefixes or ways a line could contain a
        # command. If one of them matches, dispatch into self._do_command()

        for callbacktuple in self.__callbacks:
            # the configured prefix
            if callbacktuple.prefix is not None and event.message.startswith(callbacktuple.prefix):
                msg = event.message[len(callbacktuple.prefix):].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self._do_command(match, callbacktuple, event)
                continue

            # The global prefix
            if self.__globalprefix is not None and event.message.startswith(self.__globalprefix):
                msg = event.message[len(self.__globalprefix):].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self._do_command(match, callbacktuple, event)
                continue

            # The current nickname plus a colon
            # dig deep to find the current nickname
            nick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname
            if event.message.startswith(nick + ":"):
                msg = event.message[len(nick)+1:].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self._do_command(match, callbacktuple, event)
                continue


    def _do_command(self, match, callbacktuple, event):
        """A user has issued a command. We still have to check permissions"""
        # Now create a reply method and add it to the event object

        def reply(msg):
            nick = event.user.split("!",1)[0]
            msg = "%s: %s" % (nick, msg)
            newevent = Event("irc.do_msg", user=event.channel, message=msg)
            self.transport.send_event(newevent)
        event.reply = reply

        if callbacktuple.permission is None:
            # This command does not require permission. No need to check the
            # user at all
            named_groups = match.groupdict()
            if named_groups:
                callbacktuple.callback(event, **named_groups)
            else:
                callbacktuple.callback(event, *match.groups())
            return


        # Check the user's permissions
        def check_permissions(perms):
            # User has permission perms, a list of permissions
            # We require the user to have callbacktuple.permission

            for perm in perms:
                # Does the user's permission `perm` permit
                # `callbacktuple.permission`?
                perm_parts = [re.escape(x) for x in perm.split("*")]
                perm_match_str = ".+".join(perm_parts)
                if re.match(perm_match_str, callbacktuple.permission):
                    # Successful match
                    log.msg("User %s is auth'd with %s to perform %s" % (event.user, perm, callbacktuple.callback))
                    named_groups = match.groupdict()
                    if named_groups:
                        callbacktuple.callback(event, **named_groups)
                    else:
                        callbacktuple.callback(event, *match.groups())


        deferred = event.get_permissions()
        deferred.addCallback(check_permission)
        
