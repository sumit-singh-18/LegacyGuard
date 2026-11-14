"""Planted bugs (mutants) on the changed lines of the old function, to measure how strong a pass is (roadmap B6).

A pass says "old and new agreed on these inputs". Whether that means anything depends on the inputs: if they cannot
tell `x < limit` from `x <= limit`, they would not have caught that bug in the edit either. So after a pass the old
function is run again with small bugs planted in exactly the lines the edit changed. The share of planted bugs the
same inputs catch is the confidence reported with the pass. Mutation score predicts fault detection far better than
coverage (Meta's ACH; MutGen found suites with 100% coverage and a 4% mutation score).

Only plain functions and methods are mutated: the source is compiled again in the module's namespace, which
decorators, closures and zero-argument super() do not survive. The worker first runs the unmutated recompiled source
(the identity) and gives up if that alone behaves differently from the real function.

Besides the arithmetic and logic operators there are two families for edits that only touch text:

- strings, modelled on mutmut: a literal wrapped in "XX", emptied, or with its case changed (escape sequences are
  kept intact, and encoding names given to encode/decode/codecs get no case change; docstrings, triple-quoted
  strings that are not regex patterns, f-string parts, annotations and print/logging/sys.std*.write messages are
  left alone), and string methods swapped with their mirror (lower/upper, lstrip/rstrip, find/rfind, ...;
  split/rsplit only when maxsplit is given, since they are equivalent otherwise);
- regexes, a cheap subset of MutRex (Arcaini, Gargantini, Riccobene, Mutation 2017) on literals used as patterns
  by re.compile/match/search/fullmatch/sub/subn/split/findall/finditer: CCR (drop one item of a character class),
  RM (shift a range end by one), QC (quantifier change), M2C (unescaped . -> \\.), CCN (negate a class) and CA (a
  letter also matches its other case; not under ignore-case). Every regex mutant must still compile with
  re.compile and the call's flags here, in the guard process. In verbose mode whitespace and comments are not
  mutated. Lookaround bodies, backreferences and the groups they refer to are outside MutRex's grammar and are
  left alone; the rest of such a pattern is mutated.

Python's re is not regular, so equivalent regex mutants cannot be ruled out by an automata check (MutRex measured
2.3% equivalent mutants). A surviving mutant is reported with its operator, which says what the inputs failed to
distinguish.
"""
from __future__ import annotations

import ast
import copy
import re
import textwrap
import warnings

MAX_MUTANTS = 8
_CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
             ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.Div: ast.Mult, ast.FloorDiv: ast.Mult,
             ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult}
_BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}
_CORE = ("cmp", "bin", "bool", "not", "const", "flipbool", "return")

_METHOD_PAIRS = [("lower", "upper"), ("lstrip", "rstrip"), ("startswith", "endswith"), ("find", "rfind"),
                 ("index", "rindex"), ("split", "rsplit"), ("partition", "rpartition"),
                 ("removeprefix", "removesuffix"), ("ljust", "rjust")]
