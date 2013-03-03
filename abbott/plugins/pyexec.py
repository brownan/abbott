# encoding: UTF-8
import os.path
from io import StringIO
import time
import sys, os, posixpath, errno, stat
import traceback
import random

from twisted.internet import defer, reactor, error
from twisted.internet.utils import getProcessOutput
from twisted.python import log
from twisted.internet.protocol import ProcessProtocol

from pypy.translator.sandbox import sandlib
from pypy.translator.sandbox.vfs import Dir, RealDir, RealFile, File
from pypy.translator.sandbox.vfs import UID, GID

from ..pluginbase import BotPlugin
from ..command import CommandPluginSuperclass

class PyPyTwistedSandboxProtocol(ProcessProtocol, object):
    """A twisted version of pypy's sandlib.SandboxedProc"""
    def __init__(self, ended_deferred=None, timelimit=None, **kwargs):
        self.ended_deferred = ended_deferred

        self.__error = StringIO()

        self.exited = False
        if timelimit:
            def timesup():
                if not self.exited:
                    log.msg("Time limit reached. aborting.")
                    self.abort()
            reactor.callLater(timelimit, timesup)

        self.__instream = StringIO()

        # This is a workaround for pypy's unmarshaller behavior. Since we get
        # strings in blocks from the twisted reactor, we may not have a
        # complete request in one call to outReceived(). However, the pypy
        # unmarshaler doesn't always throw errors when it requests more data
        # than is available, but rather silently returns the data it has. For
        # example, to unmarshal a length 10000 string, it will call
        # self.__instream.read(10000), but if less than that much of the string
        # was given in that call, it doesn't notice and just returns the
        # truncated string. This also means the next call with the rest of the
        # string will error because it is an invalid marshal request.
        #
        # So, we modify the read() method of this stringio object here so that
        # it raises EOFError in the event that not enough stream is available
        # to fulfill the request. That exception is caught in outReceived() and
        # the data is saved for the next call, where the new data is appended
        # and the marshal is restarted.
        oldread = self.__instream.read
        def newread(n):
            pos = self.__instream.tell()
            self.__instream.seek(0,2)
            endpos = self.__instream.tell()
            self.__instream.seek(pos)
            if n > endpos - pos:
                raise EOFError("Not enough string to fulfill read request")
            return oldread(n)
        self.__instream.read = newread

    def connectionMade(self):
        # Because sandlib.write_exception() calls write() and flush() and we'd
        # like to be able to just pass the transport object
        self.transport.flush = lambda: None

    def errReceived(self, text):
        self.__error.write(text)
        
    @defer.inlineCallbacks
    def outReceived(self, text):
        self.__instream.write(text)
        #self.__instream.seek(0,2)
        #log.msg("Received {0} bytes of input (total {1}). Unmarshalling...".format(len(text), self.__instream.tell()))
        self.__instream.seek(0)
        try:
            fname = sandlib.marshal.load(self.__instream)
            args = sandlib.marshal.load(self.__instream)
        except EOFError as e:
            #log.msg("EOFError unmarshalling args ({0}). Deferring until we get more data".format(e))
            self.__instream.seek(0,2)
            return
        except Exception as e:
            log.msg(traceback.format_exc())
            self.abort()
            return
        else:
            self.__instream.truncate(0)

        #log.msg("unmarshal successful. Sandbox func call: {0}{1!r}".format(fname, sandlib.shortrepr(args)))
        try:
            retval = self.handle_message(fname, *args)
            if isinstance(retval, defer.Deferred):
                answer, resulttype = (yield retval)
            else:
                answer, resulttype = retval
        except Exception as e:
            #log.msg("Raise exception: {1}, {0}".format(e, e.__class__.__name__))
            tb = sys.exc_info()[2]
            sandlib.write_exception(self.transport, e, tb)
        else:
            if not self.exited:
                #log.msg("Return: {0}".format(sandlib.shortrepr(answer)))
                sandlib.write_message(self.transport, 0)  # error code - 0 for ok
                sandlib.write_message(self.transport, answer, resulttype)

    def abort(self):
        """Kill the process and bail out"""
        self.transport.loseConnection()
        if not self.exited:
            self.transport.signalProcess("KILL")
        if self.ended_deferred:
            self.ended_deferred.callback("Process aborted")
            self.ended_deferred = None

    def processExited(self, status):
        self.exited = True
        self.transport.loseConnection()

    def processEnded(self, reason):
        if self.ended_deferred:
            e = self.__error.getvalue()
            if e:
                self.ended_deferred.callback(e)
            else:
                self.ended_deferred.callback("Process exited with code {0}".format(reason.value.exitCode))
            self.ended_deferred = None

    def handle_message(self, fnname, *args):
        if '__' in fnname:
            log.msg("Was going to exec {0} but it is unsafe".format(fnname))
            raise ValueError("unsafe fnname")
        try:
            handler = getattr(self, 'do_' + fnname.replace('.', '__'))
        except AttributeError:
            log.msg("Tried to exec {0} but no handler exists".format(fnname))
            raise RuntimeError("no handler for this function")
        resulttype = getattr(handler, 'resulttype', None)
        return handler(*args), resulttype

