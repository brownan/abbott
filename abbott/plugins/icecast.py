# encoding: UTF-8
from StringIO import StringIO

from twisted.web.client import Agent
from twisted.internet import reactor
from twisted.internet import defer
from twisted.internet.protocol import Protocol

from ..command import CommandPluginSuperclass

try:
    from bs4 import BeautifulSoup
except ImportError:
    from BeautifulSoup import BeautifulSoup

class ReceiveBody(Protocol):
    def __init__(self, d):
        self.d = d
        self.content = StringIO()
    def dataReceived(self, content):
        self.content.write(content)
    def connectionLost(self, reason):
        self.d.callback(self.content.getvalue())

class IcecastStatus(CommandPluginSuperclass):
    
    def start(self):
        super(IcecastStatus, self).start()

        self.install_command(
                cmdname="radio",
                permission=None,
                callback=self.radio_status,
                helptext="What's currently playing on Icecast? Use this command to find out!",
                )

    @defer.inlineCallbacks
    def _send_request(self):
        """Returns a defered object which fires with the body of the response
        for the icecast page url.

        """
        url = self.config['url']
        agent = Agent(reactor)
        response = (yield agent.request(
                'GET',
                url.encode("ASCII"),
                ))

        d = defer.Deferred()
        response.deliverBody(ReceiveBody(d))
        defer.returnValue((yield d))


    def _get_status(self, content):
        """Takes the content of an icecast page, parses it, and returns an
        iterable over stream dicts with info about each stream

        """
        page = BeautifulSoup(content,
                convertEntities=BeautifulSoup.HTML_ENTITIES)
        for div in page.findAll('div', attrs={'class': 'streamheader'}):
            table = div.nextSibling.nextSibling
            yield dict((tr.td.string[:-1], (tr.td.nextSibling.nextSibling.string or "")[:])
                    for tr in table.findAll('tr'))
        


    @defer.inlineCallbacks
    def radio_status(self, event, match):
        response_deferred = self._send_request()
        response_deferred.addCallback(self._get_status)
        streams = list((yield response_deferred))
        maxtitlelen = max(len(s['Stream Title']) for s in streams)

        count = 0
        for stream in streams:
            count += 1
            replystr = u'{title:<{maxtitlelen}} — {song} [{listeners} listener{s}]'.format(
                title=stream['Stream Title'],
                listeners=stream['Current Listeners'],
                s='s' if '1'!=stream['Current Listeners'] else '',
                song=stream['Current Song'] or "<no metadata available>",
                maxtitlelen=maxtitlelen,
                )
            event.reply(replystr)
        if count > 0:
            event.reply("Head to {url} for stream links and to listen in!".format(
                url=self.config['url']))
        else:
            event.reply("No streams are currently playing at {url)".format(
                url=self.config['url']))
