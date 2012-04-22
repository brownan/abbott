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
        # First call all middleware
        for callback_name, callback_obj_set in self._middleware_listeners.iteritems():
            callback_match = callback_name.replace("*", "[^. ]+") + "$"
            if re.match(callback_match, event.eventtype):
                for callback_obj in callback_obj_set:
                    event = callback_obj.received_middleware_event(event)
                    if not event:
                        return

        # Now call the event handlers
        for callback_name, callback_obj_set in self._event_listeners.iteritems():
            callback_match = callback_name.replace("*", "[^.]+") + "$"
            if re.match(callback_match, event.eventtype):
                for callback_obj in callback_obj_set:
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