class PyPyTwistedIOSandboxedProtocol(PyPyTwistedSandboxProtocol):
    def __init__(self, *args, **kwargs):
        super(PyPyTwistedIOSandboxedProtocol, self).__init__(*args, **kwargs)

        iv = kwargs.get("inputvalue", "")
        if isinstance(iv, str):
            iv = iv.encode("UTF-8")
        self._input = StringIO(iv)

        self._inputlimit = kwargs.get("inputlimit", None)
        self._written = 0

        self.output = StringIO()
        self.error = StringIO()
        self.both = StringIO()

    def do_ll_os__ll_os_read(self, fd, size):
        if fd == 0:
            inputdata = self._input.read(size)
            return inputdata
        else:
            raise OSError("Trying to read from fd {0}".format(fd))

    def do_ll_os__ll_os_write(self, fd, data):
        if self._inputlimit and fd in (1,2):
            left = self._inputlimit - self._written
            data = data[:left]
            self._written += len(data)

        if fd == 1:
            self.output.write(data)
            self.both.write(data)
        elif fd == 2:
            self.output.write(data)
            self.both.write(data)
        else:
            raise OSError("Trying to write to fd {0}".format(fd))
        return len(data)

    @defer.inlineCallbacks
    def do_ll_time__ll_time_sleep(self, seconds):
        d = defer.Deferred()
        reactor.callLater(seconds, d.callback, None)
        yield d
        return

    def do_ll_time__ll_time_time(self):
        return time.time()

    def do_ll_time__ll_time_clock(self):
        try:
            starttime = self.starttime
        except AttributeError:
            starttime = self.starttime = time.time()
        return time.time() - starttime

class PyPyTwistedVirtualizedSandboxedProtocol(PyPyTwistedSandboxProtocol):
    virtual_env = {}
    virtual_cwd = "/tmp"
    virtual_fd_range = list(range(3,50))
    virtual_console_isatty = False

    def __init__(self, *args, **kwargs):
        super(PyPyTwistedVirtualizedSandboxedProtocol, self).__init__(*args, **kwargs)

        self.virtual_root = self.build_virtual_root()
        self.open_fds = {}

    def build_virtual_root(self):
        raise NotImplementedError("must be overriden")

    def do_ll_os__ll_os_envitems(self):
        return list(self.virtual_env.items())

    def do_ll_os__ll_os_getenv(self, name):
        return self.virtual_env.get(name)

    def translate_path(self, vpath):
        # XXX this assumes posix vpaths for now, but os-specific real paths
        vpath = posixpath.normpath(posixpath.join(self.virtual_cwd, vpath))
        dirnode = self.virtual_root
        components = [component for component in vpath.split('/')]
        for component in components[:-1]:
            if component:
                dirnode = dirnode.join(component)
                if dirnode.kind != stat.S_IFDIR:
                    raise OSError(errno.ENOTDIR, component)
        return dirnode, components[-1]

    def get_node(self, vpath):
        dirnode, name = self.translate_path(vpath)
        if name:
            node = dirnode.join(name)
        else:
            node = dirnode
        return node

    def do_ll_os__ll_os_stat(self, vpathname):
        node = self.get_node(vpathname)
        return node.stat()
    do_ll_os__ll_os_stat.resulttype = sandlib.RESULTTYPE_STATRESULT

    do_ll_os__ll_os_lstat = do_ll_os__ll_os_stat

    def do_ll_os__ll_os_isatty(self, fd):
        return self.virtual_console_isatty and fd in (0, 1, 2)

    def allocate_fd(self, f, node=None):
        for fd in self.virtual_fd_range:
            if fd not in self.open_fds:
                self.open_fds[fd] = (f, node)
                return fd
        else:
            raise OSError(errno.EMFILE, "trying to open too many files")

    def get_fd(self, fd, throw=True):
        """Get the objects implementing file descriptor `fd`.

        Returns a pair, (open file, vfs node)

        `throw`: if true, raise OSError for bad fd, else return (None, None).
        """
        try:
            f, node = self.open_fds[fd]
        except KeyError:
            if throw:
                raise OSError(errno.EBADF, "bad file descriptor")
            return None, None
        return f, node

    def get_file(self, fd, throw=True):
        """Return the open file for file descriptor `fd`."""
        return self.get_fd(fd, throw)[0]

    def do_ll_os__ll_os_open(self, vpathname, flags, mode):
        node = self.get_node(vpathname)
        if flags & (os.O_RDONLY|os.O_WRONLY|os.O_RDWR) != os.O_RDONLY:
            raise OSError(errno.EPERM, "write access denied")
        # all other flags are ignored
        f = node.open()
        return self.allocate_fd(f, node)

    def do_ll_os__ll_os_close(self, fd):
        f = self.get_file(fd)
        del self.open_fds[fd]
        f.close()

    def do_ll_os__ll_os_read(self, fd, size):
        f = self.get_file(fd, throw=False)
        if f is None:
            return super(PyPyTwistedVirtualizedSandboxedProtocol, self).do_ll_os__ll_os_read(
                fd, size)
        else:
            if not (0 <= size <= sys.maxsize):
                raise OSError(errno.EINVAL, "invalid read size")
            # don't try to read more than 256KB at once here
            return f.read(min(size, 256*1024))

    def do_ll_os__ll_os_fstat(self, fd):
        f, node = self.get_fd(fd)
        return node.stat()
    do_ll_os__ll_os_fstat.resulttype = sandlib.RESULTTYPE_STATRESULT

    def do_ll_os__ll_os_lseek(self, fd, pos, how):
        f = self.get_file(fd)
        f.seek(pos, how)
        return f.tell()
    do_ll_os__ll_os_lseek.resulttype = sandlib.RESULTTYPE_LONGLONG

    def do_ll_os__ll_os_getcwd(self):
        return self.virtual_cwd

    def do_ll_os__ll_os_strerror(self, errnum):
        # unsure if this shouldn't be considered safeboxsafe
        return os.strerror(errnum) or ('Unknown error %d' % (errnum,))

    def do_ll_os__ll_os_listdir(self, vpathname):
        node = self.get_node(vpathname)
        return list(node.keys())

    def do_ll_os__ll_os_getuid(self):
        return UID
    do_ll_os__ll_os_geteuid = do_ll_os__ll_os_getuid

    def do_ll_os__ll_os_getgid(self):
        return GID
    do_ll_os__ll_os_getegid = do_ll_os__ll_os_getgid

