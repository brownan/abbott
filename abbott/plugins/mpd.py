from twisted.internet.protocol import Factory
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet import reactor
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet import defer
from twisted.python import log

from ..command import CommandPluginSuperclass

class MPDProtocol(LineOnlyReceiver):
    """Simple interface to issue a command and get a response. For now only
    supports one command at a time

    """
    delimiter = "\n"
    def connectionMade(self):
        self.current_data = {}
        self.current_deferred = None

    def lineReceived(self, line):
        line = line.decode("UTF-8", "replace")

        if line.startswith("OK MPD"):
            pass
        elif line == "OK":
            if self.current_deferred:
                cb = self.current_deferred
                data = self.current_data

                self.current_deferred = None
                self.current_data = {}

                cb.callback(data)
            else:
                log.msg("Warning: got a line but did not request anything: %r" % line)
        else:
            line_parts = line.split(":", 1)
            self.current_data[line_parts[0].strip()] = line_parts[1].strip()

    def issue_command(self, command):
        """Issues a command and returns a deferred object with the results of
        that command
        
        """
        if self.current_deferred:
            raise RuntimeError("Cannot issue more than one command at once")

        self.sendLine(command.encode("UTF-8"))
        d = defer.Deferred()
        self.current_deferred = d
        self.current_data = {}
        return d

class MPDFactory(Factory):
    def buildProtocol(self, addr):
        return MPDProtocol()

class MPDPlugin(CommandPluginSuperclass):
    replyprefix = "Currently playing: "

    def start(self):
        super(MPDPlugin, self).start()

        self.install_command(
                cmdname="music",
                permission=None,
                callback=self.music_query,
                helptext="Query what the local MPD server is currently playing",
                )

        mpdgroup = self.install_cmdgroup(
                "mpd",
                permission="mpd",
                helptext="MPD configuration commands"
                )

        mpdgroup.install_command(
                cmdname="setstr",
                argmatch=r"(?P<str>.+)?$",
                cmdusage="[string]",
                helptext="Sets the string to display along with the current song, or removes it.",
                callback=self.set_str,
                )

    @defer.inlineCallbacks
    def music_query(self, event, match):
        point = TCP4ClientEndpoint(reactor, "localhost", 6600)

        protocol = (yield point.connect(MPDFactory()))
        try:
            status = (yield protocol.issue_command("status"))
            songinfo = (yield protocol.issue_command("currentsong"))
        finally:
            protocol.transport.loseConnection()

        reply = event.reply
        if status['state'] == "play":
            title = songinfo.get('Title', '')
            artist = songinfo.get('Artist', '')
            album = songinfo.get('Album', '')
            name = songinfo.get('Name', '')

            if title and artist and album:
                reply("%s %s - %s - %s" % (
                self.replyprefix,
                title,
                artist,
                album,
                ))
            elif title and artist:
                reply("%s %s by %s" % (
                    self.replyprefix,
                    title,
                    artist,
                    ))
            elif title:
                reply("%s %s" % (
                    self.replyprefix,
                    title,
                    ))
            elif name:
                reply("%s \"%s\"" % (
                    self.replyprefix,
                    name,
                    ))
            else:
                reply("%s <unknown>" % (
                    self.replyprefix,
                    ))

            if self.config.get("str", None):
                reply(self.config['str'])
        else:
            reply("Not currently playing")

    def set_str(self, event, match):
        gd = match.groupdict()
        new_str = gd['str']
        if not new_str:
            new_str = ""

        self.config['str'] = new_str
        self.pluginboss.save()

        event.reply("Saved!")
