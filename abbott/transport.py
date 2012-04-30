import re
from collections import defaultdict
from itertools import chain

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