_METHOD_SWAP = {**{a: b for a, b in _METHOD_PAIRS}, **{b: a for a, b in _METHOD_PAIRS}}
_NOT_STRINGS = {"os", "path", "re", "shlex"}  # receivers whose .split() and friends are not str methods
_QUIET_CALLS = {"print", "debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
RE_FUNCS = {"compile", "match", "search", "fullmatch", "sub", "subn", "split", "findall", "finditer"}
# For a bare sub(...)/split(...) whose origin is unknown: only a literal that clearly is a regex counts.
_CLEARLY_REGEX = re.compile(r"\\[dDwWsSbBAZ]|\[[^\]]+\]|\(\?|[^\\][*+?]|^\^|\$$|\{\d+(,\d*)?\}")
_MAX_REGEX_CANDIDATES = 200


def _in_gate(node, start: int, gate: list[list[int]]) -> bool:
    line = getattr(node, "lineno", None)
    if line is None:
        return False
    absolute = line + start - 1
    return any(a <= absolute <= b for a, b in gate)


def _text_lines(node, start: int, gate: list[list[int]]) -> set | None:
    """None for anything but a str constant. For a str constant: the line offsets inside its value (0 = the line
    it starts on) that are changed lines; empty when none is. A triple-quoted pattern spans several lines and an edit
    may change only one of them, so a regex mutant counts only on a changed line of the value. When the value's
    newlines do not match the source lines (escapes, implicit concatenation), every offset counts as changed once
    any of its lines is."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    first, last = node.lineno + start - 1, (getattr(node, "end_lineno", None) or node.lineno) + start - 1
    changed = {line - first for line in range(first, last + 1) if any(a <= line <= b for a, b in gate)}
    if changed and node.value.count("\n") != last - first:
        return set(range(node.value.count("\n") + 1))
    return changed


def _sites(fn: ast.AST, start: int, gate: list[list[int]], text: str = "", re_names: dict | None = None) -> list[tuple]:
    """(kind, path, extra) for every mutable spot on a changed line, in source order. path finds the node in a fresh
    copy. For strings, methods and regexes extra is (new value, change text)."""
    re_names = re_names or {}
    patterns, skipped, nocase = _text_roles(fn, text, re_names)
    sites = []
    for path, node in _walk(fn, ()):
        lines = _text_lines(node, start, gate)
        if lines is not None:
            if not lines:
                continue
            if id(node) in skipped:
                continue
            if id(node) in patterns:
                flags = patterns[id(node)]
                sites += [("regex", path, extra) for extra in
                          _regex_mutants(node.value, verbose="x" in flags, ignorecase="i" in flags, lines=lines)]
            else:
                sites += [("str", path, extra) for extra in _string_mutants(node.value, case=id(node) not in nocase)]
            continue
        if not _in_gate(node, start, gate):
            continue
        if isinstance(node, ast.Compare):
            sites += [("cmp", path, i) for i, op in enumerate(node.ops) if type(op) in _CMP_SWAP]
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN_SWAP:
            sites.append(("bin", path, None))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            sites.append(("bool", path, None))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append(("not", path, None))
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float) and not isinstance(node.value, bool):
            sites.append(("const", path, None))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            sites.append(("flipbool", path, None))
        elif isinstance(node, ast.Return) and node.value is not None:
            sites.append(("return", path, None))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _METHOD_SWAP \
                and _string_receiver(node.func.value, re_names) \
                and (node.func.attr not in ("split", "rsplit") or len(node.args) >= 2
                     or any(k.arg == "maxsplit" for k in node.keywords)):
            new = _METHOD_SWAP[node.func.attr]
            sites.append(("method", path, (new, f"method .{node.func.attr}() -> .{new}()")))
    return sites


# ---- strings (mutmut) ----

def _short(text: str, width: int = 24) -> str:
    return repr(text if len(text) <= width else text[:width] + "...")


def _swap_case(value: str, upper: bool) -> str:
    """Case change that leaves the character after a backslash alone, so an escape like \\d keeps its meaning."""
    out, run = [], 0  # run: backslashes right before this character; an odd run escapes it
    for ch in value:
        out.append(ch if run % 2 else (ch.upper() if upper else ch.lower()))
        run = run + 1 if ch == "\\" else 0
    return "".join(out)


def _string_mutants(value: str, case: bool = True) -> list[tuple[str, str]]:
    out = [("XX" + value + "XX", f"string {_short(value)} -> {_short('XX' + value + 'XX')}")]
    if value:
        out.append(("", f"string {_short(value)} -> ''"))
    if not case:
        return out
    other = _swap_case(value, upper=True)
    if other == value:
        other = _swap_case(value, upper=False)
    if other != value:
        out.append((other, f"string {_short(value)} -> {_short(other)}"))
    return out


def _string_receiver(node, re_names: dict) -> bool:
    """False for module-like receivers (re.split, os.path.split), where the swapped name is not a str method."""
    if isinstance(node, ast.Name):
        return node.id not in _NOT_STRINGS and node.id not in re_names.get("modules", ())
    if isinstance(node, ast.Attribute):
        return node.attr not in _NOT_STRINGS
    return True


def _triple_quoted(node, text: str) -> bool:
    segment = ast.get_source_segment(text, node) if text else None
    if not segment:
        return False
    body = segment.lstrip("rRbBuUfF")
    return body.startswith('"""') or body.startswith("'''")


# ---- regexes (MutRex subset) ----

def re_aliases(module_source: str | None) -> dict:
    """{"modules": names bound to the re module, "funcs": {local name: re function}} from `import re as x` and
    `from re import sub as s` in the given source."""
    out = {"modules": {"re"}, "funcs": {}}
    if module_source:
        try:
            with warnings.catch_warnings():  # old project code may warn (invalid escapes); never on stderr
                warnings.simplefilter("ignore")
                _collect_re_imports(ast.parse(module_source), out)
        except SyntaxError:
            pass
    return out


def _collect_re_imports(tree, out: dict) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out["modules"].update(a.asname or a.name for a in node.names if a.name == "re")
        elif isinstance(node, ast.ImportFrom) and node.module == "re" and not node.level:
            out["funcs"].update({a.asname or a.name: a.name for a in node.names if a.name in RE_FUNCS})


def _re_call(call: ast.Call, names: dict) -> str:
    """"sure" for re.sub(...) and imported aliases, "maybe" for a bare sub(...) of unknown origin, "" otherwise."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in RE_FUNCS and isinstance(func.value, ast.Name) \
            and func.value.id in names["modules"]:
        return "sure"
    if isinstance(func, ast.Name):
        if func.id in names["funcs"]:
            return "sure"
        if func.id in RE_FUNCS:
            return "maybe"
    return ""


def _compiles(pattern: str, flags: int = 0) -> bool:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            re.compile(pattern, flags)
        return True
    except Exception:  # noqa: BLE001 - re.error, OverflowError, RecursionError: not a usable pattern
        return False


def _quiet_call(call: ast.Call) -> bool:
    """print, logging and warnings calls, and sys.stdout/sys.stderr.write: their text is not compared."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _QUIET_CALLS
    if isinstance(func, ast.Attribute):
        if func.attr == "write":
            stream = func.value
            return isinstance(stream, ast.Attribute) and stream.attr in ("stdout", "stderr") \
                and isinstance(stream.value, ast.Name) and stream.value.id == "sys"
        return func.attr in _QUIET_CALLS
    return False


def _codec_call(call: ast.Call) -> bool:
    """x.encode(...), x.decode(...) and codecs.*(...): encoding names ignore case."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("encode", "decode"):
            return True
        return isinstance(func.value, ast.Name) and func.value.id == "codecs"
    return False


# Position of the flags argument of each re function, after the pattern.
_FLAGS_AT = {"compile": 1, "match": 2, "search": 2, "fullmatch": 2, "findall": 2, "finditer": 2, "split": 3,
             "sub": 4, "subn": 4}
_INLINE_FLAGS = re.compile(r"\(\?([aiLmsux]+)\)")
_SCOPED_FLAGS = re.compile(r"\?(?:[aiLmsux]+(?:-[imsx]+)?|-[imsx]+):")


def _call_flags(call: ast.Call, func_name: str) -> set:
    """{"x", "i"} for re.X/re.VERBOSE and re.I/re.IGNORECASE passed to the call, positionally or as flags=."""
    at = _FLAGS_AT.get(func_name)
    expr = call.args[at] if at is not None and len(call.args) > at else \
        next((k.value for k in call.keywords if k.arg == "flags"), None)
    out = set()
    for n in ast.walk(expr) if expr is not None else ():
        name = n.attr if isinstance(n, ast.Attribute) else n.id if isinstance(n, ast.Name) else ""
        if name in ("X", "VERBOSE"):
            out.add("x")
        elif name in ("I", "IGNORECASE"):
            out.add("i")
    return out


def _re_func_name(call: ast.Call, names: dict) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return names["funcs"].get(func.id, func.id) if isinstance(func, ast.Name) else ""


def _text_roles(fn: ast.AST, text: str, re_names: dict) -> tuple[dict, set, set]:
    """({id of a str constant used as a regex pattern: its flags {"x", "i"}}, ids of str constants never mutated,
    ids of str constants that get no case change)."""
    names = {"modules": set(re_names.get("modules", ())) | {"re"}, "funcs": dict(re_names.get("funcs", {}))}
    _collect_re_imports(fn, names)
    skipped, triple, nocase = set(), set(), set()
    assigned = {}  # local name -> the str constants assigned to it
    for node in ast.walk(fn):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            skipped.add(id(node.value))  # docstrings and other bare strings
        elif isinstance(node, ast.JoinedStr):
            skipped.update(id(n) for n in ast.walk(node))
        elif isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None:
            skipped.update(id(n) for n in ast.walk(node.annotation))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            skipped.update(id(n) for n in ast.walk(node.returns))
        elif isinstance(node, ast.Call):
            args = list(node.args) + [k.value for k in node.keywords]
            if _quiet_call(node):
                skipped.update(id(n) for a in args for n in ast.walk(a))
            elif _codec_call(node):
                nocase.update(id(n) for a in args for n in ast.walk(a))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _triple_quoted(node, text):
            triple.add(id(node))
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            assigned.setdefault(node.targets[0].id, []).append(node.value)
    patterns = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        how = _re_call(node, names)
        if not how:
            continue
        first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "pattern"), None)
        if isinstance(first, ast.Constant):
            found = [first]
        elif isinstance(first, ast.Name):
            found = assigned.get(first.id, [])
        else:
            found = []
        flags = _call_flags(node, _re_func_name(node, names))
        for const in found:
            value = const.value
            if not isinstance(value, str) or id(const) in skipped or not _compiles(value, _re_flags(flags)) \
                    or (how == "maybe" and not _CLEARLY_REGEX.search(value)):
                continue
            inline = {c for m in _INLINE_FLAGS.finditer(value) for c in m.group(1)}
            patterns[id(const)] = flags | (inline & {"x", "i"})
    # A triple-quoted pattern (often verbose) is still mutated as a regex; other triple-quoted text is left alone.
    skipped |= {i for i in triple if i not in patterns}
    return patterns, skipped, nocase


def _re_flags(flags: set) -> int:
    return (re.VERBOSE if "x" in flags else 0) | (re.IGNORECASE if "i" in flags else 0)


def _escape_len(p: str, i: int) -> int:
    """Length of the escape starting at p[i] == backslash."""
    if i + 1 >= len(p):
        return 1
    c = p[i + 1]
    if c == "x":
        return 4
    if c == "u":
        return 6
    if c == "U":
        return 10
    if c == "N" and p[i + 2:i + 3] == "{":
        end = p.find("}", i)
        return end - i + 1 if end > 0 else 2
    if c.isdigit():
        j = i + 1
        while j < len(p) and j < i + 4 and p[j].isdigit():
            j += 1
        return j - i
    return 2


def _group_header_end(p: str, i: int) -> int:
    """Index just after the extension header of a group opening at p[i:i+2] == "(?"."""
    j = i + 2
    if p.startswith("P<", j) and not p.startswith("P<=", j):
        end = p.find(">", j)
        return end + 1 if end > 0 else j
    if p.startswith("<=", j) or p.startswith("<!", j):
        return j + 2
    if p[j:j + 1] in ("=", "!"):
        return j + 1
    if p.startswith("P=", j) or p.startswith("#", j) or p.startswith("(", j):
        end = p.find(")", j)
        return end + 1 if end > 0 else j
    if p[j:j + 1] in (":", ">"):
        return j + 1
    while j < len(p) and (p[j].isalpha() or p[j] == "-"):
        j += 1
    return j + 1 if p[j:j + 1] in (":", ")") else j


def _class_items(p: str, i: int) -> tuple[int, bool, list[tuple[int, int, bool]]] | None:
    """For a class opening at p[i] == "[": (index of its closing "]", negated, [(start, end, is_range)])."""
    j = i + 1
    negated = p[j:j + 1] == "^"
    if negated:
        j += 1
    items, first = [], True
    while j < len(p):
        c = p[j]
        if c == "]" and not first:
            return j, negated, items
        first = False
        k = j + (_escape_len(p, j) if c == "\\" else 1)
        if p[k:k + 1] == "-" and k + 1 < len(p) and p[k + 1] != "]":
            end = k + 1 + (_escape_len(p, k + 1) if p[k + 1] == "\\" else 1)
            items.append((j, end, True))
            j = end
        else:
            items.append((j, k, False))
            j = k
    return None


def _skip_verbose(p: str, i: int) -> int:
    """In verbose mode: the index after whitespace or a #-comment starting at p[i], or i when there is none."""
    if p[i] in " \t\n\r\f\v":
        return i + 1
    if p[i] == "#":
        end = p.find("\n", i)
        return len(p) if end < 0 else end + 1
    return i


def _protected(p: str, verbose: bool) -> list[tuple[int, int]]:
    """Spans no operator touches: lookaround bodies (outside MutRex's grammar), groups that a backreference (\\1,
    (?P=name), (?(1)...)) refers to, the backreference itself, and scoped-flag groups such as (?i:...) or (?x:...),
    where case and whitespace rules differ from the rest of the pattern."""
    spans, stack, groups, names, refs = [], [], {}, {}, []
    count, i = 0, 0
    while i < len(p):
        if verbose and _skip_verbose(p, i) != i:
            i = _skip_verbose(p, i)
            continue
        c = p[i]
        if c == "\\":
            n = _escape_len(p, i)
            if p[i + 1:i + 2].isdigit() and p[i + 1] != "0":
                refs.append(p[i + 1:i + n])
                spans.append((i, i + n))
            i += n
            continue
        if c == "[":
            parsed = _class_items(p, i)
            i = parsed[0] + 1 if parsed else i + 1
            continue
        if c == "(":
            head = p[i + 1:i + 5]
            if head.startswith("?P="):
                end = p.find(")", i)
                refs.append(p[i + 4:end])
                spans.append((i, end + 1))
                i = end + 1 if end > 0 else i + 1
                continue
            if head.startswith("?("):
                # A conditional (?(1)yes|no): its condition is not a capturing group, so skip past it.
                end = p.find(")", i + 2)
                refs.append(p[i + 3:end])
                stack.append((i, None))
                i = end + 1 if end > 0 else i + 1
                continue
            kind = "look" if head[:2] in ("?=", "?!") or head[:3] in ("?<=", "?<!") else None
            if _SCOPED_FLAGS.match(p, i + 1):
                kind = "look"  # (?i:...), (?-i:...), (?x:...): scoped flags are left alone as a whole
            if not head.startswith("?") or (head.startswith("?P<") and not head.startswith("?P<=")):
                count += 1
                kind = count
                if head.startswith("?P<"):
                    names[p[i + 4:p.find(">", i)]] = count
            stack.append((i, kind))
        elif c == ")" and stack:
            start, kind = stack.pop()
            if kind == "look":
                spans.append((start, i + 1))
            elif kind is not None:
                groups[kind] = (start, i + 1)
        i += 1
    for ref in refs:
        number = int(ref) if ref.isdigit() else names.get(ref)
        if number in groups:
            spans.append(groups[number])
    return spans


def _regex_mutants(p: str, verbose: bool = False, ignorecase: bool = False,
                   lines: set | None = None) -> list[tuple[str, str]]:
    """(mutated pattern, change text) for the MutRex subset, in source order, keeping only those that compile with
    the pattern's flags. Verbose mode skips whitespace and comments; with ignore-case there is no CA. lines, when
    given, holds the line offsets inside p where a mutant may go."""
    inline = {c for m in _INLINE_FLAGS.finditer(p) for c in m.group(1)}
    verbose, ignorecase = verbose or "x" in inline, ignorecase or "i" in inline
    flags = _re_flags({"x"} if verbose else set()) | _re_flags({"i"} if ignorecase else set())
    raw = []  # (operator, position, old length, new text)
    i = 0
    while i < len(p):
        if verbose and _skip_verbose(p, i) != i:
            i = _skip_verbose(p, i)
            continue
        c = p[i]
        if c == "\\":
            i += _escape_len(p, i)
            continue
        if c == "[":
            parsed = _class_items(p, i)
            if parsed is None:
                i += 1
                continue
            close, negated, items = parsed
            raw.append(("CCN", i, 2, "[") if negated else ("CCN", i, 1, "[^"))
            if len(items) > 1:
                raw += [("CCR", a, b - a, "") for a, b, _ in items]
            for a, b, is_range in items:
                lo, hi = p[a], p[b - 1]
                if is_range and b - a == 3 and ord(lo) < ord(hi):
                    raw.append(("RM", b - 1, 1, chr(ord(hi) - 1)))
                    raw.append(("RM", a, 1, chr(ord(lo) + 1)))
            i = close + 1
            continue
        if c == "(" and p[i + 1:i + 2] == "?":
            i = _group_header_end(p, i)
            continue
        if c in "*+":
            raw.append(("QC", i, 1, "+" if c == "*" else "*"))
        elif c == "?":
            raw.append(("QC", i, 1, ""))
        elif c == "{":
            m = re.match(r"\{(\d+)\}", p[i:])
            if m:
                raw.append(("QC", i, len(m.group(0)), "{%d}" % (int(m.group(1)) + 1)))
            m = m or re.match(r"\{\d*,\d*\}", p[i:])
            if m:
                i += len(m.group(0))
                continue
        elif c == ".":
            raw.append(("M2C", i, 1, "\\."))
        elif c.isalpha() and len(c.swapcase()) == 1 and c.swapcase() != c and not ignorecase:
            raw.append(("CA", i, 1, f"[{c}{c.swapcase()}]"))
        i += 1
    protected = _protected(p, verbose)
    out, seen = [], {p}
    for op, pos, length, new in raw:
        if any(a <= pos < b or a < pos + length <= b for a, b in protected):
            continue
        if lines is not None and p.count("\n", 0, pos) not in lines:
            continue
        mutated = p[:pos] + new + p[pos + length:]
        if mutated in seen or not _compiles(mutated, flags):
            continue
        seen.add(mutated)
        before = p[max(0, pos - 6):pos + length + 6]
        after = p[max(0, pos - 6):pos] + new + p[pos + length:pos + length + 6]
        out.append((mutated, f"regex {op}: {before!r} -> {after!r}"))
        if len(out) >= _MAX_REGEX_CANDIDATES:
            break
    return out


# ---- tree plumbing ----

def _walk(node, path):
    yield path, node
    for field, value in ast.iter_fields(node):
        if isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, ast.AST):
                    yield from _walk(item, path + ((field, i),))
        elif isinstance(value, ast.AST):
            yield from _walk(value, path + ((field, None),))


