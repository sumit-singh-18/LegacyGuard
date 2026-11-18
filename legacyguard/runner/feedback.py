"""Task 6.8e: what keeps a changed statement from running, and a bounded second pass of inputs aimed at it.

After the first runs, the traced line gate says which changed statements no input ran. `diagnose` walks the function's
syntax tree (Python's ast, as the guard conditions of Sprint 6.4 do) from such a statement up to its def and lists what
must hold for it to run: the if/elif/else, while, for, try/except and match branches around it, and the early exits
before it in the same block (`if not x: return`, `assert x`). A line after an unconditional return or raise is dead.
`chain_text` puts this in words for the untested reason.

`build` then makes inputs aimed at those conditions, in the spirit of branch distance in search-based testing but kept
simple: the bases are the first-pass inputs that got furthest along the chain (by their own traced lines), and each new
input is a base with one tested parameter changed, or with every tested parameter set at once to a value built to meet
the whole chain (a list of the checked type and length, a string with the checked prefix and suffix, a dict with the
checked key). Conditions made only of parameters, literals, a few builtins and str/dict methods are evaluated here on
the candidate values (never on project objects: only plain values decoded from the inputs), so values that meet the
whole chain come first. Every input is source "feedback" and weak, like the edit's own guesses.

Strings for a regex condition come from a small walk over the parsed pattern, each checked with re itself. Sprint 5's
sampler (inputs._regex_samples) is not used here: it keeps only strings on which the old and new patterns disagree,
and it needs Hypothesis in a child process; a guard needs strings the one pattern accepts, quickly. Sprint 5's flag
parsing (examples._flags), time limit (inputs._time_limit) and guard values (inputs._guard_values) are reused.

Our heuristic, not taken from a paper or a library."""
from __future__ import annotations

import ast
import itertools
import json
import re
import time

from runner import lg_codec

MAX_FEEDBACK_ITEMS = 40   # extra inputs per function in the second pass
MAX_FEEDBACK_BASES = 3    # first-pass inputs used as bases, per unreached statement
MAX_LINES = 6             # unreached statements diagnosed per function
MAX_POOL = 8              # candidate values per parameter for the combined search
MAX_COMBOS = 512          # value combinations evaluated per unreached statement
TEXT_MAX = 60             # characters of a condition shown in the reason
EVAL_S = 0.05             # one condition on one set of values, in the guard's own process
REGEX_SAMPLES = 4
MAX_SIZE = 10_000         # longest string, list or dict built here, or read by a condition evaluated here
MAX_REGEX_TEXT = 1_000    # longest string the regex walk builds before it gives up
SMALL_FACTOR = 100        # largest literal a `*` or `%` may use in a condition evaluated here

_NOT_PLAIN = object()     # a base value that is not a plain literal (an object, a recipe): never evaluated here
_NONE_YET = object()


class Cond:
    """One thing that must hold for a statement to run.

    kind: if, else, while, loop, loop-else, except, try-else, case, early, early-else, assert, nested.
    need: True/False when `test` must be true/false; None when it is not a plain test (a loop's iterable, an except).
    header: the line that runs when the condition is tested; inside: a line that runs only when it holds (absolute)."""
    __slots__ = ("kind", "line", "text", "need", "test", "header", "inside", "word", "extra")

    def __init__(self, kind, line, text, need=None, test=None, header=0, inside=0, word="", extra=""):
        self.kind, self.line, self.text, self.need, self.test = kind, line, text, need, test
        self.header, self.inside, self.word, self.extra = header or line, inside, word, extra


# ---- diagnosis ----

def _seg(text: str, node) -> str:
    try:
        s = ast.get_source_segment(text, node) or ast.unparse(node)
    except Exception:  # noqa: BLE001
        s = ""
    s = " ".join(s.split())
    return s if len(s) <= TEXT_MAX else s[:TEXT_MAX - 1] + "…"


def _parse_function(source: str):
    """(text, function node) for one function's text, or None."""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    return (source, fn) if fn is not None else None


_EXIT_WORDS = {ast.Return: "return", ast.Raise: "raise", ast.Continue: "continue", ast.Break: "break"}


def _exits(stmts: list) -> str:
    """The word for how a block always leaves ('return', 'raise', ...), or '' when it can fall through."""
    if not stmts:
        return ""
    last = stmts[-1]
    for cls, word in _EXIT_WORDS.items():
        if isinstance(last, cls):
            return word
    if isinstance(last, ast.If):
        a, b = _exits(last.body), _exits(last.orelse)
        return a if a and b else ""
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _exits(last.body)
    return ""


def _falsy_constant(test) -> bool | None:
    if isinstance(test, ast.Constant):
        return not test.value
    return None


def _field_of(parent, child) -> tuple[str, list] | None:
    for name in ("body", "orelse", "finalbody", "handlers", "cases"):
        block = getattr(parent, name, None)
        if isinstance(block, list) and any(x is child for x in block):
            return name, block
    return None


def _chain(text: str, fn, node, parents: dict, off: int) -> tuple[list[Cond], str]:
    """(conditions from the def down to node, in source order; why it can never run, or '')."""
    conds: list[Cond] = []
    dead = ""
    child, parent = node, parents.get(id(node))
    L = lambda n: n + off  # noqa: E731 - relative line to absolute
    while parent is not None:
        where = _field_of(parent, child)
        if where is not None:
            name, block = where
            idx = next(i for i, x in enumerate(block) if x is child)
            for j, prev in enumerate(block[:idx]):
                nxt = block[j + 1].lineno
                word = next((w for c, w in _EXIT_WORDS.items() if isinstance(prev, c)), "")
                if word and not dead:
                    dead = f"it follows `{_seg(text, prev)}` at line {L(prev.lineno)}, which always runs first at the same level"
                elif isinstance(prev, ast.If):
                    be, ee = _exits(prev.body), _exits(prev.orelse)
                    if be and ee and not dead:
                        dead = f"both branches of `if {_seg(text, prev.test)}` at line {L(prev.lineno)} leave before it"
                    elif be:
                        conds.append(Cond("early", L(prev.lineno), _seg(text, prev.test), False, prev.test, inside=L(nxt), word=be))
                    elif ee:
                        conds.append(Cond("early-else", L(prev.lineno), _seg(text, prev.test), True, prev.test, inside=L(nxt), word=ee))
                elif isinstance(prev, ast.Assert):
                    conds.append(Cond("assert", L(prev.lineno), _seg(text, prev.test), True, prev.test, inside=L(nxt)))
        if parent is fn:
            break
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            conds.append(Cond("nested", L(parent.lineno), getattr(parent, "name", "lambda"), inside=L(parent.body[0].lineno)))
            break
        name = where[0] if where else ""
        if isinstance(parent, ast.If):
            if name == "body":
                c = Cond("if", L(parent.lineno), _seg(text, parent.test), True, parent.test, inside=L(parent.body[0].lineno))
                c.extra = "elif" if text.splitlines()[parent.lineno - 1].lstrip().startswith("elif") else "if"
                conds.append(c)
                if _falsy_constant(parent.test) is True and not dead:
                    dead = f"`if {c.text}` at line {c.line} is never true"
            elif name == "orelse":
                conds.append(Cond("else", L(parent.lineno), _seg(text, parent.test), False, parent.test, inside=L(parent.orelse[0].lineno)))
                if _falsy_constant(parent.test) is False and not dead:
                    dead = f"`if {_seg(text, parent.test)}` at line {L(parent.lineno)} is always true"
        elif isinstance(parent, ast.While):
            if name == "body":
                conds.append(Cond("while", L(parent.lineno), _seg(text, parent.test), True, parent.test, inside=L(parent.body[0].lineno)))
            elif name == "orelse":
                conds.append(Cond("loop-else", L(parent.lineno), "", inside=L(parent.orelse[0].lineno)))
        elif isinstance(parent, (ast.For, ast.AsyncFor)):
            if name == "body":
                conds.append(Cond("loop", L(parent.lineno), _seg(text, parent.iter), None, parent.iter, inside=L(parent.body[0].lineno),
                                  extra=_seg(text, parent.target)))
            elif name == "orelse":
                conds.append(Cond("loop-else", L(parent.lineno), "", inside=L(parent.orelse[0].lineno)))
        elif isinstance(parent, ast.ExceptHandler):
            conds.append(Cond("except", L(parent.lineno), _seg(text, parent.type) if parent.type is not None else "",
                              inside=L(parent.body[0].lineno)))
        elif isinstance(parent, ast.Try) or type(parent).__name__ == "TryStar":
            if name == "orelse":
                conds.append(Cond("try-else", L(parent.lineno), "", inside=L(parent.orelse[0].lineno)))
        elif type(parent).__name__ == "match_case":
            conds.append(Cond("case", L(parent.pattern.lineno), _seg(text, parent.pattern), inside=L(parent.body[0].lineno)))
        child, parent = parent, parents.get(id(parent))
    conds.sort(key=lambda c: c.line)
    return conds, dead


