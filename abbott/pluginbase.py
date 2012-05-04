from __future__ import print_function
import json
from copy import copy

class PluginBoss(object):
    """Handles the loading and unloading of plugins and the reading 
    of config files and storage of configuration.

    There is one instance of this class per bot, and every plugin instance has
    a handle to it
    """
    def __init__(self, config, transport):
        self._filename = config
        self._transport = transport

        self.loaded_plugins = {}
        
        try:
            self.load()
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
            chan = raw_input("Any particular channel I should join to begin with? >")
            chan = chan.strip()
            if chan:
                break


        print("Alright, I can take it from here. All other commands you can issue to the bot at runtime")

        self.config = {
                'core': {
                    'admins': [admin],
                    'plugins': [
                        # Good default set of plugins to bootstrap functionality
                        'irc.IRCBotPlugin',
                        'irc.IRCController',
                        'irc.ReplyInserter',
                        'auth.Auth',
                        'plugincontroller.PluginController',
                        ],
                    },
                'plugin_config': {
                    'irc.IRCBotPlugin': {
                        'server': server,
                        'port': port,
                        'nick': nick,
                        'channels': [chan],
                        },
                    'auth.Auth': {
                        'perms': {
                                admin: [
                                    [None, '*'] # all permissions on all channels
                                    ]
                            },
                        },
                    },
                }
        self.save()

    def load(self):
        with open(self._filename, 'r') as file_handle:
            self.config = json.load(file_handle)

    def save(self):
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
        plugin.start()

        self.loaded_plugins[plugin_name] = plugin

    def unload_plugin(self, plugin_name):
        plugin = self.loaded_plugins.pop(plugin_name)
        self._transport.unhook_plugin(plugin)
        plugin.stop()

    def get_plugin_config(self, plugin_name):
        try:
            return self.config['plugin_config'][plugin_name]
        except KeyError:
            config = {}
            self.config['plugin_config'][plugin_name] = config
            return config


class BotPlugin(object):
    """All bot plugins should inherit from this. It provides methods for
    talking to the transport layer and for saving persistent configuration

    """
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

    ### Convenience methods for use by the plugin to install event listeners
    def install_middleware(self, matchstr):
        self.transport.install_middleware(matchstr, self)

    def listen_for_event(self, matchstr):
        self.transport.listen_for_event(matchstr, self)
