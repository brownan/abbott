# encoding: UTF-8
import ast
import signal

from twisted.python import log

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

    def invoke_pyeval(self, event, match):
        evalstr = match.groupdict()['expression']

        signal.signal(signal.SIGALRM, alarmhandler)
        signal.alarm(1)
        try:
            replystr = str(safeeval(evalstr))
        except UnsafeCode, e:
            replystr = e.args[0]
        except Exception, e:
            if e.args:
                replystr = "%s: %s" % (type(e).__name__, e.args[0])
            else:
                replystr = type(e).__name__
        finally:
            signal.alarm(0)

        if isinstance(replystr, str):
            # Check to see if it's valid ascii
            try:
                replystr = replystr.decode("ASCII")
            except UnicodeDecodeError:
                replystr = repr(replystr)

        lines = replystr.split("\n")
        lines = [x.strip() for x in lines]
        lines = [x for x in lines if x]
        if len(lines) > 1:
            lines = [lines[0] + u" …(output truncated)"]

        for line in lines:
            maxlen = 200
            if len(line) >= maxlen:
                line = line[:maxlen-3] + u"…"
            event.reply(line)

### The rest of this file is the "safe" eval mechanism

# Set of names to import from the __builtins__ module into the scope of the
# executed code. This is a whitelist, so the code's scope will not get any
# other builtins
#
# Notable items not included: open, __import__, file, eval, execfile, input
#
# getattr and setattr are also not allowed, as they would allow access to
# restricted properties of various objects. There's probably still some way to
# get by this though.
ALLOWED_GLOBALS = {
    'abs': abs,
    'all': all,
    'any': any,
    'basestring': basestring,
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
    'long': long,
    'map': map,
    'max': max,
    'min': min,
    'next': next,
    'object': object,
    'oct': oct,
    'ord': ord,
    'pow': pow,
    'range': range,
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
    'unichr': unichr,
    'unicode': unicode,
    'vars': vars,
    'xrange': xrange,
    'zip': zip,
    'apply': apply,
    'buffer': buffer,
    'coerce': coerce,
    }

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
            # Disallow all __*__ attributes
            if node.attr.startswith("_"):
                raise UnsafeCode("Attribute %s is not allowed" % node.attr)
        
        # recurse to sub-nodes
        self.generic_visit(node)

def safeeval(evalstr):

    # Parse the code into an abstract syntax tree
    expr = ast.parse(evalstr, mode='eval')

    # Check that any attribute access is safe
    checker = SafetyChecker()
    checker.visit(expr)

    # Passed the tests? compile
    codeobj = compile(expr, "<unknown>", 'eval')

    # Prepare a restricted set of local and global variables for the execution's
    # scope
    scope = {
            # important so it doesn't inherit the global __builtins__
            "__builtins__": {}, 
            }

    # Now add some safe global functions back into the scope
    scope["__builtins__"].update(ALLOWED_GLOBALS)
    scope.update(ADDITIONAL_GLOBALS)
    
    return eval(codeobj, scope, scope)

