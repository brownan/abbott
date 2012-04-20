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

        self._loaded_plugins = {}
        
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


        print("Alright, I can take it from here. All other commands you can issue to the bot at runtime")

        self._config = {
                'core': {
                    'server': server,
                    'port': port,
                    'admins': [admin],
                    'nick': nick,
                    'plugins': ['irc.IRCBotPlugin'],
                    'plugin_config': {},
                    }
                }
        self.save()

    def _load(self):
        with open(self._filename, 'r') as file_handle:
            self._config = json.load(file_handle)

    def __getattr__(self, key):
        value = self._config['core'][key]
        return copy(value)

    def __setattr__(self, key, value):
        self._config['core'][key] = value
        self.save()

    def save(self):
        with open(self._filename, 'w') as output_file_handle:
            json.dump(self._config, output_file_handle)

    def load_all_plugins(self):
        """Called by the main method at startup time to load all configured plugins"""
        for plugin_name in self._config['core']['plugins']:
            self.load_plugin(plugin_name)

    def load_plugin(self, plugin_name):
        """Loads the named plugin.
        
        plugin_name is expected to be in the form A.B where A is the module and
        B is the class. This module is expected to live in the twisted.plugins
        package.
        
        """
        modulename, classname = plugin_name.split(".")
        module = __import__("twistbot.plugins."+modulename, fromlist=[classname])
        pluginclass = getattr(module, classname)
        
        plugin = pluginclass(plugin_name, self._transport, self)
        plugin.start()

        self._loaded_plugins[plugin_name] = plugin

    def unload_plugin(self, plugin_name):
        plugin = self._loaded_plugins.pop(plugin_name)
        plugin.stop()

    def get_plugin_config(self, plugin_name):
        return self._config['plugin_config'].get(plugin_name, {})


class BotPlugin(object):
    """All bot plugins should inherit from this. It provides methods for
    talking to the transport layer and for saving persistent configuration

    """
    def __init__(self, plugin_name, transport, pluginboss):
        # These variables are private and name-mangled to 1. keep the
        # implementation hidden from subclasses, and 2. to prevent namespace
        # collisions with subclasses (further hiding the implementation of this
        # superclass). It's not strictly necessary since namespace collisions
        # are unlikely, but it strikes me as the "right way" to do this kind of
        # thing: where a superclass wants to provide functionality to its
        # subclasses in a carefully exposed manner.
        self.__plugin_name = plugin_name
        self.__transport = transport
        self.__pluginboss = pluginboss

    ### Transport-layer methods. Plugins should call these methods for sending
    ### and receiving events from other plugins

    def listen_for_event(self, eventname, callback):
        """Listen for an event. The callback signature is event-specific"""
        raise NotImplementedError() # XXX TODO

    def stop_listening(self, eventname):
        """Disconnects all handlers this plugin has for the given event"""
        raise NotImplementedError() # XXX TODO

    def send_message(self, pluginid, eventname, *args, **kwargs):
        """Send a message to a specific plugin"""
        raise NotImplementedError() # XXX TODO

    def broadcast_message(self, eventname, *args, **kwargs):
        """Broadcast a message to all plugins listening for this event"""
        raise NotImplementedError() # XXX TODO

    ### Persistent storage methods. Plugins should call these methods for
    ### saving and restoring data from the persistent store

    def save_config(self):
        """Saves the configuration persistently"""
        self.__pluginboss.save()

    def get_config(self):
        """Returns a dictionary of name/value mappings from the persistent
        store. This returns a reference. Edit the returned dictionary and call
        self.save_config() to save to the persistent store.

        Note: the config may be saved randomly at any time. However, it is not
        guaranteed to be saved unless self.save_config() is called!

        """
        return self.__pluginboss.get_plugin_config(self.__plugin_name)

    ### Interface to periodic and deferred callbacks. Plugins can call these
    ### methods as a simple interface to the twisted scheduler

    def register_periodic_callback(self, timeout, callback):
        raise NotImplementedError() # XXX TODO

    def call_later(self, timeout, callback):
        raise NotImplementedError() # XXX TODO


    ### Plugins should override these methods if appropriate

    def reload(self):
        """This is called to indicate the configuration has changed and the
        plugin should call get_config() and make any necessary changes to its
        runtime

        """
        pass

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

    ### These "secret" methods expose the underlying objects for plugins that
    ### wish to control core functionality
    def _get_transport(self):
        return self.__transport
    def _get_pluginboss(self):
        return self.__pluginboss