def diagnose(source: str, start: int, missed: list, executed: list | None = None) -> list[dict]:
    """For each unreached statement range [first, last] (absolute lines) of a function whose text `source` starts at
    line `start`: {"line", "conds": [Cond], "dead": why it cannot run or "", "text": the reason in words}."""
    got = _parse_function(source) if source and start else None
    if got is None:
        return []
    text, fn = got
    off = start - 1
    parents: dict = {}
    depth: dict = {}
    for n in ast.walk(fn):
        for c in ast.iter_child_nodes(n):
            parents[id(c)] = n
    stmts = [n for n in ast.walk(fn) if isinstance(n, ast.stmt) and n is not fn]
    for n in stmts:
        d, p = 0, parents.get(id(n))
        while p is not None and p is not fn:
            d, p = d + 1, parents.get(id(p))
        depth[id(n)] = d
    done = set(executed or [])
    out = []
    for first, _last in missed[:MAX_LINES]:
        rel = first - off
        at = [n for n in stmts if n.lineno == rel]
        if not at:
            at = [n for n in stmts if n.lineno <= rel <= (n.end_lineno or n.lineno)]
            at.sort(key=lambda n: -depth[id(n)])
            at = at[:1]
        else:
            at.sort(key=lambda n: depth[id(n)])
        if not at:
            continue
        conds, dead = _chain(text, fn, at[0], parents, off)
        info = {"line": first, "conds": conds, "dead": dead}
        info["text"] = chain_text(info, done if executed is not None else None)
        out.append(info)
    return out


def _describe(c: Cond) -> str:
    """One clause of the chain, as a verb phrase after 'line N'."""
    if c.kind == "if":
        return f"is under `{c.extra or 'if'} {c.text}` (line {c.line})"
    if c.kind == "else":
        return f"runs only when `{c.text}` (line {c.line}) is false"
    if c.kind == "while":
        return f"is inside `while {c.text}` (line {c.line})"
    if c.kind == "loop":
        return f"is inside the loop `for {c.extra} in {c.text}` (line {c.line}), so `{c.text}` must not be empty"
    if c.kind == "loop-else":
        return f"is in the else of the loop at line {c.line}, which runs when the loop ends without break"
    if c.kind == "except":
        return f"is in `except {c.text}` (line {c.line}), so the try above it must raise" if c.text else \
            f"is in the bare `except` at line {c.line}, so the try above it must raise"
    if c.kind == "try-else":
        return f"is in the else of the try at line {c.line}, which runs only when its body raises nothing"
    if c.kind == "case":
        return f"is under `case {c.text}` (line {c.line})"
    if c.kind == "early":
        return f"only runs when the `if {c.text}` on line {c.line} does not {c.word}"
    if c.kind == "early-else":
        return f"only runs when `{c.text}` on line {c.line} is true (otherwise its else branch does `{c.word}`)"
    if c.kind == "assert":
        return f"only runs when `assert {c.text}` on line {c.line} passes"
    if c.kind == "nested":
        return f"is inside the nested function `{c.text}` (line {c.line}), which runs only when something calls it"
    return f"is under line {c.line}"


def _blocked_at(c: Cond) -> str:
    """What the inputs did at a condition they reached but never got past."""
    if c.kind in ("if", "while", "early-else", "assert"):
        return f"`{c.text}` was never true"
    if c.kind == "else":
        return f"`{c.text}` was never false"
    if c.kind == "early":
        return f"the early `{c.word}` there always happened"
    if c.kind == "loop":
        return "the loop body never ran"
    if c.kind == "except":
        return "nothing in the try raised that"
    if c.kind == "case":
        return "no input matched that case"
    return "the branch was never taken"


def chain_text(info: dict, executed: set | None = None) -> str:
    """'line 59 only runs when the `if not os.path.isdir(d)` on line 52 does not return and is under `if hooks_dir is
    None` (line 55); the inputs reached line 55 but `hooks_dir is None` was never true'."""
    line = info["line"]
    if info.get("dead"):
        return f"line {line} cannot run: {info['dead']}"
    conds = info["conds"]
    if not conds:
        return (f"line {line} has no condition above it in the function, so the inputs left the function before it "
                "(an exception or a return in an earlier statement)")
    clauses = [_describe(c) for c in conds]
    text = f"line {line} " + (", ".join(clauses[:-1]) + " and " + clauses[-1] if len(clauses) > 1 else clauses[0])
    if executed is not None:
        for c in conds:
            if c.inside and c.inside in executed:
                continue
            if c.header in executed:
                text += f"; the inputs reached line {c.header} but {_blocked_at(c)}"
            else:
                text += f"; no input reached line {c.header}"
            break
    return text


# ---- evaluating conditions on plain values ----

SAFE_FUNCS = {"len": len, "isinstance": isinstance, "str": str, "int": int, "float": float, "bool": bool, "list": list,
              "tuple": tuple, "dict": dict, "set": set, "frozenset": frozenset, "type": type, "any": any,
              "all": all, "min": min, "max": max, "abs": abs, "sorted": sorted, "range": range, "enumerate": enumerate,
              "zip": zip, "reversed": reversed}
# Builtins that build or walk a sequence: their arguments must be parameters (whose values are size-checked before
# evaluation), small literals, or a whitelisted method of a parameter, never another call or an expression, so
# `len(list(range(1000000)) * 1000000)` cannot be built here. `bytes` is left out entirely (`bytes(10**9)`).
SIZED_FUNCS = {"list", "tuple", "dict", "set", "frozenset", "sorted", "range", "enumerate", "zip", "reversed", "any", "all",
               "min", "max", "str", "int", "float"}