class PyExec(CommandPluginSuperclass):
    def start(self):
        super(PyExec, self).start()
        self.install_command(
                cmdname="pyexec",
                cmdusage="<statement>",
                argmatch="(?P<command>.+)$",
                permission=None,
                helptext="Run a python statement and print the result",
                callback=self.run_command,
                )
        
    def reload(self):
        super(PyExec, self).reload()
        if "inputlimit" not in self.config:
            self.config['inputlimit'] = self.config.get("inputlimit", 1024)
            self.config.save()

    @defer.inlineCallbacks
    def run_command(self, event, match):
        line_to_exec = match.groupdict()['command']

        pypy_root = self.config['pypy_root']

        sandbox_binary = os.path.join(pypy_root, "pypy-sandbox")
        
        # Create the sandbox protocol class we'll be using
        class Sandbox(PyPyTwistedVirtualizedSandboxedProtocol, PyPyTwistedIOSandboxedProtocol):
            virtual_cwd = "/"
            def build_virtual_root(self):
                excludes = [".pyc", ".pyo"]
                return Dir({
                     'startup.py': RealFile(os.path.join(pypy_root, "startup.py")),
                     'bin': Dir({ }),
                     'tmp': Dir({ }),
                     'var': Dir({ }),
                     'usr': Dir({
                         'bin': Dir({
                             "python": RealFile(sandbox_binary),
                             }),
                         'lib-python': RealDir(os.path.join(pypy_root, "lib-python"),exclude=excludes),
                         'lib_pypy': RealDir(os.path.join(pypy_root, "lib_pypy"),exclude=excludes),
                         }),
                     'etc': Dir({
                         'passwd': File(random.choice(["just kidding!","nope","so close!","try again","you're kidding, right?","fooled you!"])),
                         }),
                     'proc': Dir({
                         'cpuinfo': RealFile("/proc/cpuinfo"),
                         }),
                     })
            def do_ll_os__ll_sysconf(self, arg):
                return 9001
            def do_ll_os__ll_os_fork(self):
                return random.randint(0,50000)

        finished = defer.Deferred()

        # Start the sandbox process
        log.msg("Executing python line %r" % (line_to_exec,))
        sandbox = Sandbox(
                ended_deferred = finished,
                timelimit=self.config.get("timelimit", 5),
                inputvalue=line_to_exec,
                inputlimit=self.config["inputlimit"]+1,
                )
        try:
            reactor.spawnProcess(
                    sandbox,
                    sandbox_binary,
                    [ "/usr/bin/python",
                        "--heapsize", str(5*1024*1024),
                        "-S",
                        "-i",
                        "/startup.py",
                       ],
                    env={},
                )
        except Exception:
            # I don't think this will ever fail here
            event.reply("Process failed to launch")
            return

        exit_reason = (yield finished)
        log.msg(exit_reason)

        output = sandbox.both.getvalue().decode("UTF-8").rstrip("\n")
        if not output:
            event.reply("No output")
            return
        if len(output) > self.config['inputlimit']:
            output = output[:self.config['inputlimit']] + "…"

        lines = output.split("\n")
        for line in lines[:5]:
            event.reply("output: {0}".format(line), userprefix=False)
        if len(lines) > 5:
            event.reply("output: …", userprefix=False)

