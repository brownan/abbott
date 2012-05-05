import re
from collections import namedtuple
import random

from twisted.python import log
from twisted.internet import reactor
from twisted.internet import defer

from .pluginbase import BotPlugin

class _CommandGroup(object):
    """Internal object created by CommandPluginSuperclass.install_cmdgroup()

    """
    def __init__(self,
            grpname,
            cmdlist,
            cmdglist,
            prefix=None,
            permission=None,
            helptext=None,
            globalprefix=None,
            ):
        self.grpname = grpname
        self.cmdlist = cmdlist
        self.prefix = prefix
        self.permission = permission
        self.globalprefix = globalprefix

        if grpname:
            help_re = re.compile("(?:help )?(?:%s)?%s" % (
                prefix if prefix else "",
                re.escape(grpname),
                ))
        else:
            help_re = None

        helplines = []
        if helptext:
            helplines.append(helptext)

        # For the purposes of the helpstr, find the most specific prefix we can
        # (any will work though)
        if prefix is None:
            prefix = globalprefix
            if prefix is None:
                prefix = "{nickname}: "
        helplines.append("Usage: %s%s <subcommand> [arguments ...]" % (
            prefix,
            grpname,
            ))
        helplines.append("Use '%shelp %s <subcommand>' for more information on a subcommand" % (
            prefix,
            grpname,
            ))

        self.subcmds = []
        cmdglist.append(_CommandGroupTuple(
            grpname=grpname,
            helpre=help_re,
            helplines=helplines,
            subcmds=self.subcmds,
            ))

    def install_command(self,
            cmdname,
            callback,
            cmdmatch=None,
            cmdusage=None,
            argmatch=None,
            permission=None,
            prefix=None,
            helptext=None):
        """Install a command.

        cmdname is the name of the command, used in command listing and usage
        text

        callback is the callable to call when the command is invoked. It takes
        two arguments: the Event object from the request, and a re.Match object
        from the cmdmatch parameter.

        cmdmatch, if specified, is a regular expression string specifying how
        to match just the command name (not the arguments). If it is not
        specified, the cmdname string is used to match the command. It should
        not have any capturing parenthesis since this will be concatenated with
        the argument matching regular expression.

        cmdusage, if specified, is the usage text for the command's argumetns.
        For example, if the command takes two requried arguments and one
        optional argument, this should be set to something like "<arg1> <arg2>
        [arg3]"

        argmatch is a regular expression string that matches the arguments of
        this command. The resulting match object is passed in to the callback.
        If not specified, the command takes no arguments. You probably want to
        put a $ at the end of this if given, otherwise any trailing string will
        still match.

        permission, if given, is the permission required for a user to execute
        this command. The special string "%c" is replaced by the channel name.
        If not given, all users are allowed to execute this command.

        prefix is an additional prefix that may be given to have the bot
        recognize a command. For example, a "ban" command may want to specify a
        prefix of "." so the standard ban command works, even if "." isn't a
        normal command trigger for this bot.

        helptext, if given, is displayed after the usage in help messages as a
        short one-line description of this command.

        """
        # This is to support empty self.grpname for top-level commands
        if self.grpname:
            grpname = self.grpname + " "
        else:
            grpname = ""

        # Put together a regular expression string matching the command part of
        # the message
        command_str = "%s(?:%s)" % (
                re.escape(grpname),
                cmdmatch if cmdmatch else re.escape(cmdname),
                )

        # Now put together a regular expression string matching the entire
        # command plus arguments
        if argmatch:
            # command takes arguments
            # Explanation of the (?: |\b):
            # normally we want to match a space between the command and its
            # arguments, but to support possibly empty arguments or fancier
            # regular expressions, we also accept the empty string between the
            # command and its arguments as long as it's a word boundary. This
            # probably strictly isn't the right thing to do, but the only thing
            # this really disallows is having no boundry between the command
            # and its arguments.
            commandargs_str = command_str + r"(?: |\b)(?:%s)" % argmatch
        else:
            # command doesn't take arguments
            commandargs_str = command_str + "$"


        # Put together a regular expression string matching the entire command
        # plus the prefix. If this command doesn't give a prefix, go with the
        # group prefix.
        prefix = prefix if prefix is not None else self.prefix
        if prefix is not None:
            prefix_re = re.compile(re.escape(prefix) + commandargs_str)
        else:
            prefix_re = None

        # This should match the command without any arguments and an optional
        # "help" at the beginning. The \b at the end is so that the help text
        # for a command e.g. "reload" doesn't get triggered for trying to run a
        # command "reloadall"
        help_re = re.compile(r"(?:help )?(?:%s)?%s\b" % (
            re.escape(prefix) if prefix is not None else "",
            command_str,
            ))

        # At this point, `prefix` could be either the command's or the group's
        # prefix, but could be None if there is no special prefix. For the
        # purposes of the usage text, if the prefix is None, put either the
        # global prefix or the bot's nick here
        if prefix is None:
            prefix = self.globalprefix
            if prefix is None:
                # This will be replaced in __do_help(), since we don't want to
                # assume the nick won't change at runtime
                prefix = "{nickname}: "

        help_str = """Usage: %s%s%s %s\n%s""" % (
                prefix,
                grpname,
                cmdname,
                cmdusage if cmdusage else "",
                helptext if helptext else "No documentation provided (you're on your own!)",
                )

        self.cmdlist.append(_CommandTuple(
            cmdname="%s%s" % (grpname, cmdname),
            permission=permission if permission else self.permission,
            commandre=re.compile(commandargs_str),
            prefixre=prefix_re,
            helpre=help_re,
            callback=callback,
            helplines=help_str.split("\n"),
            ))
        self.subcmds.append(
                (cmdname,permission if permission else self.permission)
                )


