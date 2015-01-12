# encoding: UTF-8


import json
import os
import os.path
import sys
from collections import defaultdict
try:
    from UserDict import UserDict
except:
    # This moved in python 3
    from collections import UserDict
from functools import wraps

from twisted.internet import defer
from twisted.internet import reactor
from twisted.python import log

class PluginConfig(UserDict):
    """Installed in plugins as self.config. Provides a dictionary-like
    interface with a method .save() to save to persistent storage. Uses a json
    file as a backing store.

    """
    def __init__(self, jsonfile):
        """Initialize a config from a json file."""
        self._jsonfile = jsonfile
        with open(jsonfile, 'r') as inp:
            self.data = json.load(inp)

    def save(self):
        with open(self._jsonfile+"~", 'w') as out:
            json.dump(self.data, out, indent=4)
        os.rename(self._jsonfile+"~", self._jsonfile)


class PluginBoss(object):
    """Handles the loading and unloading of plugins and the reading 
    of config files and storage of configuration.

    There is one instance of this class per bot, and every plugin instance has
    a handle to it
    """
    def __init__(self, config, transport):
        self._configdir = config
        self._transport = transport

        self._filename = os.path.join(config, "config.json")

        self.loaded_plugins = {}

        if not os.path.exists(self._configdir):
            os.mkdir(self._configdir)
        elif not os.path.isdir(self._configdir):
            print("The config parameter should be a directory. Please make the necessary adjustments")
            sys.exit(1)
        
        try:
            self._load()
        except IOError:
            self._seed_defaults()

    def _seed_defaults(self):
        print("""\
It seems your config file doesn't exist or is unreadable.
I'll create a new one for you now""")
        server = raw_input("First, what irc server do you want to connect to? [irc.freenode.net] >")
        if not server.strip():
            server = "irc.freenode.net"
        prompt = "Uh huh, and what port would you like? SSL please. [7000] >"
        while True:
            port = raw_input(prompt)
            if not port.strip():
                port = 7000
            try:
                port = int(port)
            except ValueError:
                prompt = "That doesn't seem to be a number. I need a port /number/! [7000] >"
            else:
                break

        while True:
            admin = raw_input("And who should be the admin of this bot? This should be the nickserv account >")
            admin = admin.strip()
            if admin:
                break

        while True:
            nick = raw_input("What should the bot's nickname be? >")
            nick = nick.strip()
            if nick:
                break

        while True:
            channel = raw_input("Any particular channel I should join to begin with? >")
            channel = channel.strip()
            if channel:
                break


        print("Alright, I can take it from here. All other commands you can issue to the bot at runtime")

        self.config = {
                'core': {
                    'plugins': [
                        # Good default set of plugins to bootstrap functionality
                        'irc.IRCBotPlugin',
                        'irc.IRCController',
                        'ircutil.ReplyInserter',
                        # whois and names probably aren't necessary unless
                        # you're also running the ircadmin plugins, but just
                        # in case, it can't hurt.
                        'ircutil.IRCWhois',
                        'ircutil.Names',
                        'auth.Auth',
                        'plugincontroller.PluginController',
                        'corecontrol.CoreControl',
                        'corecontrol.Help',
                        ],
                    },
                'plugin_config': {
                    'irc.IRCBotPlugin': {
                        'server': server,
                        'port': port,
                        'nick': nick,
                        'realname': 'Bot operated by' + admin,
                        'channels': [channel],
                        # REMOVE is supported on freenode. I don't know about any others.
                        'remove': "freenode" in server,
                        },
                    'auth.Auth': {
                        'perms': {
                                admin: [
                                    [None, '*'] # all permissions on all channels
                                    ]
                            },
                        },
                    },
                'command': {
                    "prefix": None,
                    },
                }
        self.save()

    def _load(self):
        with open(self._filename, 'r') as file_handle:
            self.config = json.load(file_handle)

    def save(self):
        """Saves the master config. Use plugin.config.save() to save plugin
        configs
        
        """
        with open(self._filename, 'w') as output_file_handle:
            json.dump(self.config, output_file_handle, indent=4)

    def load_all_plugins(self):
        """Called by the main method at startup time to load all configured plugins"""
        for plugin_name in self.config['core']['plugins']:
            self.load_plugin(plugin_name)

    def load_plugin(self, plugin_name):
        """Loads the named plugin.
        
        plugin_name is expected to be in the form A.B where A is the module and
        B is the class. This module is expected to live in the plugins package.
        
        """
        modulename, classname = plugin_name.split(".")
        module = __import__("abbott.plugins."+modulename, fromlist=[classname])
        
        pluginclass = getattr(module, classname)
        
        plugin = pluginclass(plugin_name, self._transport, self)
        try:
            plugin.start()
        except Exception:
            self._transport.unhook_plugin(plugin)
            raise

        self.loaded_plugins[plugin_name] = plugin

    def unload_plugin(self, plugin_name):
        plugin = self.loaded_plugins.pop(plugin_name)
        self._transport.unhook_plugin(plugin)
        plugin.stop()

    def get_plugin_config(self, plugin_name):
        """Returns a config dictionary for the named plugin. This dict has an
        additional method: .save(), to save any changes back to persistant
        store

        """
        try:
            old_config = self.config['plugin_config'][plugin_name]
        except KeyError:
            old_config = {}
        
        plugin_config_path = os.path.join(self._configdir, plugin_name)+".json"

        if not os.path.exists(plugin_config_path):
            with open(plugin_config_path, "w") as out:
                json.dump(old_config, out, indent=4)

        if "plugin_config" in self.config and plugin_name in self.config['plugin_config']:
            del self.config['plugin_config'][plugin_name]
            self.save()
        if "plugin_config" in self.config and not self.config['plugin_config']:
            del self.config['plugin_config']
            self.save()


        return PluginConfig(plugin_config_path)


