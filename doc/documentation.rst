====================
Abbott Documentation
====================

Abbott is a generic plugin framework designed with support in mind for an IRC
bot.  The core of Abbott consists of a general plugin loading and
inter-communication mechanism, and a mechanism for plugins to save persistent
data. All bot functionality is provided by plugins, the core is just there to
tie all plugins together.

Abbott is built on Twisted, and heavily uses twisted concepts. For example, the
entire bot is single threaded; plugins are expected not to block for IO or any
other reason. Rather, they are expected to make use of Twisted deferreds and
the Twisted reactor's callLater() function, as well as Twisted's facilities
for IO.

For those that aren't familiar, Twisted deferreds are, put simply, a promise
that a value will exist at a later time. They are a kind of monad. If a
function returns a deferred, it typically means that the function will provide
its value at a later time through the deferred. The caller assigns a callback
function to the deferred, which will be called when the value is ready. Plugins
make heavy use of the defer.inlineCallbacks decorator to simplify the common
pattern of pausing a function to wait for a deferred, then resuming the
function. For more information, consult the Twisted docs on deferreds.

Plugin API
==========

Plugins typically inherit from the pluginbase.BotPlugin type, and should live
in a module within the abbott.plugins package. Plugins should not typically
override the constructor (but can still be useful to initialize values before
any other method is called). The constructor takes three parameters: the plugin
name, the Transport object, and the PluginBoss object (see below for about the
Transport and PluginBoss objects).

The default constructor assigns the transport and pluginboss to the local
attributes by the same names, and calls the reload() method.

The following three methods are required of every plugin:

start() takes no parameters, and is called once when the plugin has been
loaded. This is the usual place to do any initialization and set up all the
callbacks that the plugin wants to listen to.

stop() takes no parameters, and is called once when the plugin is to be
unloaded. This method should cancel any timed, recurring, or deferred events as
appropriate. Transport hooks are unhooked automatically and do not need to be
handled here. If nothing needs to be done, a plugin may leave this method
un-overridden.

reload() takes no parameters, and is called whenever an external event signals
to the plugin that its configuration has changed in the persistent store. The
plugin's job for this method is to make any necessary changes to its runtime in
response to the new configuration. The default implementation of reload() is to
retrieve the new config and assign it to the local attribute “config”. If this
is sufficient, then there is no reason to override this method.

There are also the following methods, described in the Transport section of
this document: received_event(), received_middleware_event(),
incoming_request(), install_middleware(), listen_for_event(),
provides_request().

Plugin Boss
===========

The plugin boss is a core class. One instance exists per invocation of Abbott,
and it handles loading, unloading, and reloading of plugins, as well as
handling persistent data for the plugins. Plugins are provided the plugin boss
instance as the third parameter to their constructor and it's typically
assigned to a plugin's “pluginboss” attribute.

There is one method of the plugin boss of special interest to plugins: the
get_plugin_config() method. It is called with a single parameter: the plugin
name (provided as the first argument to the constructor and typically set to
the “plugin_name” attribute), to retrieve the persistent data for this plugin.
The default implementation of a plugin's reload() method calls
get_plugin_config() and assigns the result to the “config” attribute.

Config object
-------------

The config returned by get_plugin_config() is an object that implements the
dictionary interface, and has an additional method: save(), which saves the
config to disk. Anytime a change is made to the config, save() should be
called.

Any basic type can be stored in a config: dictionaries, lists, strings (unicode
only), floats, ints. Implementation note: config is saved with JSON so only
JSON types are allowed.

Transport Layer
===============

The transport layer provides inter-plugin communication for all Abbott plugins,
and plugins are expected to exclusively use the provided transport layer for
all communication with other plugins. Every plugin is provided an instance of a
Transport object as the second parameter to its constructor and it's
typically assigned to a plugin's “transport” attribute.

The transport layer has two ways for plugins to communicate. Both are similar,
but are intended for different situations and uses. One is the *Event* system,
where plugins can broadcast events to any other plugin, and the other is the
*Request* system where a plugin can *request* some value or action of another
plugin.

Events
------

An event is a generic broadcast from any plugin to any other plugin. That is,
any plugin can broadcast any event, and any plugin can listen for any event. It
can be thought of as *something has happened* and plugins to which the event is
relevant can react to it, such as an incoming IRC message. However, it can also
be used for outgoing events, such as outgoing IRC messages (which are broadcast
by any plugin and listened for by the IRC plugin).