def _get(root, path):
    node = root
    for field, i in path:
        node = getattr(node, field) if i is None else getattr(node, field)[i]
    return node


def _set(root, path, new):
    parent = _get(root, path[:-1])
    field, i = path[-1]
    if i is None:
        setattr(parent, field, new)
    else:
        getattr(parent, field)[i] = new


def _apply(fn: ast.AST, site) -> tuple[ast.AST, str] | None:
    kind, path, extra = site
    tree = copy.deepcopy(fn)
    node = _get(tree, path)
    if kind == "cmp":
        old = node.ops[extra]
        node.ops[extra] = _CMP_SWAP[type(old)]()
        return tree, f"{type(old).__name__} -> {type(node.ops[extra]).__name__}"
    if kind == "bin":
        old = node.op
        node.op = _BIN_SWAP[type(old)]()
        return tree, f"{type(old).__name__} -> {type(node.op).__name__}"
    if kind == "bool":
        old = node.op
        node.op = _BOOL_SWAP[type(old)]()
        return tree, f"{type(old).__name__} -> {type(node.op).__name__}"
    if kind == "not":
        _set(tree, path, node.operand)
        return tree, "not removed"
    if kind == "const":
        node.value = node.value + 1
        return tree, f"{node.value - 1!r} -> {node.value!r}"
    if kind == "flipbool":
        node.value = not node.value
        return tree, f"{not node.value} -> {node.value}"
    if kind == "return":
        node.value = ast.Constant(value=None)
        return tree, "returns None"
    if kind in ("str", "regex"):
        node.value = extra[0]
        return tree, extra[1]
    if kind == "method":
        node.func.attr = extra[0]
        return tree, extra[1]
    return None