class BotPlugin(object):
    """All bot plugins should inherit from this. It provides methods for
    talking to the transport layer and for saving persistent configuration

    If subclasses set the class attribute DEFAULT_CONFIG, it specifies default
    config items that will always be present in self.config, initialized with
    the default value if it is not found in the config file. DEFAULT_CONFIG
    should be a dictionary mapping strings to default values.

    The REQUIRES class variable should be set to a list of plugins that this
    one depends on.

    """
    REQUIRES = []
    DEFAULT_CONFIG = {}
    def __init__(self, plugin_name, transport, pluginboss):
        self.plugin_name = plugin_name
        self.transport = transport
        self.pluginboss = pluginboss

        self.reload()

    ### Plugins should override these methods if appropriate

    def reload(self):
        """This is called to indicate the configuration has changed and the
        plugin should make any necessary changes to its runtime.
        
        This method is called by the constructor, and anytime an external event
        indicates the configuration has changed.

        Feel free to override. This is just an example.

        """
        self.config = self.pluginboss.get_plugin_config(self.plugin_name)
        save = lambda: None
        for key, defaultvalue in self.DEFAULT_CONFIG.items():
            if key not in self.config:
                save = self.config.save
                self.config[key] = defaultvalue
        save()

    def start(self):
        """Do any initialization here. This is called after __init__()

        This should do any sort of interaction with the twisted reactor such as connecting

        """
        pass

    def stop(self):
        """Do any finilization here. This should unhook any events it has
        hooked and remove it from the twisted reactor if applicable

        """
        pass

    ### Convenience dispatcher methods, but feel free to override them if you
    ### want!

    def received_event(self, event):
        """An event has been received by this plugin"""
        method = getattr(self, "on_event_%s" % event.eventtype.replace(".","_"), None)
        if method:
            method(event)

    def received_middleware_event(self, event):
        """This event has been intercepted before it got to its destination. We
        can return a new / modified event, or None to indicate the event should
        be swallowed

        """
        method = getattr(self, "on_middleware_%s" % event.eventtype.replace(".","_"), None)
        if method:
            return method(event)
        return event

    def incoming_request(self, name, *args, **kwargs):
        """A request has been issued to this plugin. Return a deferred.

        """
        method = getattr(self, "on_request_%s" % name.replace(".","_"), None)
        if method:
            toret = method(*args, **kwargs)
        else:
            toret = defer.fail(NotImplementedError("The plugin {0} does not provide a request method for {1}".format(self.plugin_name, name)))
        return toret

    ### Convenience methods for use by the plugin to install event listeners
    def install_middleware(self, matchstr):
        self.transport.install_middleware(matchstr, self)

    def listen_for_event(self, matchstr):
        self.transport.listen_for_event(matchstr, self)

    def provides_request(self, name):
        self.transport.provides_request(name, self)

