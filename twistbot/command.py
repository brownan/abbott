import re
from collections import namedtuple

from twisted.python import log

from .pluginbase import BotPlugin
from .transport import Event

CommandTuple = namedtuple("CommandTuple", ['re', 'permission', 'callback'])

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

    def start(self):
        super(CommandPluginSuperclass, self).start()
        self.listen_for_event("irc.on_privmsg")

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

        This is the preferred way to install "help" commands.

        """
        self.__catchalls.append(
                CommandTuple(re.compile(formatstr), permission, callback)
                )

    def help_msg(self, formatstr, helpstr, permission=None):
        """A helpful shortcut for help messages that installs a catchall
        callback that simply replies with the given help string.
        """
        def callback(event, match):
            event.reply("Usage: " + helpstr)
        self.install_catchall(formatstr, permission, callback)

    def on_event_irc_on_privmsg(self, event):
        # Check for all the different prefixes or ways a line could contain a
        # command. If one of them matches, dispatch into self.__do_command()

        # dig deep to find the current nickname; we use it in a couple checks
        # below
        nick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

        for callbacktuple in self.__callbacks:
            # If it was a private message, match against the entire message and
            # disregard the other ones
            if event.channel == nick:
                match = callbacktuple.re.match(event.message)
                if match:
                    self.__do_command(match, callbacktuple, event)
                continue

            # the configured prefix
            if self.prefix is not None and event.message.startswith(self.prefix):
                msg = event.message[len(self.prefix):].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self.__do_command(match, callbacktuple, event)

            # The global prefix
            if self.__globalprefix is not None and event.message.startswith(self.__globalprefix):
                msg = event.message[len(self.__globalprefix):].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self.__do_command(match, callbacktuple, event)

            # The current nickname plus a colon
            if event.message.startswith(nick + ":"):
                msg = event.message[len(nick)+1:].strip()
                match = callbacktuple.re.match(msg)
                if match:
                    self.__do_command(match, callbacktuple, event)


    def __do_command(self, match, callbacktuple, event):
        """A user has issued a command. We still have to check permissions"""
        # Now create a reply method and add it to the event object

        def reply(msg, userprefix=True, notice=False):
            if notice:
                eventname = "irc.do_notice"
            else:
                eventname = "irc.do_msg"
            nick = event.user.split("!",1)[0]
            
            mynick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

            if userprefix and mynick != event.channel:
                # Never prefix the user if this is a PM, otherwise obey the
                # request or the default
                msg = "%s: %s" % (nick, msg)

            # If it was sent to us directly (channel == mynick) then send it
            # directly back. Otherwise send it to the originating channel
            channel = event.channel if event.channel != mynick else nick

            newevent = Event(eventname, user=channel, message=msg)
            self.transport.send_event(newevent)
        event.reply = reply

        if callbacktuple.permission is None:
            # This command does not require permission. No need to check the
            # user at all
            callbacktuple.callback(event, match)
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
                    callbacktuple.callback(event, match)
                    break
                else:
                    log.msg("User %s does not have permissions for %s" % (event.user, callbacktuple.callback))


        deferred = event.get_permissions()
        deferred.addCallback(check_permissions)
        