_GROWING = (ast.Mult, ast.Mod, ast.FloorDiv, ast.Div)
SAFE_METHODS = {"startswith", "endswith", "get", "keys", "values", "items", "lower", "upper", "strip", "lstrip", "rstrip",
                "split", "rsplit", "isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower", "isidentifier",
                "isnumeric", "isdecimal", "count", "find", "casefold", "partition", "rpartition", "splitlines", "title"}
RE_CALLS = {"match", "search", "fullmatch"}
RE_FLAGS = {"I", "IGNORECASE", "M", "MULTILINE", "S", "DOTALL", "X", "VERBOSE", "A", "ASCII", "U", "UNICODE"}
_OK_NODES = (ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd, ast.Compare, ast.Constant, ast.Tuple,
             ast.List, ast.Set, ast.Load, ast.Subscript, ast.Slice, ast.BinOp, ast.Add, ast.Sub, ast.Mod, ast.Mult,
             ast.FloorDiv, ast.Div, ast.keyword, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
             ast.In, ast.NotIn, ast.BitOr, ast.BitAnd, ast.Dict)


def _evaluable(node, params: set, regex_names: set) -> bool:
    """True if node uses only parameters, literals, SAFE_FUNCS, SAFE_METHODS and re matching: nothing that could run
    project code or touch the system when evaluated here."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            if n.id in params or n.id in SAFE_FUNCS or n.id in regex_names or n.id == "re":
                continue
            return False
        if isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                return False
            base_name = n.value.id if isinstance(n.value, ast.Name) else None
            if base_name == "re" and n.attr in RE_CALLS | RE_FLAGS:
                continue
            if base_name in regex_names and n.attr in RE_CALLS:
                continue
            if base_name != "re" and n.attr in SAFE_METHODS:
                continue
            return False
        if isinstance(n, ast.Call):
            # its func is a Name or an Attribute, checked on its own; no *args or **kwargs
            if any(isinstance(a, ast.Starred) for a in n.args) or any(k.arg is None for k in n.keywords):
                return False
            if isinstance(n.func, ast.Name) and n.func.id in SIZED_FUNCS and not all(
                    _small_arg(a, params) for a in list(n.args) + [k.value for k in n.keywords]):
                return False
            continue
        if isinstance(n, ast.BinOp) and isinstance(n.op, _GROWING):
            # `x * 2`, `x % 2`, `n // 10`: one side a parameter, the other a small number (or both small literals);
            # never `'a' * 10**9`, `'%999999999s' % x` or `xs * ys`.
            sides = (n.left, n.right)
            if not all(_small_number(x) or _param(x, params) for x in sides) or all(_param(x, params) for x in sides):
                return False
            continue
        if not isinstance(n, _OK_NODES):
            return False
    return True


def _small_number(node) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)
            and abs(node.value) <= SMALL_FACTOR)


def _small_arg(node, params: set) -> bool:
    """An argument a sequence builtin may take: a parameter, a small literal, or a whitelisted method of a parameter."""
    if _param(node, params):
        return True
    if isinstance(node, ast.Constant):
        v = node.value
        return v is None or isinstance(v, bool) or (isinstance(v, (int, float)) and abs(v) <= MAX_SIZE) or \
            (isinstance(v, (str, bytes)) and len(v) <= MAX_SIZE)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return len(node.elts) <= 100 and all(_small_arg(e, params) for e in node.elts)
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and _param(node.func.value, params) is not None
            and node.func.attr in SAFE_METHODS and not node.args)


def _small_value(v, depth: int = 0) -> bool:
    """A plain value a condition may read here: no text or container over MAX_SIZE, no number over 10**12."""
    if isinstance(v, bool) or v is None:
        return True
    if isinstance(v, (int, float)):
        return abs(v) <= 10**12 if v == v and v not in (float("inf"), float("-inf")) else True
    if isinstance(v, (str, bytes, bytearray)):
        return len(v) <= MAX_SIZE
    if isinstance(v, (list, tuple, set, frozenset)):
        return len(v) <= MAX_SIZE and (depth >= 3 or all(_small_value(x, depth + 1) for x in v))
    if isinstance(v, dict):
        return len(v) <= MAX_SIZE and (depth >= 3 or all(_small_value(k, depth + 1) and _small_value(x, depth + 1) for k, x in v.items()))
    return True


def _names(node) -> set:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class _Evaluator:
    def __init__(self, params: set, regexes: dict):
        self.params, self.regexes = params, regexes
        self.cache: dict = {}

    def code(self, node):
        key = id(node)
        got = self.cache.get(key)
        if got is None or got[0] is not node:  # the node is kept with its code, so its id cannot be reused meanwhile
            ok = _evaluable(node, self.params, set(self.regexes))
            got = self.cache[key] = (node, compile(ast.fix_missing_locations(ast.Expression(body=node)), "<condition>", "eval")
                                     if ok else None)
        return got[1]

    def uses_regex(self, node) -> bool:
        return any(isinstance(n, ast.Name) and (n.id == "re" or n.id in self.regexes) for n in ast.walk(node))

    def value(self, node, state: dict):
        """(ok, value). Not ok when the node cannot be evaluated here or a parameter it reads has no plain value."""
        code = self.code(node)
        if code is None:
            return False, None
        local = {}
        for name in _names(node) & self.params:
            v = state.get(name, _NONE_YET)
            if v is _NONE_YET or v is _NOT_PLAIN or not _small_value(v):
                return False, None
            local[name] = v
        from runner.inputs import _RegexSlow, _time_limit
        # The 50 ms timer (inputs._time_limit) is a SIGALRM interval timer: it works only on the main thread and only
        # where signal.setitimer exists. Without it nothing stops a backtracking pattern, so a regex condition is then
        # tried on short text only (2**16 steps at worst); everything else evaluated here is bounded by the size checks
        # above (MAX_SIZE values, no growing operators on large operands), timer or not.
        if self.uses_regex(node) and not _timer_available() and any(
                isinstance(v, (str, bytes)) and len(v) > OFF_MAIN_TEXT for v in _flat(local.values())):
            return False, None
        try:
            with _time_limit(EVAL_S):
                return True, eval(code, {"__builtins__": {}, **SAFE_FUNCS, "re": re, **self.regexes}, local)  # noqa: S307 - whitelisted AST only (_evaluable)
        except (Exception, _RegexSlow):  # noqa: BLE001 - the real code would raise here too: the condition is not met
            return True, _RAISED

    def holds(self, c: Cond, state: dict) -> bool | None:
        """Whether the condition holds for these values: True, False, or None when it cannot be told here."""
        if c.test is None:
            return None
        ok, v = self.value(c.test, state)
        if not ok:
            return None
        if v is _RAISED:
            return False
        if c.kind == "loop":
            try:
                return any(True for _ in itertools.islice(iter(v), 1))
            except Exception:  # noqa: BLE001
                return False
        if c.need is None:
            return None
        try:
            return bool(v) == c.need
        except Exception:  # noqa: BLE001
            return False


_RAISED = object()
OFF_MAIN_TEXT = 16        # longest text a regex condition is tried on off the main thread (as inputs._regex_samples)


def _timer_available() -> bool:
    """True where inputs._time_limit can interrupt: the main thread of a platform with signal.setitimer."""
    import signal
    import threading
    return threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer")



def _flat(values, depth: int = 0):
    for v in values:
        yield v
        if depth < 3 and isinstance(v, (list, tuple, set, frozenset)):
            yield from _flat(v, depth + 1)
        elif depth < 3 and isinstance(v, dict):
            yield from _flat(list(v.keys()) + list(v.values()), depth + 1)


def _score(conds: list[Cond], ev: _Evaluator, state: dict) -> tuple[int, int, int]:
    """(1 if every condition that can be told holds and at least one can, conditions held in order, conditions held)."""
    prefix, held, known, broken = 0, 0, 0, False
    for c in conds:
        h = ev.holds(c, state)
        if h is False:
            broken = True
        elif h is True:
            held += 1
            known += 1
            if not broken:
                prefix += 1
        elif not broken:
            prefix += 1
    return (1 if known and not broken else 0, prefix, held)


# ---- candidate values ----

TRUTHY = ["x", 1, [1], {"k": 1}, True]
FALSY = [None, 0, "", [], {}, False]
TYPE_SAMPLES = {"int": [1, 0], "float": [1.5], "str": ["x", ""], "bytes": [b"x"], "bool": [True], "list": [[1, 2, 3], []],
                "tuple": [(1, 2, 3), ()], "dict": [{"k": 1}, {}], "set": [{1}], "frozenset": [frozenset({1})],
                "Mapping": [{"k": 1}], "MutableMapping": [{"k": 1}], "Sequence": [[1, 2, 3]], "MutableSequence": [[1, 2, 3]],
                "Iterable": [[1, 2, 3]], "Collection": [[1, 2, 3]], "NoneType": [None], "Decimal": []}
STR_TESTS = {"isdigit": "123", "isdecimal": "123", "isnumeric": "123", "isalpha": "abc", "isalnum": "a1", "isspace": " ",
             "isupper": "ABC", "islower": "abc", "isidentifier": "name"}


def _same(a, b) -> bool:
    return type(a) is type(b) and _key(a) == _key(b)


def _key(v) -> str:
    try:
        return json.dumps(lg_codec.encode_input(v), sort_keys=True)
    except Exception:  # noqa: BLE001
        return repr(v)


def _unique(values: list) -> list:
    out, seen = [], set()
    for v in values:
        k = (type(v).__name__, _key(v))
        if k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _const(node):
    try:
        return True, ast.literal_eval(node)
    except Exception:  # noqa: BLE001
        return False, None


def _param(node, params: set) -> str | None:
    return node.id if isinstance(node, ast.Name) and node.id in params else None


def _types(node) -> list[str]:
    """['list', 'tuple'] for isinstance's second argument."""
    items = node.elts if isinstance(node, ast.Tuple) else [node]
    out = []
    for n in items:
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
        elif isinstance(n, ast.Constant) and n.value is None:
            out.append("NoneType")
    return out