class EventWatcher(object):
    """This is a mixin for plugins that adds event watching features, which
    eases the implementation of certain design patterns. This does all the
    bookkeeping and keeps track of all the deferred and timer objects so that
    callers don't have to.

    To use it, define your plugin with this mixin::
    
        class MyPlugin(EventWatcher, BotPlugin):
            pass

        class AnotherPlugin(EventWatcher, CommandPluginSuperclass):
            pass
    
    Then, in a method, call self.wait_for(), which will return a deferred
    object. For increased usefulness, methods should be decorated with
    defer.inlineCallbacks and you should yield the returned deferred, which
    will suspend execution of the method until a matching event comes in.
    
    For example, consider this plugin that responds to someone saying "hello".
    With this mixin, the entire plugin could be implemented in the start()
    method::

        class HelloResponder(EventWatcher, BotPlugin)

            @defer.inlineCallbacks
            def start(self):
                self.listen_for_event("irc.do_msg")
                while True:
                    event = (yield self.wait_for(
                            Event("irc.on_privmsg", message="hello")))
                
                    self.transport.send_event(Event("irc.do_msg",
                            user=event.channel,
                            message="hi!",
                            ))
    
    See docs on wait_for() for options and more info on how to use it.

    Note that if you use functionality in this mixin, you should take care to
    properly call super() for __init__(), stop() and received_event() if you
    override those methods!

    Also note that functions that yield to a wait_for() may never resume if the
    plugin is stopped. (this is a feature)

    """
    def __init__(self, plugin_name, transport, pluginboss):
        super(EventWatcher, self).__init__(plugin_name, transport, pluginboss)

        # Holds all timers so that we can cancel them on stop
        self.__timers = set()

        # Maps event names to sets of (event_match object, deferred object, timer object)
        self.__watchers = defaultdict(set)

    def stop(self):
        for s in self.__timers:
            s.cancel()
        super(EventWatcher, self).stop()

    def received_event(self, event):
        toremove = []
        try:
            for event_match, d, timer in list(self.__watchers[event.eventtype]):
                # Every attribute specified in the event_match template object must
                # be equal to the corresponding attribute in the received event
                for attr in dir(event_match):
                    if attr.startswith("_"): continue
                    if not hasattr(event, attr) or getattr(event_match, attr) != getattr(event, attr):
                        break
                else:
                    # we have a match
                    toremove.append((event_match, d, timer))
                    if timer:
                        timer.cancel()
                        self.__timers.remove(timer)
                    d.callback(event)
        finally:
            # In a finally block in case the callback raises an error of some
            # sort. Actually, I don't think callbacks raise errors to the
            # caller (the error goes to the next errback handler) Anyways
            # putting this in a finally block can't hurt
            for item in toremove:
                self.__watchers[event.eventtype].remove(item)
        super(EventWatcher, self).received_event(event)


    def wait_for(self, event_match=None, timeout=None):
        """This method returns a twisted deferred that fires when an event is
        received, or when the given timeout expires, whichever comes first.

        event_match should be an Event object with the correct type and
        parameters of the one you wish to match. Each parameter given on
        event_match must equal the corresponding parameter on the incoming
        event.

        It is the caller's responsibility to make sure the plugin has a hook in
        place to catch any given event types. So remember to call
        self.listen_for_event in start() for events you pass to wait_for()

        A timeout of 0 is different than a timeout of None. None is equivalent
        to infinite timeout, in that it will wait forever for the event. A
        timeout of 0 will always pass through and never return an event.
        Exception: if both are None then success is returned.

        """
        if timeout == 0:
            return defer.succeed(None)
        if not event_match and not timeout:
            return defer.succeed(None)
        elif not event_match:
            # just a timeout
            d = defer.Deferred()
            def timer_timesup():
                self.__timers.remove(timer)
                d.callback(None)
            timer = reactor.callLater(timeout, timer_timesup)
            self.__timers.add(timer)
            return d

        else:

            # An event watcher and possibly a timer
            d = defer.Deferred()
            if timeout:
                # both an event watcher and a timer
                def timer_and_event_timesup():
                    self.__timers.remove(timer)
                    self.__watchers[event_match.eventtype].remove((event_match, d, timer))
                    d.callback(None)
                timer = reactor.callLater(timeout, timer_and_event_timesup)
                self.__timers.add(timer)
            else:
                timer = None
            self.__watchers[event_match.eventtype].add((event_match, d, timer))
            return d

