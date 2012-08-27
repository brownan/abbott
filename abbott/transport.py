import re
from collections import defaultdict
from itertools import chain

from twisted.internet import defer
from twisted.python import log

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

Aside from events, there is another mechanism meant for inter-plugin
communication. One plugin can send a "request" that another plugin perfrom some
action and return some result. A plugin must register that it provides a
particular request by calling its .transport.provides_request(request_name,
self) method. The request names are of a separate namespace as events, and
globbing is not allowed; each request must be specifically declared. When a
request comes in, the plugin's incoming_request() method is called. The first
argument is the request name.  The rest are *args, **kwargs. It is expected to
return a deferred object. To issue a request, call transport.send_request()
with the request name and the appropriate arguments.

Other notes about requests: only one plugin may provide a request handler for a
particular request name. If more than one handler tries to provide a particular
request, the behavior is undefined.

"""

class Transport(object):
    """A generalized transport layer to send messages from one plugin to another.
    
    There is one instance of this class per bot, shared among all the plugins
    """

    def __init__(self):
        # maps event names to sets of objects
        self._middleware_listeners = defaultdict(set)
        self._event_listeners = defaultdict(set)
        self._request_listeners = {}

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


    ### Request Interface

    def issue_request(self, name, *args, **kwargs):
        """Plugins: call this to send a request to some other plugin. Returns a deferred"""
        try:
            obj = self._request_listeners[name]
        except KeyError:
           return defer.fail(NotImplementedError("Request name %r is not implemented"%(name,)))

        toret = obj.incoming_request(name, *args, **kwargs)

        if not isinstance(toret, defer.Deferred):
            log.err("Requets method %s provided by %s did not return a deferred" % (name, obj))
            toret = defer.fail(TypeError("Request Method did not return a deferred"))

        return toret

    def provides_request(self, name, obj_to_notify):
        """Plugins: call this in your start() method to receive requests for this reqeust name

        """
        self._request_listeners[name] = obj_to_notify


    ### Called on plugin unloading

    def unhook_plugin(self, plugin):
        for obj_set in chain(self._middleware_listeners.itervalues(),
                self._event_listeners.itervalues()):
            obj_set.discard(plugin)
        for reqname, obj in self._request_listeners.items():
            if obj is plugin:
                del self._request_listeners[reqname]


class Event(object):
    """Pretty much just a container for data"""
    def __init__(self, eventtype, **kwargs):
        self.__dict__.update(kwargs)
        self.eventtype = eventtype

