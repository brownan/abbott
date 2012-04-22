import pprint

from ..pluginbase import BotPlugin

class Log(BotPlugin):
    def start(self):
        self.listen_for_event("*.*")

    def received_event(self, event):
        print
        print "Received event %s" % (event.eventtype,)
        print pprint.pformat(event.__dict__)
        try:
            d = event.get_permissions()
        except AttributeError:
            return
        def cb(perms):
            print "User has permissions %s" % perms
        d.addCallback(cb)