def non_reentrant(**keyargs_def):
    """This is a handy function decorator that will pass through the first call
    to the function, but prevent a second call to the function with the same
    key from entering the function until the first one finishes, at which point
    both calls get the same result.

    More precisely, this function returns a decorator for a function which is
    expected to return a deferred. Only one execution of the function per key
    is allowed. Other functions will wait for the original invocation by
    returning deferred which will fire upon the first invocation's completion
    (with the same value).

    This is a handy way of de-duplicating effort. If two functions both call
    some kind of method that needs to wait for a result, then only one instance
    of the method is actually invoked, but they both get the answer.

    This decorator can only wrap methods which return a deferred (including
    functions decorated with defer.inlineCallbacks).

    keyargs_def is a dictionary mapping keyword argument names (in **kwargs) to the
    positional index (in *args), as a way of declaring the arguments you want
    part of the key.  The positional index can be None if the argument to key
    is keyword only—not positional.  Remember, positional arguments start at 0
    and that includes the 'self' paramater of methods. If the keyword and
    positional arguments do not match, behavior is undefined.

    example:

        @non_reentrant(self=0, channel=1)
        @defer.inlineCallbacks
        def compute_value(self, channel, param, somethingelse):
            ...
            yield something()
            ...
            defer.returnValue(value)

    If another function calls compute_value() while it was waiting for
    something() to return (and they were both called with the same channel),
    then the new caller will wait for the existing invocation of
    compute_value() to finish, at which point both callers get the return value
    `value`.

    Warning, any arguments not part of the declared key are ignored for all but
    the original invocation, since the function isn't called more than once at
    a time.

    Also note that it is unnecessary to include the `self` parameter of methods
    only because in this framework there is only ever one instance of each
    plugin object. If you are using this decorator in other situations, you
    need to declare self=0. The reason is since the entrants are stored in a
    closure of the decorator function, it is stored per-class, not
    per-instance.

    """
    def decorator(func):

        # This maps a tuple of key arguments to a list of deferred objects
        entrants = defaultdict(list)

        @wraps(func)
        def new_func(*args, **kwargs):
            # determine what our key argument tuple is.
            key_arguments = []
            # note that this stores key arguments in the order returned from
            # this dictionary, which depends the order being consistent.
            # Dictionary order is guaranteed not to change as long as it
            # doesn't mutate, so this is okay.
            for kwarg, posarg in keyargs_def.items():
                if posarg is not None and len(args) > posarg:
                    key_arguments.append(args[posarg])
                elif kwarg in kwargs:
                    key_arguments.append(kwargs[kwarg])
                else:
                    # This argument was not given. We cannot do anything this
                    # call
                    return func(*args, **kwargs)

            key_arguments = tuple(key_arguments)

            # Now that we have a key by which to track invocations, check our
            # entrants dict
            d = defer.Deferred()
            entrants[key_arguments].append(d)

            if len(entrants[key_arguments]) == 1:
                # No existing calls to this function (because we were the
                # first), call it and set a handler for its completion
                real_d = func(*args, **kwargs)
                def done(param):
                    for other_d in entrants.pop(key_arguments):
                        other_d.callback(param)
                real_d.addBoth(done)
            return d


        return new_func

    return decorator