# ---- picking ----

def _spread(items: list, limit: int) -> list:
    """Every k-th item, so the picks cover all the changed lines instead of the first few spots of the first line."""
    step = max(1, len(items) // limit)
    return items[::step][:limit]


def _mix(groups: list[list], limit: int) -> list:
    """Round-robin over the groups, so no single kind of operator fills every slot."""
    queues = [list(g) for g in groups if g]
    out = []
    while queues and len(out) < limit:
        for q in queues:
            if len(out) < limit:
                out.append(q.pop(0))
        queues = [q for q in queues if q]
    return out


def _string_op(site) -> str:
    if site[0] == "method":
        return "method"
    new = site[2][0]
    return "empty" if new == "" else "wrap" if new.startswith("XX") and new.endswith("XX") else "case"


def _pick(sites: list[tuple], limit: int) -> list[tuple]:
    """Up to limit sites: the arithmetic/logic operators spread as before, mixed round-robin with the string operators
    (one queue per operation) and the regex operators (one queue per MutRex operator). With only arithmetic/logic
    spots the picks are the same as before string and regex operators existed."""
    def grouped(key, items):
        groups: dict = {}
        for s in items:
            groups.setdefault(key(s), []).append(s)
        return _mix([_spread(g, limit) for g in groups.values()], limit)

    core = _spread([s for s in sites if s[0] in _CORE], limit)
    strings = grouped(_string_op, [s for s in sites if s[0] in ("str", "method")])
    regexes = grouped(lambda s: s[2][1].split(":")[0], [s for s in sites if s[0] == "regex"])
    return _mix([core, strings, regexes], limit)


def generate(source: str, start_line: int, gate: list[list[int]], limit: int = MAX_MUTANTS,
             module_source: str | None = None) -> dict | None:
    """{"name", "identity": source, "mutants": [{"source", "change"}]} for the old function's text, or None when it
    cannot be mutated (decorated, nested, unparseable, or no mutable spot on a changed line).

    start_line is the function's first line in the old file; gate holds the changed statements as absolute lines.
    module_source, when given, is the old module's text: its `import re as x` / `from re import sub` aliases mark
    regex calls for certain. Without it a bare sub(...) counts only when its literal clearly is a regex."""
    if not gate:
        return None
    text = textwrap.dedent(source)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except SyntaxError:
        return None
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.decorator_list:
        return None
    # tree-sitter's text starts at the def line, so line 1 of the parsed text is start_line in the file.
    sites = _sites(fn, start_line, gate, text, re_aliases(module_source))
    if not sites:
        return None
    identity = ast.unparse(fn)
    out = []
    for site in _pick(sites, limit):
        made = _apply(fn, site)
        if made is None:
            continue
        mutated, change = made
        try:
            source_out = ast.unparse(ast.fix_missing_locations(mutated))
        except Exception:  # noqa: BLE001 - a mutant we cannot print is simply left out
            continue
        if source_out != identity:  # a mutant identical to the original is not a bug
            out.append({"source": source_out, "change": change})
    if not out:
        return None
    return {"name": fn.name, "identity": identity, "mutants": out}