_CommandTuple = namedtuple("_CommandTuple", [
    "cmdname",
    "permission",
    "commandre",
    "prefixre",
    "helpre",
    "callback",
    "helplines"
    ])
_CommandGroupTuple = namedtuple("_CommandGroupTuple", [
    "grpname",
    "helpre",
    "helplines",
    "subcmds",
    ])

class CommandPluginSuperclass(BotPlugin):
    """This class is meant to be a superclass of plugins that wish to use the
    command abstractions. It is NOT to be installed as a plugin itself.

    It provides several things:

    the install_command() function will install a command. This means the
    plugin will listen to incoming irc.on_privmsg events, determine if it is a
    command directed at this bot, verify permissions, and then call the
    callback. See the documentation for the Command() class.

    This plugin overrides on_event_irc_on_privmsg(), reload() and start(), so
    if you implement these functions in a subclass, be sure to call the
    superclass's method!

    Use of the permissions in installed commands requires the use of the
    auth.Auth plugin.

    The global prefix is configured in config['command']['prefix']. For
    example, if the global prefix is "!", all commands must be prefixed with a
    "!". But individual commands may override this prefix.

    """

    def __init__(self, *args, **kwargs):
        super(CommandPluginSuperclass, self).__init__(*args, **kwargs)

        # List of _CommandTuple objects installed by install_command()
        # functions
        self.__cmds = []

        # List of _CommandGroupTuple objects There is always one top-level
        # object. Others are installed with self.install_cmdgroup()
        self.__cmdgs = []

        # put this function here as a way to define top-level commands
        self.install_command = self.install_cmdgroup(
                grpname="",
                ).install_command

    @property
    def cmdgs(self):
        return self.__cmdgs

    def start(self):
        super(CommandPluginSuperclass, self).start()
        self.listen_for_event("irc.on_privmsg")

    def reload(self):
        super(CommandPluginSuperclass, self).reload()
        commandconfig = self.pluginboss.config.get("command", {})
        self.__globalprefix = commandconfig.get("prefix", None)

    def install_cmdgroup(self,
            grpname,
            prefix=None,
            permission=None,
            helptext=None,
            ):
        return _CommandGroup(
                grpname=grpname,
                cmdlist=self.__cmds,
                cmdglist=self.__cmdgs,
                prefix=prefix,
                permission=permission,
                helptext=helptext,
                globalprefix=self.__globalprefix,
                )

    def on_event_irc_on_privmsg(self, event):
        """Checks to see if this is a command. If so, dispatch to the command
        handler as appropriate.

        """
        # dig deep to find the current nickname; we use it in a couple checks
        # below
        nick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname

        # First see if this looks like a command. A command takes the form of
        # <botname>: <command>
        # or
        # <global prefix> <command>
        message = event.message

        nickprefix = nick + ":"
        globalprefix = self.__globalprefix.strip()
        if message.startswith(nickprefix):
            message = message[len(nickprefix):].strip()
        elif globalprefix and message.startswith(globalprefix):
            message = message[len(globalprefix):].strip()
        elif event.direct:
            # Don't require a prefix if this was sent in a direct message to me
            message = message
        else:
            # Don't match the command by itself... we require a prefix (but
            # don't return just yet, there could be a command-specific prefix
            # that could still match)
            message = None

        # Look through all our defined commands to see if any match
        for cmd in self.__cmds:
            m = cmd.commandre.match(message) if message else None
            if m:
                self.__do_command(event, cmd, m)
                return
            if cmd.prefixre:
                m = cmd.prefixre.match(event.message.strip())
                if m:
                    self.__do_command(event, cmd, m)
                    return
            if message and cmd.helpre.match(message):
                self.__do_help(event, cmd)
                return

        # No commands or help for a specific command matched, now check for a
        # match on help for a command group. These checks are done in reverse
        # order so that we always display the most specific help text we can.
        for cmdg in reversed(self.__cmdgs):
            if cmdg.helpre and message and cmdg.helpre.match(message):
                self.__do_help(event, cmdg)
                return

    @defer.inlineCallbacks
    def __do_command(self, event, cmd, match):
        """A user has issued command `cmd` and it matched with regular
        expression Match object `match`.

        This method's job is to check permissions and dispatch.

        """

        if (yield event.has_permission(cmd.permission, event.channel)):
            log.msg("User %s is auth'd to perform %s" % (event.user, cmd.cmdname))
            cmd.callback(event, match)
        else:
            log.msg("User %s does not have permission for %s" % (event.user, cmd.cmdname))
            replies = self.pluginboss.config.get('command',{}).get('denied_msgs',[])
            if not replies or event.direct:
                event.reply(notice=True, direct=True,
                        msg="Sorry, you don't have access to that command")
            else:
                reactor.callLater(random.uniform(0.5,3), event.reply, random.choice(replies), userprefix=False, notice=False)

    @defer.inlineCallbacks
    def __do_help(self, event, cmd):
        """Send to the user help info about this command"""
        nick = self.pluginboss.loaded_plugins['irc.IRCBotPlugin'].client.nickname
        if hasattr(cmd, "subcmds"):
            # This is a command group
            for line in cmd.helplines:
                event.reply(notice=True, direct=True,
                        msg=line.replace("{nickname}", nick))

            cmds_with_access = []
            cmds_with_global_access = []

            for subcmd in cmd.subcmds:
                where = (yield event.where_permission(subcmd[1]))
                if None in where:
                    cmds_with_global_access.append(subcmd[0])
                elif where:
                    cmds_with_access.append(subcmd[0])

            if not cmds_with_access and not cmds_with_global_access:
                event.reply(notice=True, direct=True,
                    msg="You don't have access to any of these commands, however",
                    )
            else:
                if cmds_with_global_access:
                    event.reply(notice=True, direct=True,
                            msg="You have access to these subcommands: %s" % (
                                ", ".join(cmds_with_global_access)))
                if cmds_with_access:
                    event.reply(notice=True, direct=True,
                            msg="You have access to these subcommands in select channels: %s" % (
                                ", ".join(cmds_with_access)))

        else:
            # A regular command
            for line in cmd.helplines:
                event.reply(notice=True, direct=True,
                        msg=line.replace("{nickname}", nick))
            where = (yield event.where_permission(cmd.permission))
            if None in where:
                event.reply(notice=True, direct=True,
                        msg="You have global access to this command and can run it anywhere")
            elif where:
                event.reply(notice=True, direct=True,
                        msg="You can run this command in these channels: %s" % (
                            ", ".join(where),
                            ))
            else:
                event.reply(notice=True, direct=True,
                        msg="You don't have access to this command. Get out of here you!")