Events are named by a string, with two components separated by a dot. By
convention, the first section is a general category of message, and the second
is the specific event. For example, ”irc.on_privmsg” is broadcast by the IRC
plugin when a privmsg is received by the server.

Plugins can listen for an event normally, or as “middleware”. When an event
enters the transport object, all middleware handlers are called, and then all
normal handlers are called. Middleware handlers are allowed to modify or even
swallow an event, so they are handy for providing additional functionality,
either transparently, or functionality that other plugins are aware of and can
use (such as adding new attributes to an event).

Event API
`````````

The following methods are provided on the base BotPlugin for communicating with
the transport's Event system.

install_middleware(matchstr)
listen_for_event(matchstr)
    These two methods install the plugin as a listener for the specified event.
    Events may be globbed, such as “irc.*” to listen for all irc events. A
    plugin will typically call one or more of these methods in its start()
    method, such as self.listen_for_event("irc.*")
    
received_event(event)
received_middleware_event(event)
    These two methods are called by the transport layer when events that were
    requested earlier were fired by some plugin. The parameter is the event
    object that was sent. See below for information about Event objects.
    Middleware handlers are expected to return the (possibly modified) event
    object, or None to indicate the event is to be swallowed and no more
    handlers called.
    
    The default implementation of these methods dispatch to a method whose name
    is derived from the event name. For events, this is on_event_%s() and for
    middleware it's on_middleware_%s() where %s is the event name with dots
    replaced with underscores. For example, the event irc.on_privmsg calls
    on_event_irc_on_privmsg(). Plugins may override this behavior if they wish.
    
To send an event, a plugin must first make a new instance of an Event object
(abbott.transport.Event). Event objects take one positional parameter: the
event name, and zero or more keyword parameters: the event attributes. Each
keyword parameter is assigned directly to the object's attributes, so
Event("some.event", attr=1) will have event.attr == 1. Event objects are just a
container.

Once an Event object is constructed, it is sent by passing it to the
transport's send_event() method. Recall that the transport object is assigned
to the “transport” attribute of each plugin, so plugins typically call
self.transport.send_event(eventobj). Or, to be more concise:
self.transport.send_event(Event("event.name", ...))

Requests
--------

Requests are similar to events. The difference is that only one plugin is
allowed to listen for a particular request, and request handlers are expected
to return a twisted deferred that will fire at some point. Requests are meant
to be a way of requesting from a plugin some data or some action that returns
data.

Requests are named the same as events: with a string made of two components
separated by a dot. Requests and Event names do not share a namespace.

Requests API
````````````

The following methods are provided on the base BotPlugin for communicating with
the transport's Request system.

provides_request(name)
    Indicates the plugin will provide a handler for the given request name.
    Note that the name cannot be globbed here; a plugin must declare every
    request name it wishes to handle.
    
incoming_request(name, \*args, \**kwargs)
    Called when another plugin has issued a request by the given name. args and
    kwargs are as passed by the caller. This function is expected to return a
    deferred, and is an error to return anything else.
    
    The default implementation of this method dispatches to a method whose name
    is derived from the request name. The format is on_request_%s() where %s is
    the request name with periods replaced by underscores.
    
To issue a request, a plugin should call transport.issue_request(name, \*args,
\**kwargs). The args and kwargs are passed as-is and are defined by which
request is being called.

Command Plugins
===============

The CommandPluginSuperclass is a subclass of BotPlugin that plugins themselves
may subclass to gain lots of boilerplate code to handle IRC commands. Plugins
that derive from CommandPluginSuperclass depend on several IRC-related plugins
in order to function, thus is tightly integrated with IRC. A future feature
would be to abstract a command interface away from IRC and have an IRC-command
connector so that other data sources can interface with the same command
plugins, but since Abbott was built to be an IRC bot, the command plugins
tightly integrate with the IRC plugins right now.

The CommandPluginSuperclass allows plugins to declare in their start() method
commands that they provide, and the superclass automatically handles analyzing
incoming lines that look like commands, parsing them, dispatch, permissions,
and automatic help text.

Plugins that derive from CommandPluginSuperclass (hereby called “command
plugins”) declare commands they provide by calling self.install_command() for
each command they provide. This is typically done in the start() method, but
make sure to call super(my_command_plugin, self).start() since the super class
also has some stuff in start().

Here are the parameters for install_command(). All except cmdname and callback
are optional.

cmdname
    A string indicating the command name itself. This is used in the help
    listing and, unless cmdmatch is specified, is also how you invoke the
    command.
    
callback
    The callable to call when the command is invoked. The callable gets two
    parameters: the event that initiated the command, and a re.Match object,
    used to retrieve the parameters.
    
cmdmatch
    An optional regex string specifying how to match the command name. This is
    useful if you want to specify an alias, so you can do something like
    "cmdname|cmdalias". It should not begin with a ^ or end with a $, since it
    is combined with other regexes to form a complete pattern.
    
cmdusage
    A string to explain the arguments of this command. Something like
    “<required arg> [optional arg] ...”
    
argmatch
    A regular expression that matches the *arguments* of this command. It
    should not include the command name, and it *should* end in a dollar sign
    unless you know what you're doing.
    
permission
    A permission string that is required for this command to succeed. If this
    is None or not specified, then everyone can invoke this command. See below
    for about the permission system.
    
prefix
    Commands are typically invoked with a prefix. For example, if the prefix is
    ! then a command is invoked by saying “!cmdname”. By default, commands use
    the globally defined prefix. If you wish to also add another prefix to
    invoke this command, add it here.
    
helptext
    Text to say along with the usage text for this command in the help output.
    This ought to explain what the command does.
    
    
In addition to defining commands, plugins may define command *groups*. A
command group is a way of logically grouping commands and not polluting the
global command namespace. Grouped commands are invoked with::

    !groupname commandname [args] ...
    
(assuming ! is the prefix). To declare a group, invoke self.install_cmdgroup()
in the start() method. This takes the following arguments

grpname
    The name of this command group

prefix
    Assign a custom prefix for all commands in this group
    
permission
    Assign a *default* permission for all commands in this group. You can still
    override this for individual commands.
    
helptext
    Text to display when showing help information for this group.
    
the install_cmdgroup() method returns a group object. Plugins then invoke the
group.install_command() method to install group commands (with the same
parameters and details as the above install_command() method)

Permissions
===========

Authentication and authorization are provided by the stock plugin auth.Auth. It
is used by command plugins, and so it's worth explaining it here instead of
with the auth plugin.

Commands can declare a permission that is required in order to invoke it.
Permissions are strings that are made of components separated by dots. They are
somewhat heirarchical and can contain as many components as you wish. Users are
granted permission to e.g. “perm.action” and then may perform commands that are
listed as requiring permission “perm.action”.

Users may be granted a globbed permission, such as “perm.*” to grant all
subpermissions of perm. Users may also be assigned the permission “perm”
directly, and they get all sub-permissions of that permission. The difference
is those users will also be granted permission to use commands assigned just
“perm”, while users with “perm.*” cannot invoke commands with just “perm”.

Globs do not transcend dots, so permissions such as “perm.*.asdf” are also
possible, although I have yet to think of a good use for that pattern.

Permissions are assigned on a per-channel basis, or globally. The super-user
permission is simply “*”. Also, default permissions can be granted that apply
to all users regardless of their authentication or identification.

Plugins
=======

This section lists each plugin that comes with abbott, what they do, and the
events and requests they each provide / listen for / react to.

irc.IRCBotPlugin
----------------

This is the main IRC bot plugin. It handles connecting to an IRC server, and
acts as a connector between the server and the Abbott transport layer: messages
from the server are relayed as events, and events are relayed to the server.

A note about unicode: this plugin correctly handles unicode for all string
parameters to its events. It WILL pass unicode objects to events that it emits,
and correctly handles unicode objects to events it listens for. For outgoing
lines, unicode control characters are stripped out except for a small whitelist
that includes standard IRC color codes and CTCP codes.

Also implements rate limiting for messages to the server. If 5 lines are sent
to the server in less than 2 seconds, then a rate limit of 1 line every 2
seconds is set until no lines have been sent for 2 seconds.

All event emitted take the form irc.on_* and all events that are listend for
take the form irc.do_*.

Events emitted
``````````````

Event("irc.on_join", channel)
    Emitted when a channel is joined. The channel parameter is the name of the
    channel joined.
    
Event("irc.on_part", channel)
    Emitted when we part a channel.
    
Event("irc.on_privmsg", user, channel, message, direct)
    Emitted when we receive a PRIVMSG from the server.
    
    user
        The user that sent the message
        
    channel
        The channel that the message was received on
        
    message
        The message content
        
    direct
        this is a boolean that is set to True when channel is equal to the
        bot's nickname, indicating this was sent directly to the bot instead of
        seen on a channel.
        
Event("irc.on_notice", user, channel, message)
    Same as privmsg but for NOTICE messages. The lack of a direct parameter is
    probably an oversight.
    
Event("irc.on_mode_change", user, channel, set, mode, arg)
    Emitted when we witness a mode change on a channel.
    
    user
        the user that instigated the mode change
        
    channel
        the channel where the mode changed
        
    set
        True or False whether the mode was set or unset
        
    mode
        A single character indicating what mode was changed
        
    arg
        The argument, or None if this mode doesn't take an argument
        
Event("irc.on_user_joined", user, channel)
    Emitted when we witness a user join a channel. Arguments are
    self-explanatory.
    
Event("irc.on_user_part", user, channel)
    Emitted when we witness a user part a channel.
    
Event("irc.on_user_quit", user, message)
    Emitted when we witness a user quit
    
Event("irc.on_user_kick", kickee, channel, kicker, message)
    Emitted when we witness a user being kicked.
    
Event("irc.on_action", user, channel, data)
    Emitted when a user performs an action on a channel
    
Event("irc.on_topic_updated", user, channel, newtopic)
    Emitted when a user updates a topic on a channel
    
Event("irc.on_nick_change", oldnick, newnick)
    Emitted when we witness a user change nicks
    
Event("irc.on_unknown", prefix, command, params)
    Emitted on events which *twisted* doesn't have a handler for. This is
    sort-of a catch-all, but this is not necessarily all IRC messages which we
    don't have handlers for. Check the twisted code for exactly which messages
    are caught with irc_unknown().
    
Events Listened for
```````````````````

