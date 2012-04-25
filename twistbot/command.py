# encoding: UTF-8
import re
from collections import namedtuple
import random
from itertools import chain

from twisted.python import log
from twisted.internet import reactor

from .pluginbase import BotPlugin
from .transport import Event

CommandTuple = namedtuple("CommandTuple", ['re', 'permission', 'callback'])

def has_permission(user_perms, required_perm):
    """If one of the user's permissions grant access to required_perm, return
    True. Otherwise, return False

    """
    if required_perm is None:
        return True
    for perm in user_perms:
        # does perm permit required_perm?
        # Turn it into a regular expression where * captures one or more
        # characters
        perm_parts = [re.escape(x) for x in perm.split("*")]
        perm_match_str = ".+".join(perm_parts)
        if re.match(perm_match_str, required_perm):
            return True
    return False

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

    the prefix attribute, if not None, is a prefix to be added to the format
    string for commands to this class, overriding the global default. If prefix
    is None, the global default prefix is required.

    The global prefix is configured in config['command']['prefix']. For
    example, if the global prefix is "!", all commands must be prefixed with a
    "!". But individual commands may override this prefix.

    """
    prefix = None

    def __init__(self, *args, **kwargs):
        super(CommandPluginSuperclass, self).__init__(*args, **kwargs)

        commandconfig = self.pluginboss.config.get("command", {})
        self.__globalprefix = commandconfig.get("prefix", None)

        # A list of (regular expression object, permission, callback, prefix)
        # tuples
        self.__callbacks = []
        self.__catchalls = []

        self.commandlist = []

    def start(self):
        super(CommandPluginSuperclass, self).start()
        self.listen_for_event("irc.on_privmsg")

    def define_command(self, cmdname):
        """This is purely for the help plugin so it knows what commands this
        plugin defines. Call this for each top level command and it will be
        listed in the output of the "help" command if the help plugin is
        installed
        """
        self.commandlist.append(cmdname)

    def install_command(self, formatstr, permission, callback):
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


        The callback format takes two parameters: the event object and the
        regular expression match object of the matched command. The event
        object has one extra attribute inserted: reply. This is a callable that
        takes a string and will reply to the source of the message.

        """
        self.__callbacks.append(
                CommandTuple(re.compile(formatstr), permission, callback)
                )

    def install_catchall(self, formatstr, permission, callback):
        """This method installs a command in the same way that
        install_command() does, with the exceptions that commands installed
        with this will *only* be called if no other command from this plugin is
        matched.

        This is used primarily for "help" messages. See help_msg() for a more
        convenient way to define help messages.

        """
        self.__catchalls.append(
                CommandTuple(re.compile(formatstr), permission, callback)
                )

    def help_msg(self, formatstr, permission, helpstr):
        """A helpful shortcut for help messages that installs a catchall
        callback that simply replies with the given help string.

        This is intended to catch commands that don't have all the paramters
        specified. For example, if your command is:

            mycommand (?P<opt1>\w+) (P<opt2>\w+)$

        Then you would probably want to define a help message that matches::

            mycommand

        that way any command starting with "mycommand" that doesn't match the
        real command will display the help.

        This method also adds an implicit (help )? to the beginning of the
        format string, so that one can explicitly request the help for a
        command. This is especially useful for commands that don't take
        parameters.
        """
        def callback(event, match):
            """This function gets called when a user issues a command that
            doesn't match any installed commands but does match `formatstr`.

            """
            event.reply("Usage: " + helpstr)
        # Always display the help, even if the user doesn't have the
        # permissions. In the future we may display a different help text if
        # the user doesn't have permissions.
        self.install_catchall("(help )?(?:" + formatstr+")", None, callback)

    def on_event_irc_on_privmsg(self, event):
        """When a message comes in, we check if it matches against any
        installed commands. If so, call self.__do_command() to check
        permissions and then call the command's installed callback.

        This checks each command, and then each fallback command. If a command
        matches, the others are disregarded; only the first matching command is
        executed.

        """
        # Check for all the different prefixes or ways a line could contain a
        # command. If one of them matches, dispatch into self.__do_command()

        # dig deep to find the current nickname; we use it in a couple checks
        # below
        nick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

        for callbacktuple in chain(self.__callbacks, self.__catchalls):
            # If it was a private message, match against the entire message and
            # disregard the other ones
            if event.channel == nick:
                match = callbacktuple.re.match(event.message)
                if match:
                    self.__do_command(match, callbacktuple, event)
                    return
                continue

            # the configured prefix
            if self.prefix is not None and event.message.startswith(self.prefix):
                msg = event.message[len(self.prefix):].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self.__do_command(match, callbacktuple, event)
                    return

            # The global prefix
            if self.__globalprefix is not None and event.message.startswith(self.__globalprefix):
                msg = event.message[len(self.__globalprefix):].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self.__do_command(match, callbacktuple, event)
                    return

            # The current nickname plus a colon
            if event.message.startswith(nick + ":"):
                msg = event.message[len(nick)+1:].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self.__do_command(match, callbacktuple, event)
                    return


    def __do_command(self, match, callbacktuple, event):
        """Some user has issued a command, and the command matched a registered
        command for this plugin.

        We still don't know if the user is auth'd, or what permissions the user
        has, so this method does both of those things before calling the
        callback function registered for this command.

        This function boils down to calling the get_permissions() function that
        the Auth plugin has inserted into the event object, and providing the
        resulting deferred a callback that checks the permission and either
        calls the command's callback or leaves a witty response.
        
        """

        if callbacktuple.permission is None:
            # This command does not require permission. No need to check the
            # user at all
            callbacktuple.callback(event, match)
            return


        # Check the user's permissions
        def check_permissions(perms):
            # User has permission perms, a list of permissions
            # We require the user to have callbacktuple.permission
            if has_permission(perms, callbacktuple.permission):
                log.msg("User %s is auth'd to perform %s" % (event.user, callbacktuple.callback))
                callbacktuple.callback(event, match)
            else:
                log.msg("User %s does not have permissions for %s" % (event.user, callbacktuple.callback))
                replies = self.pluginboss.config.get('command',{}).get('denied_msgs',[])
                if not replies:
                    replies = ["Sorry, you don't have access to that command"]
                reactor.callLater(random.uniform(0.5,2), event.reply, random.choice(replies), userprefix=False, notice=False)


        deferred = event.get_permissions()
        deferred.addCallback(check_permissions)
        
