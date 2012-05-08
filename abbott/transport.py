import re
from collections import defaultdict
from itertools import chain

"""
About the Abbott event system:

Events are instances of the Event class below. Each event has a name, which is
typically a string with dot separators. The convention is that events should be
hierarchical, like domain.action, such as irc.on_privmsg (which is fired on
receiving a PRIVMSG command from the IRC server.)

Event objects carry an arbitrary (or rather, event-defined) set of attributes.

Each BotPlugin object can register to be notified when an event is emitted by
any other plugin. Plugins can register listeners on a particular event name, or
glob event names such as irc.* or irc.on_*

Globs do not transcend dots, so you must do something like *.* to receive all
events.

There are two ways to register an event: as a normal listener, or as a
middleware listener. There are two differences: all middleware listeners are
called before normal listeners, and middleware listeners have an opportunity to
edit the event arbitrarily (add or change attributes) or destroy the event (in
which case no other handlers will be called.

This system allows for things like an auth plugin which inserts authentication
information into the event for use by other plugins. It's perfectly fine to
insert callback functions as attributes on the event too, not just values. (See
the auth.Auth plugin)

Ideas for the future: a way for plugins to interact directly with each other to
request information, returning Deferred objects. Useful if one plugin wants to
ask information from another plugin, but another plugin may not know
immedaitely. Maybe some kind of "provides" interface.

"""

class Transport(object):
    """A generalized transport layer to send messages from one plugin to another.
    
    There is one instance of this class per bot, shared among all the plugins
    """

    def __init__(self):
        # maps event names to sets of objects
        self._middleware_listeners = defaultdict(set)
        self._event_listeners = defaultdict(set)

    def send_event(self, event):
        # Note: iterating over the listener dictionaries and sets are done with
        # copies, not an iterator, because of the posibility of the dictionary
        # being modified somewhere down the stack in an event handler

        # First call all middleware
        for callback_name, callback_obj_set in self._middleware_listeners.items():
            callback_parts = [re.escape(x) for x in callback_name.split("*")]
            callback_match = "[^. ]+".join(callback_parts) + "$"
            if re.match(callback_match, event.eventtype):
                for callback_obj in set(callback_obj_set):
                    event = callback_obj.received_middleware_event(event)
                    if not event:
                        return

        # Now call the event handlers
        for callback_name, callback_obj_set in self._event_listeners.items():
            callback_match = callback_name.replace("*", "[^.]+") + "$"
            if re.match(callback_match, event.eventtype):
                for callback_obj in set(callback_obj_set):
                    callback_obj.received_event(event)

    def install_middleware(self, matchstr, obj_to_notify):
        self._middleware_listeners[matchstr].add(obj_to_notify)

    def listen_for_event(self, matchstr, obj_to_notify):
        self._event_listeners[matchstr].add(obj_to_notify)

    def unhook_plugin(self, plugin):
        for obj_set in chain(self._middleware_listeners.itervalues(),
                self._event_listeners.itervalues()):
            obj_set.discard(plugin)


class Event(object):
    """Pretty much just a container for data"""
    def __init__(self, eventtype, **kwargs):
        self.__dict__.update(kwargs)
        self.eventtype = eventtype