Send these events from your plugin to do something in IRC! Note that for events
that fail (such as doing things that require OP without OP), there is no way to
tell if it succeeded or failed unless you listen for the appropriate failure
message from the server, which will be emitted via irc.on_unknown.

Event("irc.do_join_channel", channel)
    Join the specified channel
    
Event("irc.do_leave_channel", channel)
    Leave the channel
    
Event("irc.do_kick", channel, user, reason)
    Kick the user from the channel (bot must have OP or this will silently
    fail, unless you are listening for the appropriate failure message that
    will probably come through irc.on_unknown)
    
    reason is optional
    
Event("irc.do_invite", user, channel)
    Sends an invite to the user for the given channel.
    
Event("irc.do_topic", channel, topic)
    Attempts to set the channel topic. This will silently fail if the bot is
    not OP or the channel is not +t.
    
Event("irc.do_mode", channel, set, modes, limit, user, mask)
    Attempt to perform a mode change on the channel. Set is a boolean, and
    modes is the modestring to set or unset.
    
    You must specify one of limit, user, or mask for modes that take
    parameters. See twisted.words.protocols.irc.IRCClient.mode for details.
    
Event("irc.do_say", channel, message, length)
    Send a message to a channel. This is just like do_msg below except channel
    must be a channel, not a user.
    
    length is optional and indicates the maximum length of a single line.
    messages beyond that will automatically be sent as multiple messages. If
    not specified, a good value is estimated and used automatically.
    
Event("irc.do_msg", channel, message, length)
    The standard interface to IRC's PRIVMSG. user is either a nick or a
    channel. length is the same as irc.do_say above.
    
Event("irc.do_notice", user, message)
    Sends a NOTICE to the given user (a nick or channel).
    
Event("irc.do_away", message)
    Sets the client as away with the given message
    
Event("irc.do_back")
    Clears the client's away status
    
Event("irc.do_whois", nickname, server)
    Issues a whois to the server for the given nickname.
    
    server is optional as per the IRC protocol.
    
    Note that responses are emitted through a series of irc.on_unknown events.
    
Event("irc.do_setnick", nickname)
    Issues a request to change the client's nickname.
    
Event("irc.do_quit", message)
    Issues a QUIT message to the server with the optional message.
    
Event("irc.do_raw", line)
    Sends a raw line to the server. Trailing newline is not required. This can
    be used to send non-standard messages such as freenode's REMOVE command, or
    commands that twisted simply doesn't know about such as NAMES.
    
Requests Provided
`````````````````
irc.getnick
    Deferred fires immediately with the bot's current nickname
    
IRCController
-------------

A command plugin that 
