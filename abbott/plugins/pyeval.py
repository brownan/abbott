# encoding: UTF-8
from __future__ import unicode_literals
import ast
import signal
import traceback

from twisted.python import log
from twisted.internet import defer

from ..command import CommandPluginSuperclass

class TimeoutError(Exception): pass
def alarmhandler(signum, frame):
    raise TimeoutError("Execution timed out")

class PyEval(CommandPluginSuperclass):
    def start(self):
        super(PyEval, self).start()

        self.install_command(
                cmdname="pyeval",
                cmdusage="<expression>",
                argmatch="(?P<expression>.+)$",
                helptext="Evalutaes a Pythen expression and prints the result",
                callback=self.invoke_pyeval,
                )

    @defer.inlineCallbacks
    def invoke_pyeval(self, event, match):
        evalstr = match.groupdict()['expression']

        safe = (yield event.has_permission("pyeval.trusted", event.channel))

        if safe:
            log.msg("PyEvaling trusted expr %r" % evalstr)
        else:
            log.msg("PyEvaling untrusted expr %r" % evalstr)

        signal.signal(signal.SIGALRM, alarmhandler)
        signal.alarm(1 if not safe else 10)
        try:
            try:
                replyobj = safeeval(evalstr, safe)
            finally:
                signal.alarm(0)
        except UnsafeCode as e:
            replystr = e.args[0]
        except Exception as e:
            replystr = traceback.format_exception_only(type(e),e)[-1].strip()
        else:
            if isinstance(replyobj, str):
                # Check to see if it's valid ascii
                try:
                    replystr = replyobj.decode("ASCII")
                except UnicodeDecodeError:
                    replystr = repr(replyobj)
            elif isinstance(replyobj, str):
                replystr = replyobj
            else:
                replystr = str(str(replyobj))


        lines = replystr.split("\n")
        lines = [x.strip() for x in lines]
        lines = [x for x in lines if x]
        if len(lines) > 1:
            lines = [lines[0] + " …(output truncated)"]

        for line in lines:
            maxlen = 200
            if len(line) >= maxlen:
                line = line[:maxlen-3] + "…"
            event.reply(line)

### The rest of this file is the "safe" eval mechanism

# Set of builtins to add into the scope of the executed code. This is a
# whitelist, so the code's scope will not get any other builtins
#
# Notable items not included: open, __import__, file, eval, execfile, input
#
# getattr and setattr are also not allowed, as they would allow access to
# restricted properties of various objects. There's probably still some way to
# get by this though.
ALLOWED_BUILTINS = {
    'abs': abs,
    'all': all,
    'any': any,
    'basestring': str,
    'bin': bin,
    'bool': bool,
    'bytearray': bytearray,
    'callable': callable,
    'chr': chr,
    'cmp': cmp,
    'complex': complex,
    'dict': dict,
    'dir': dir,
    'divmod': divmod,
    'enumerate': enumerate,
    'filter': filter,
    'float': float,
    'format': format,
    'frozenset': frozenset,
    'globals': globals,
    'hasattr': hasattr,
    'hash': hash,
    'hex': hex,
    'id': id,
    'int': int,
    'isinstance': isinstance,
    'iter': iter,
    'len': len,
    'list': list,
    'locals': locals,
    'long': int,
    'map': map,
    'max': max,
    'min': min,
    'next': next,
    'object': object,
    'oct': oct,
    'ord': ord,
    'pow': pow,
    'range': range,
    # because __import__ is used in the builtin reduce to import the functools version
    'reduce': __import__("functools").reduce,
    'repr': repr,
    'reversed': reversed,
    'round': round,
    'set': set,
    'slice': slice,
    'sorted': sorted,
    'str': str,
    'sum': sum,
    'tuple': tuple,
    'type': type,
    'unichr': chr,
    'unicode': str,
    'vars': vars,
    'xrange': xrange,
    'zip': zip,
    'apply': apply,
    'buffer': buffer,
    'coerce': coerce,
    'True': True,
    'False': False,
    'None': None,
    }

# And some modules to import into our scope
ADDITIONAL_GLOBALS = dict((x,__import__(x)) for x in [
    "math",
    "cmath",
    "decimal",
    "fractions",
    "random",
    "itertools",
    "hashlib",
    
    ])

class UnsafeCode(Exception):
    pass

class SafetyChecker(ast.NodeVisitor):
    def visit(self, node):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise UnsafeCode("Attribute %s is not allowed" % node.attr)
        
        # recurse to sub-nodes
        self.generic_visit(node)

def safeeval(evalstr, safe=False):
    """Eval an expression and return the result. If safe is True, relaxes the
    restrictions.

    """

    # Parse the code into an abstract syntax tree
    expr = ast.parse(evalstr, mode='eval')

    # Check that any attribute access is safe
    if not safe:
        checker = SafetyChecker()
        checker.visit(expr)

    # Passed the tests? compile
    codeobj = compile(expr, "<unknown>", 'eval')

    # Make sure to override the __builtins__ item, otherwise the namespace will
    # inherit the default __builtins__ and get everything. Thus, not putting
    # this in will let the environment have the full default set of builtins.
    scope = {}
    if not safe:
        scope["__builtins__"] = dict(ALLOWED_BUILTINS)

    # And a few handy, safe modules
    scope.update(ADDITIONAL_GLOBALS)
    
    return eval(codeobj, scope, scope)