def _sized(like, k: int):
    """A value of the same kind as `like` with k items (a string, list, tuple or dict), at most MAX_SIZE."""
    k = min(max(0, k), MAX_SIZE)
    if isinstance(like, str):
        return (like * (k // max(1, len(like)) + 1))[:k] if like else "a" * k
    if isinstance(like, bytes):
        return (like * (k // max(1, len(like)) + 1))[:k] if like else b"a" * k
    if isinstance(like, dict):
        items = list(like.items())[:k]
        return dict(items + [(f"k{i}", i) for i in range(len(items), k)])
    if isinstance(like, tuple):
        return tuple(_sized(list(like), k))
    if isinstance(like, list):
        filler = like[-1] if like else 1
        return (list(like) + [filler] * k)[:k] if like else list(range(1, k + 1))
    return list(range(1, k + 1))


def _cmp_values(c) -> list:
    """Both sides of a comparison with constant c (as Sprint 6.4's guard values)."""
    from runner.inputs import _guard_values
    return [v for v in _guard_values([c]) if v is not None or c is None][:6]


def _regex_examples(pattern: str, flags: int, fn: str) -> list[str]:
    """Strings that `re.<fn>(pattern)` accepts, built by walking the parsed pattern (no Hypothesis, no child process):
    the first branch and the fewest repeats, then other branches and more repeats. Each is checked with re itself."""
    try:
        compiled = re.compile(pattern, flags)
    except (re.error, TypeError, ValueError, OverflowError, RecursionError):
        return []
    try:
        from re import _parser as sre_parse  # Python 3.11+
    except ImportError:  # pragma: no cover - Python 3.10
        import sre_parse  # type: ignore[no-redef]
    try:
        parsed = sre_parse.parse(pattern, flags)
    except Exception:  # noqa: BLE001
        return []
    test = {"match": compiled.match, "fullmatch": compiled.fullmatch}.get(fn, compiled.search)
    from runner.inputs import _RegexSlow, _time_limit
    longest = 200 if _timer_available() else OFF_MAIN_TEXT
    out = []
    for branch, extra in ((0, 0), (0, 1), (1, 0), (2, 0), (0, 2), (1, 1)):
        try:
            s = _walk(parsed, branch, extra, {})
        except Exception:  # noqa: BLE001 - a construct the walker does not know
            continue
        try:
            with _time_limit(EVAL_S):
                if len(s) <= longest and test(s) and s not in out:
                    out.append(s)
        except (Exception, _RegexSlow):  # noqa: BLE001
            continue
        if len(out) >= REGEX_SAMPLES:
            break
    return out


_CATEGORY_CHAR = {"CATEGORY_DIGIT": "1", "CATEGORY_NOT_DIGIT": "a", "CATEGORY_SPACE": " ", "CATEGORY_NOT_SPACE": "a",
                  "CATEGORY_WORD": "a", "CATEGORY_NOT_WORD": "-", "CATEGORY_LINEBREAK": "\n", "CATEGORY_NOT_LINEBREAK": "a"}


def _in_char(items) -> str:
    negate = any(str(op) == "NEGATE" for op, _ in items)
    chars = []
    for op, av in items:
        name = str(op)
        if name == "LITERAL":
            chars.append(chr(av))
        elif name == "RANGE":
            chars.append(chr(av[0]))
        elif name == "CATEGORY":
            chars.append(_CATEGORY_CHAR.get(str(av), "a"))
    if not negate:
        return chars[0] if chars else "a"
    for ch in "axb0 -_Z.":
        if ch not in chars:
            return ch
    return "é"


def _walk(parsed, branch: int, extra: int, groups: dict) -> str:
    out = []
    for op, av in parsed:
        name = str(op)
        if name == "LITERAL":
            out.append(chr(av))
        elif name == "NOT_LITERAL":
            out.append("a" if chr(av) != "a" else "b")
        elif name == "ANY":
            out.append("a")
        elif name == "IN":
            out.append(_in_char(av))
        elif name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            lo, hi, sub = av
            n = max(lo, min(1, hi)) + extra
            if hi is not None and isinstance(hi, int) and hi >= 0 and n > hi:
                n = hi
            out.append("".join(_walk(sub, branch, extra, groups) for _ in range(min(n, 50))))
            if sum(map(len, out)) > MAX_REGEX_TEXT:
                raise ValueError("too long")
        elif name == "SUBPATTERN":
            gid, sub = av[0], av[-1]
            s = _walk(sub, branch, extra, groups)
            if gid is not None:
                groups[gid] = s
            out.append(s)
        elif name == "BRANCH":
            alts = av[1]
            out.append(_walk(alts[min(branch, len(alts) - 1)], branch, extra, groups))
        elif name in ("AT", "ASSERT", "ASSERT_NOT"):
            continue
        elif name == "CATEGORY":
            out.append(_CATEGORY_CHAR.get(str(av), "a"))
        elif name == "GROUPREF":
            out.append(groups.get(av, ""))
        elif name == "ATOMIC_GROUP":
            out.append(_walk(av, branch, extra, groups))
        else:
            raise ValueError(name)
    return "".join(out)


def module_regexes(module_text: str | None) -> dict:
    """{NAME: compiled pattern} for module-level `NAME = re.compile('literal'[, flags])`."""
    out: dict = {}
    if not module_text:
        return out
    try:
        tree = ast.parse(module_text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return out
    from runner.examples import _flags
    for stmt in tree.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            continue
        v = stmt.value
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and isinstance(v.func.value, ast.Name)
                and v.func.value.id == "re" and v.func.attr == "compile" and v.args
                and isinstance(v.args[0], ast.Constant) and isinstance(v.args[0].value, str)):
            flag_node = v.args[1] if len(v.args) > 1 else next((k.value for k in v.keywords if k.arg == "flags"), None)
            flags = _flags(flag_node) if flag_node is not None else 0
            if flags is None:
                continue
            try:
                out[stmt.targets[0].id] = re.compile(v.args[0].value, flags)
            except (re.error, TypeError, ValueError, OverflowError, RecursionError):
                continue
    return out


def _regex_call(node, params: set, regexes: dict):
    """(param, pattern, flags, fn) for re.match(PAT, p[, flags]) / re.search / re.fullmatch and RX.match(p)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in RE_CALLS):
        return None
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id == "re" and len(node.args) >= 2:
        ok, pat = _const(node.args[0])
        p = _param(node.args[1], params)
        if not ok or not isinstance(pat, str) or p is None:
            return None
        from runner.examples import _flags
        flag_node = node.args[2] if len(node.args) > 2 else next((k.value for k in node.keywords if k.arg == "flags"), None)
        flags = _flags(flag_node) if flag_node is not None else 0
        return (p, pat, flags or 0, node.func.attr) if flags is not None else None
    if isinstance(owner, ast.Name) and owner.id in regexes and node.args:
        p = _param(node.args[0], params)
        if p is not None:
            rx = regexes[owner.id]
            return p, rx.pattern, rx.flags, node.func.attr
    return None


def _key_access(node, params: set):
    """(dict param, key node) for p.get(K) and p[K]."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        p = _param(node.func.value, params)
        return (p, node.args[0]) if p else None
    if isinstance(node, ast.Subscript):
        p = _param(node.value, params)
        return (p, node.slice) if p else None
    return None


class _Plan:
    """Candidate values per parameter, and the steps that build one value meeting the whole chain."""

    def __init__(self, params: set, regexes: dict):
        self.params, self.regexes = params, regexes
        self.pool: dict[str, list] = {}
        self.steps: list = []  # functions state -> None, applied in chain order
        self.too_large: list = []  # lengths a condition needs that are above MAX_SIZE: nothing is built for the line

    def add(self, p: str, values: list):
        self.pool.setdefault(p, []).extend(values)


def _set_key(state: dict, p: str, key, value) -> None:
    cur = state.get(p)
    d = dict(cur) if isinstance(cur, dict) else {}
    d[key] = value
    state[p] = d


def _atoms(test, need):
    """[(atom, need)]: `a and not b` needing True gives (a, True), (b, False); a disjunction gives atoms with need None."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _atoms(test.operand, None if need is None else not need)
    if isinstance(test, ast.BoolOp):
        conj = (isinstance(test.op, ast.And) and need is True) or (isinstance(test.op, ast.Or) and need is False)
        return [a for v in test.values for a in _atoms(v, need if conj else None)]
    if isinstance(test, ast.Compare) and len(test.ops) > 1:
        sides = [test.left] + list(test.comparators)
        return [(ast.Compare(left=sides[i], ops=[op], comparators=[sides[i + 1]]), need) for i, op in enumerate(test.ops)]
    return [(test, need)]


def _truthy_like(cur):
    if isinstance(cur, str):
        return cur or "x"
    if isinstance(cur, (list, tuple, dict, set)) and cur:
        return cur
    if isinstance(cur, (int, float)) and not isinstance(cur, bool):
        return cur or 1
    return "x" if cur is None or cur is _NOT_PLAIN or cur is _NONE_YET else (cur or True)


def _falsy_like(cur):
    """A false value of the same kind: '' for text, 0 for a number, an empty list or dict; None otherwise."""
    if isinstance(cur, bool):
        return False
    if isinstance(cur, (str, bytes, list, tuple, dict, set, frozenset, int, float)):
        return type(cur)()
    return None


def _plan_atom(plan: _Plan, atom, need) -> None:
    params, P = plan.params, (lambda n: _param(n, plan.params))
    want = need is not False  # the side the combined value aims at (a disjunction aims at true)
    # p
    if P(atom):
        p = atom.id
        plan.add(p, (TRUTHY + FALSY) if want else (FALSY + TRUTHY))
        if need is not None:
            plan.steps.append(lambda s, p=p, want=want: s.__setitem__(p, _truthy_like(s.get(p)) if want else _falsy_like(s.get(p))))
        return
    # p.get(K) / p[K] as a truth value
    acc = _key_access(atom, params)
    if acc:
        p, knode = acc
        ok, k = _const(knode)
        if ok:
            plan.add(p, [{k: 1}, {k: "x"}, {k: True}, {k: None}, {}])
            if need is not None:
                plan.steps.append(lambda s, p=p, k=k, want=want: _set_key(s, p, k, 1 if want else None))
        elif P(knode):
            q = knode.id
            plan.steps.append(lambda s, p=p, q=q, want=want: _set_key(s, p, s.get(q), 1 if want else None)
                              if s.get(q) not in (_NOT_PLAIN, _NONE_YET) else None)
        return
    if isinstance(atom, ast.Call):
        _plan_call(plan, atom, need, want)
        return
    if isinstance(atom, ast.Compare) and len(atom.ops) == 1:
        _plan_compare(plan, atom.left, atom.ops[0], atom.comparators[0], need, want)


def _plan_call(plan: _Plan, atom, need, want) -> None:
    params, P = plan.params, (lambda n: _param(n, plan.params))
    f = atom.func
    rx = _regex_call(atom, params, plan.regexes)
    if rx:
        p, pat, flags, fn = rx
        found = _regex_examples(pat, flags, fn)
        plan.add(p, found + ["", "x"] if want else ["", "x"] + found)
        if need is not None and found:
            plan.steps.append(lambda s, p=p, v=found[0], want=want: s.__setitem__(p, v if want else ""))
        return
    if isinstance(f, ast.Name) and f.id == "isinstance" and len(atom.args) == 2 and P(atom.args[0]):
        p, names = atom.args[0].id, _types(atom.args[1])
        samples = [v for t in names for v in TYPE_SAMPLES.get(t, [])]
        plan.add(p, samples + [None] if want else [None, "x", 1] + samples)
        if need is not None and samples:
            def step(s, p=p, names=tuple(names), samples=tuple(samples), want=want):
                cur = s.get(p)
                fits = any(type(cur).__name__ == t or (t in ("Sequence", "Iterable", "Collection") and isinstance(cur, (list, tuple)))
                           or (t in ("Mapping", "MutableMapping") and isinstance(cur, dict)) for t in names)
                if want and not fits:
                    first = names[0]
                    if first in ("list", "tuple", "set", "frozenset") and isinstance(cur, (list, tuple, set, frozenset)):
                        s[p] = {"list": list, "tuple": tuple, "set": set, "frozenset": frozenset}[first](cur)
                    else:
                        s[p] = samples[0]
                elif not want and fits:
                    s[p] = None
            plan.steps.append(step)
        return
    if isinstance(f, ast.Attribute) and P(f.value):
        p, meth = f.value.id, f.attr
        if meth in ("startswith", "endswith") and atom.args:
            ok, lit = _const(atom.args[0])
            parts = [lit] if isinstance(lit, (str, bytes)) else [x for x in lit if isinstance(x, (str, bytes))] if ok and isinstance(lit, tuple) else []
            if ok and parts:
                part = parts[0]
                filler = "x" if isinstance(part, str) else b"x"
                plan.add(p, ([part + filler if meth == "startswith" else filler + part, part] if want else [filler]) + parts[1:3])
                if need is not None:
                    def step(s, p=p, part=part, meth=meth, want=want, filler=filler):
                        cur = s.get(p)
                        cur = cur if isinstance(cur, type(part)) else filler
                        has = cur.startswith(part) if meth == "startswith" else cur.endswith(part)
                        if want and not has:
                            s[p] = part + cur if meth == "startswith" else cur + part
                        elif not want and has:
                            s[p] = filler
                    plan.steps.append(step)
            elif atom.args and P(atom.args[0]):
                q = atom.args[0].id  # p.startswith(q): p built from q's value
                def step(s, p=p, q=q, meth=meth, want=want):
                    qv, cur = s.get(q), s.get(p)
                    if isinstance(qv, str) and want:
                        cur = cur if isinstance(cur, str) else "x"
                        if not (cur.startswith(qv) if meth == "startswith" else cur.endswith(qv)):
                            s[p] = qv + cur if meth == "startswith" else cur + qv
                plan.steps.append(step)
            return
        if meth in STR_TESTS:
            plan.add(p, [STR_TESTS[meth], "-"] if want else ["-", STR_TESTS[meth]])
            if need is not None:
                plan.steps.append(lambda s, p=p, v=STR_TESTS[meth], want=want: s.__setitem__(p, v if want else "-"))
            return


def _plan_compare(plan: _Plan, left, op, right, need, want) -> None:
    params, P = plan.params, (lambda n: _param(n, plan.params))
    lc, lv = _const(left)
    rc, rv = _const(right)
    # p OP const / const OP p
    if P(left) and rc or P(right) and lc:
        p, c, flipped = (left.id, rv, False) if P(left) and rc else (right.id, lv, True)
        if isinstance(op, (ast.In, ast.NotIn)) and not flipped:
            members = list(c)[:4] if isinstance(c, (tuple, list, set, frozenset, dict, str)) else []
            if isinstance(c, (set, frozenset)):
                members = sorted(members, key=repr)
            outside = (members[0] + "x" if members and isinstance(members[0], str) else
                       (max(m for m in members if isinstance(m, (int, float)) and not isinstance(m, bool)) + 1
                        if any(isinstance(m, (int, float)) and not isinstance(m, bool) for m in members) else None))
            yes = isinstance(op, ast.In) == want
            plan.add(p, members + [outside] if yes else [outside] + members)
            if need is not None and members:
                plan.steps.append(lambda s, p=p, v=(members[0] if yes else outside): s.__setitem__(p, v))
            return
        if isinstance(op, (ast.In, ast.NotIn)) and flipped:
            # const in p: a string, list or dict holding it
            yes = isinstance(op, ast.In) == want
            holders = ([f"{c}", f"a{c}b"] if isinstance(c, str) else []) + [[c], {c: 1}]
            plan.add(p, holders if yes else ["", [], {}])
            if need is not None:
                def step(s, p=p, c=c, yes=yes):
                    cur = s.get(p)
                    if yes:
                        if isinstance(cur, str) and isinstance(c, str):
                            s[p] = cur if c in cur else cur + c
                        elif isinstance(cur, dict):
                            s[p] = cur if c in cur else {**cur, c: 1}
                        elif isinstance(cur, (list, tuple)):
                            s[p] = cur if c in cur else type(cur)(list(cur) + [c])
                        else:
                            s[p] = c if isinstance(c, str) else [c]
                    elif isinstance(cur, (str, list, tuple, dict)) and c in cur:
                        s[p] = type(cur)()
                plan.steps.append(step)
            return
        if isinstance(op, (ast.Is, ast.IsNot)) and c is None:
            yes = isinstance(op, ast.Is) == want
            plan.add(p, [None, "x", 1, [1]] if yes else ["x", 1, [1], None])
            if need is not None:
                plan.steps.append(lambda s, p=p, yes=yes: s.__setitem__(p, None) if yes else
                                  s.__setitem__(p, _truthy_like(s.get(p)) if s.get(p) in (None, _NOT_PLAIN, _NONE_YET) else s.get(p)))
            return
        values = _cmp_values(c)
        plan.add(p, values)
        if need is not None:
            def step(s, p=p, op=op, c=c, flipped=flipped, want=want, values=tuple(values)):
                for v in (s.get(p),) + values:
                    if v in (_NOT_PLAIN, _NONE_YET):
                        continue
                    try:
                        got = _compare(op, c, v) if flipped else _compare(op, v, c)
                    except Exception:  # noqa: BLE001
                        continue
                    if got == want:
                        s[p] = v
                        return
            plan.steps.append(step)
        return
    # len(p) OP n
    for side, other, other_const, flip in ((left, right, (rc, rv), False), (right, left, (lc, lv), True)):
        if (isinstance(side, ast.Call) and isinstance(side.func, ast.Name) and side.func.id == "len" and len(side.args) == 1
                and P(side.args[0]) and other_const[0] and isinstance(other_const[1], int)):
            p, n = side.args[0].id, other_const[1]
            sizes = [k for k in (0, n - 1, n, n + 1, MAX_SIZE) if 0 <= k <= MAX_SIZE]
            try:
                fits = [k for k in sizes if (_compare(op, n, k) if flip else _compare(op, k, n)) == want]
            except Exception:  # noqa: BLE001
                fits = sizes
            if need is not None and not fits:
                # `len(xs) > 50000000`: a value that long is not built in the guard's process (nor sent to the worker).
                plan.too_large.append(n + 1 if n >= 0 else n)
                return
            plan.add(p, [_sized(x, k) for k in (n + 1, n, max(0, n - 1)) if 0 <= k <= MAX_SIZE for x in ("a", [1])])
            if need is not None:
                def step(s, p=p, n=n, op=op, flip=flip, want=want):
                    cur = s.get(p)
                    like = cur if isinstance(cur, (str, bytes, list, tuple, dict)) else s.get(_LIKE, [1])
                    for k in (len(like), n + 1, n, n - 1, n + 2, 0):
                        if k < 0 or k > MAX_SIZE:
                            continue
                        try:
                            got = _compare(op, n, k) if flip else _compare(op, k, n)
                        except Exception:  # noqa: BLE001
                            continue
                        if got == want:
                            s[p] = _sized(like, k)
                            return
                plan.steps.append(step)
            return
    # p.method() == const (p.lower() == 'yes', p.strip() == '')
    if (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute) and P(left.func.value)
            and left.func.attr in SAFE_METHODS and rc and isinstance(rv, str)):
        p = left.func.value.id
        plan.add(p, [rv, rv.upper(), f" {rv} ", rv + "x"])
        if need is not None and isinstance(op, ast.Eq) and want:
            plan.steps.append(lambda s, p=p, v=rv: s.__setitem__(p, v))
        return
    # p.get(K) OP const / p[K] OP const
    acc = _key_access(left, params)
    if acc and rc:
        p, knode = acc
        ok, k = _const(knode)
        if ok:
            vals = _cmp_values(rv)[:4]
            plan.add(p, [{k: v} for v in vals])
            if need is not None:
                def step(s, p=p, k=k, op=op, c=rv, want=want, vals=tuple(vals)):
                    for v in vals:
                        try:
                            if _compare(op, v, c) == want:
                                _set_key(s, p, k, v)
                                return
                        except Exception:  # noqa: BLE001
                            continue
                plan.steps.append(step)
        return
    # p % k OP c
    if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Mod) and P(left.left) and rc:
        okk, k = _const(left.right)
        if okk and isinstance(k, int) and k and isinstance(rv, int):
            p = left.left.id
            plan.add(p, [rv, rv + k, rv + 1])
            if need is not None and isinstance(op, ast.Eq) and want:
                plan.steps.append(lambda s, p=p, v=rv + k: s.__setitem__(p, v))
        return
    # two parameters: p OP q, p in q
    if P(left) and P(right):
        p, q = left.id, right.id

        def step(s, p=p, q=q, op=op, want=want):
            pv, qv = s.get(p), s.get(q)
            if pv in (_NOT_PLAIN, _NONE_YET):
                return
            if isinstance(op, (ast.In, ast.NotIn)):
                yes = isinstance(op, ast.In) == want
                if not yes:
                    return
                if not isinstance(qv, dict) and isinstance(s.get(_LIKE), list) and _hashable(pv):
                    s[q] = {pv: 1}  # the attempt that builds containers: a mapping holding p, so q[p] works too
                elif isinstance(qv, dict):
                    s[q] = {**qv, pv: qv.get(pv, 1)} if _hashable(pv) else qv
                elif isinstance(qv, str) and isinstance(pv, str):
                    s[q] = qv if pv in qv else qv + pv
                elif isinstance(qv, (list, tuple)):
                    s[q] = qv if pv in qv else type(qv)(list(qv) + [pv])
                else:
                    s[q] = pv if isinstance(pv, str) else [pv]
            elif isinstance(op, (ast.Eq, ast.Is)) and want or isinstance(op, (ast.NotEq, ast.IsNot)) and not want:
                s[q] = pv
            elif isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) and isinstance(pv, (int, float)) and not isinstance(pv, bool):
                for v in (pv + 1, pv - 1, pv):
                    if _compare(op, pv, v) == want:
                        s[q] = v
                        return
        plan.steps.append(step)


def _hashable(v) -> bool:
    try:
        hash(v)
        return True
    except TypeError:
        return False


def _compare(op, a, b) -> bool:
    return {ast.Eq: lambda: a == b, ast.NotEq: lambda: a != b, ast.Lt: lambda: a < b, ast.LtE: lambda: a <= b,
            ast.Gt: lambda: a > b, ast.GtE: lambda: a >= b, ast.Is: lambda: a is b, ast.IsNot: lambda: a is not b,
            ast.In: lambda: a in b, ast.NotIn: lambda: a not in b}[type(op)]()


def _plan_loop(plan: _Plan, it) -> None:
    """A for loop's iterable must not be empty: p, p.items(), range(p), enumerate(p), zip(p, ...), sorted(p)."""
    P = lambda n: _param(n, plan.params)  # noqa: E731
    target = None
    if P(it):
        target = (it.id, "seq")
    elif isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) and it.func.attr in ("items", "keys", "values") and P(it.func.value):
        target = (it.func.value.id, "dict")
    elif isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.args and P(it.args[0]):
        target = (it.args[0].id, "int" if it.func.id == "range" else "seq")
    if target is None:
        return
    p, kind = target
    values = {"seq": [[1, 2], ["a"], "ab", {"k": 1}], "dict": [{"k": 1}], "int": [1, 3]}[kind]
    plan.add(p, values)

    def step(s, p=p, kind=kind, values=tuple(values)):
        cur = s.get(p)
        if kind == "int":
            if not (isinstance(cur, int) and cur > 0):
                s[p] = 1
        elif kind == "dict":
            if not (isinstance(cur, dict) and cur):
                s[p] = {**(cur if isinstance(cur, dict) else {}), "k": 1}
        elif not (isinstance(cur, (list, tuple, str, dict, set)) and cur):
            s[p] = list(cur) + [1] if isinstance(cur, (list, tuple)) else values[0]
    plan.steps.append(step)


def _plan_for(conds: list[Cond], params: set, regexes: dict) -> _Plan:
    plan = _Plan(params, regexes)
    for c in conds:
        if c.test is None:
            continue
        if c.kind == "loop":
            _plan_loop(plan, c.test)
            continue
        for atom, need in _atoms(c.test, c.need):
            try:
                _plan_atom(plan, atom, need)
            except Exception:  # noqa: BLE001 - one odd condition must not cost the others
                continue
    return plan


# ---- building the inputs ----

_PLAIN_TAGS = {"v", "bigint", "float", "Decimal", "bytes", "bytearray", "list", "tuple", "set", "frozenset", "dict"}


def _plain(enc):
    """The value of an encoded argument when it is a plain literal (decoded here without importing anything), or
    _NOT_PLAIN for objects, recipes, pickles and stand-ins."""
    def ok(e) -> bool:
        if not isinstance(e, dict) or e.get("t") not in _PLAIN_TAGS:
            return False
        t, v = e["t"], e.get("v")
        if t in ("list", "tuple", "set", "frozenset"):
            return all(ok(x) for x in v)
        if t == "dict":
            return all(ok(k) and ok(x) for k, x in v)
        return True
    if not ok(enc):
        return _NOT_PLAIN
    try:
        return lg_codec.decode(enc)
    except Exception:  # noqa: BLE001
        return _NOT_PLAIN


def _fits(value, param: dict) -> bool:
    """A value a parameter's type hint allows. No hint: anything (each value comes from a condition on it)."""
    ann = (param.get("annotation") or "").replace("typing.", "").replace(" ", "").strip("\"'")
    if not ann:
        return True
    from runner.inputs import _fits_hint
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return _fits_hint(value, ann)
    outer = re.split(r"[\[|]", re.sub(r"^Optional\[(.+)\]$", r"\1", ann))[0]
    kinds = {"list": (list,), "List": (list,), "Sequence": (list, tuple), "MutableSequence": (list,), "Iterable": (list, tuple, set, dict, str),
             "Collection": (list, tuple, set, dict), "tuple": (tuple,), "Tuple": (tuple,), "dict": (dict,), "Dict": (dict,),
             "Mapping": (dict,), "MutableMapping": (dict,), "set": (set,), "Set": (set,), "frozenset": (frozenset,)}
    return isinstance(value, kinds.get(outer, ()))


def _encode(value):
    try:
        return lg_codec.encode_input(value)
    except Exception:  # noqa: BLE001
        return None


def _item_from(base: dict, state: dict, changed: list[str], params: dict) -> dict | None:
    """A copy of base with the changed parameters set from state. A value the parameter's hint rules out, or that
    cannot be encoded, is left as the base had it; None when nothing is left to change."""
    args = [dict(a) for a in base["args"]]
    done = 0
    for p in changed:
        enc = _encode(state[p])
        if enc is None or not _fits(state[p], params[p]):
            continue
        done += 1
        at = next((i for i, a in enumerate(args) if a.get("name") == p), None)
        if at is None:
            args.append({"name": p, "kind": "kw", "value": enc})
        else:
            args[at] = {**args[at], "value": enc}
    if not done:
        return None
    out = {"args": args, "has_self": bool(base.get("has_self")), "source": "feedback", "weak": True}
    if base.get("source"):
        out["base"] = base["source"]
    return out


def _depth(conds: list[Cond], lines) -> tuple[int, int]:
    """(conditions got past in order, conditions whose test was reached) for one input's traced lines."""
    if lines is None:
        return 0, 0
    done = set(lines)
    passed = 0
    for c in conds:
        if c.inside and c.inside in done:
            passed += 1
        else:
            break
    return passed, sum(1 for c in conds if c.header in done)


def _bases(items: list[dict], input_lines: list | None, conds: list[Cond]) -> list[dict]:
    """The first-pass inputs that got furthest along the chain: most conditions passed in order, then most tested,
    then real inputs (tests, carved, examples) before guesses, then first."""
    ranked = []
    for i, item in enumerate(items):
        if item.get("expect") is not None:
            continue
        lines = input_lines[i] if input_lines and i < len(input_lines) else None
        passed, tested = _depth(conds, lines)
        ran = lines is not None and bool(lines)
        ranked.append(((-passed, -tested, not ran, bool(item.get("weak")) or item.get("source") in ("edit", "feedback"), i), item))
    ranked.sort(key=lambda t: t[0])
    out, seen = [], set()
    for _, item in ranked:
        sig = json.dumps([item.get("args"), bool(item.get("has_self"))], sort_keys=True)
        if sig not in seen:
            seen.add(sig)
            out.append(item)
        if len(out) >= MAX_FEEDBACK_BASES:
            break
    return out


_LIKE = "\0like"  # in a building state: the kind a value of unknown kind takes ("a" text, [1] a list)


def _add_combined(mine: list, order, base: dict, start: dict, state: dict, tested: list, params: dict, conds: list, ev) -> None:
    """The combined value set for every tested parameter at once (rank 0), then for one parameter at a time (rank 1)."""
    changed = [p for p in tested if state.get(p) not in (_NONE_YET, _NOT_PLAIN) and not _same(state.get(p), start.get(p))]
    if not changed:
        return
    item = _item_from(base, state, changed, params)
    if item:
        mine.append(((0,) + tuple(-x for x in _score(conds, ev, state)), next(order), item))
    for p in changed:
        one = {**start, p: state[p]}
        item = _item_from(base, one, [p], params)
        if item:
            mine.append(((1,) + tuple(-x for x in _score(conds, ev, one)), next(order), item))


def _default(param: dict):
    """A parameter's default as a plain value (the base leaves it out, so the function sees this), else _NONE_YET."""
    text = param.get("default")
    if not text:
        return _NONE_YET
    try:
        ok, value = _const(ast.parse(text, mode="eval").body)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _NONE_YET
    return value if ok else _NONE_YET


def _sig(item: dict) -> str:
    return json.dumps([item.get("args"), bool(item.get("has_self"))], sort_keys=True)


def build(sym: dict, items: list[dict], input_lines: list | None, plans: list[dict], module_text: str | None = None,
          limit: int = MAX_FEEDBACK_ITEMS, deadline: float | None = None, notes: list | None = None) -> list[dict]:
    """Second-pass inputs for the unreached statements in plans (from diagnose), at most `limit`, none already among
    `items`. Dead statements get none, nor do statements behind a condition that needs a value longer than MAX_SIZE
    (said in `notes`). Deterministic for the same arguments."""
    params = {p["name"]: p for p in sym.get("params") or [] if p["name"] not in ("self", "cls") and not p["name"].startswith("*")}
    if not params or not items:
        return []
    names = set(params)
    regexes = module_regexes(module_text)
    ev = _Evaluator(names, regexes)
    have = {_sig(i) for i in items}
    live = [pl for pl in plans if not pl.get("dead") and pl.get("conds")]
    if not live:
        return []
    share = max(4, limit // len(live))
    out: list[dict] = []
    for pl in live:
        if len(out) >= limit or (deadline is not None and time.monotonic() > deadline):
            break
        conds = pl["conds"]
        plan = _plan_for(conds, names, regexes)
        if plan.too_large:
            if notes is not None:
                notes.append(f"a condition needs a value of length {plan.too_large[0]}; too large to build")
            continue
        tested = [p for p in params if p in plan.pool or any(p in _names(c.test) for c in conds if c.test is not None)]
        if not tested:
            continue
        mine: list[tuple] = []  # (rank, order, item)
        order = itertools.count()
        late = (lambda: False) if deadline is None else (lambda: time.monotonic() > deadline)
        for base in _bases(items, input_lines, conds):
            if late():
                break
            start = {a["name"]: _plain(a["value"]) for a in base.get("args") or [] if a.get("kind") not in ("varargs", "varkw")}
            for p in params:
                if p not in start:
                    start[p] = _default(params[p])
            # 1. every tested parameter at once, built to meet the chain step by step: two sweeps, so a step that reads
            # another parameter (q.startswith(p)) sees what a later step made of it; a value of unknown kind becomes
            # text in one attempt and a list in the other.
            for like in ("a", [1]):
                state = {**start, _LIKE: like}
                for _ in range(2):
                    for step in plan.steps:
                        try:
                            step(state)
                        except Exception:  # noqa: BLE001
                            continue
                state.pop(_LIKE, None)
                _add_combined(mine, order, base, start, state, tested, params, conds, ev)
            # 2. combinations of the candidates for the tested parameters, kept when they meet every condition told here
            pools = {}
            for p in tested:
                own = [start[p]] if start.get(p) not in (_NONE_YET, _NOT_PLAIN) else []
                flip = [not start[p]] if isinstance(start.get(p), bool) else []  # a boolean default, the other way
                pools[p] = [v for v in _unique(own + flip + plan.pool.get(p, [])) if _fits(v, params[p])][:MAX_POOL]
            keys = [p for p in tested if pools.get(p)]
            if 1 <= len(keys) <= 3:
                for tried, combo in enumerate(itertools.islice(itertools.product(*[pools[p] for p in keys]), MAX_COMBOS)):
                    if tried % 32 == 31 and late():
                        break
                    cand = {**start, **dict(zip(keys, combo))}
                    sc = _score(conds, ev, cand)
                    if not sc[0]:
                        continue
                    ch = [p for p in keys if not _same(cand[p], start.get(p))]
                    item = _item_from(base, cand, ch, params) if ch else None
                    if item:
                        mine.append(((0,) + tuple(-x for x in sc), next(order), item))
            # 3. each candidate alone
            for p in keys:
                for v in pools[p]:
                    one = {**start, p: v}
                    if _same(v, start.get(p)):
                        continue
                    item = _item_from(base, one, [p], params)
                    if item:
                        mine.append(((2,) + tuple(-x for x in _score(conds, ev, one)), next(order), item))
        mine.sort(key=lambda t: (t[0], t[1]))
        taken = 0
        for _, _, item in mine:
            sig = _sig(item)
            if sig in have:
                continue
            have.add(sig)
            out.append(item)
            taken += 1
            if taken >= share or len(out) >= limit:
                break
    return out[:limit]
