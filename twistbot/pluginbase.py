
class PluginBoss(object):
    """Handles the loading and unloading of plugins and the reading 
    of config files and storage of configuration.

    There is one instance of this class per bot, and every plugin instance has
    a handle to it
    """


class BotPlugin(object):
    """All bot plugins should inherit from this. It provides methods for
    talking to the transport layer and for saving persistent configuration

    """
    def __init__(self, transport, pluginboss):
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

    def set_parameter(self, name, value):
        """Saves a configuration value persistently"""
        raise NotImplementedError() # XXX TODO

    def get_config(self):
        """Returns a dictionary of name/value mappings from the persistent
        store
        """
        raise NotImplementedError() # XXX TODO

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

        """
        pass

    def stop(self):
        """Do any finilization here.

        """
        pass
