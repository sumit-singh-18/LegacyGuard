"""What the repo itself says about a changed function: which statements changed, and example values to try (FR-35, FR-36).

Two uses, both read-only and without a model:

* gates: the statements an edit changed. A Verified pass needs every one of them run by some input (D-043).
* a pool of example values and whole example calls, taken from the function's own text (docstring included) and from
  the files that call it, tests first. Guessed inputs for an untyped parameter rarely reach real code paths; the
  project's own examples do.
"""
from __future__ import annotations

import ast
import difflib
import re
from pathlib import Path

MAX_FILES = 8
MAX_FILE_BYTES = 400_000
MAX_POOL = 60
MAX_CALLS = 40
MAX_TEXT = 60
QUOTED = re.compile(r"""(['"`])([^'"`\n]{1,40})\1""")
NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
# 00:20:00, 2024-01-31, 10.0.0.1: values with parts, often written in prose without quotes
STRUCTURED = re.compile(r"(?<![\w:/-])\d+(?:[:/-]\d+)+(?![\w:/-])|(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])")
SEPARATORS = (":", ",", "-", "/", ".", ";", "|")


# ---- gates ----

def _statement_spans(source: str) -> list[tuple[int, int, bool]] | None:
    """(first line, last line, inert) for every statement. A compound statement counts for its header lines only."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        end = node.end_lineno or node.lineno
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and hasattr(body[0], "lineno"):
            end = max(node.lineno, body[0].lineno - 1)
        # A docstring, `global` and `nonlocal` never produce a traced line, so they can never count as reached.
        inert = (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)) \
            or isinstance(node, (ast.Global, ast.Nonlocal))
        spans.append((node.lineno, end, inert))
    return spans


def _gate(source: str, lines: list[int], own_header: int) -> list[list[int]]:
    """Statement ranges for changed lines. Comments, blank lines, docstrings and the function's own def line are left out:
    calling the function never runs them, so they cannot be what a pass depends on."""
    text = source.splitlines()
    live = [n for n in lines if 0 < n <= len(text) and text[n - 1].strip() and not text[n - 1].lstrip().startswith("#")]
    spans = _statement_spans(source)
    if spans is None:
        return [[n, n] for n in live if n != own_header]
    out: list[list[int]] = []
    for n in live:
        holding = [s for s in spans if s[0] <= n <= s[1]]
        if not holding:
            continue
        start, end, inert = min(holding, key=lambda s: s[1] - s[0])
        if inert or start == own_header:
            continue
        if [start, end] not in out:
            out.append([start, end])
    return out


def gates(before_src: str, after_src: str, before_sym, after_sym) -> tuple[list[list[int]], list[list[int]]]:
    """(old gate, new gate): absolute line ranges of the statements this edit changed, in each version of the file."""
    old = before_sym.node_text.splitlines()
    new = after_sym.node_text.splitlines()
    old_lines, new_lines = [], []
    matcher = difflib.SequenceMatcher(None, [l.rstrip() for l in old], [l.rstrip() for l in new], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_lines += [before_sym.line_start + i for i in range(i1, i2)]
        new_lines += [after_sym.line_start + j for j in range(j1, j2)]
    return _gate(before_src, old_lines, before_sym.line_start), _gate(after_src, new_lines, after_sym.line_start)


def reached(gate: list[list[int]], executed: list[int] | None) -> bool | None:
    """True if an executed line falls inside a changed statement. None when there is nothing to judge by."""
    if not gate or executed is None:
        return None
    done = set(executed)
    return any(any(n in done for n in range(start, end + 1)) for start, end in gate)


def coverage(gate: list[list[int]], executed: list[int] | None) -> tuple[list[list[int]], list[list[int]]] | None:
    """(reached statements, statements never run) of a gate. None when there is nothing to judge by."""
    if not gate or executed is None:
        return None
    done = set(executed)
    hit, missed = [], []
    for start, end in gate:
        (hit if any(n in done for n in range(start, end + 1)) else missed).append([start, end])
    return hit, missed


def show_ranges(ranges: list[list[int]], limit: int = 6) -> str:
    """'45-46, 50' for statement ranges."""
    parts = [f"{s}-{e}" if e != s else f"{s}" for s, e in ranges[:limit]]
    return ", ".join(parts) + (f" and {len(ranges) - limit} more" if len(ranges) > limit else "")


# ---- example values ----

def _literal(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _decimal_call(node)
    return value if _simple(value) else _NO


_NO = object()


def _decimal_call(node):
    """Decimal("1.50") written in a test is as good as a literal."""
    if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "Decimal" and len(node.args) == 1
            and not node.keywords and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, (str, int))):
        from decimal import Decimal, InvalidOperation
        try:
            return Decimal(str(node.args[0].value))
        except InvalidOperation:
            return _NO
    return _NO


def _simple(value, depth: int = 0) -> bool:
    from decimal import Decimal
    if value is None or isinstance(value, (bool, int, float, Decimal)):
        return True
    if isinstance(value, (str, bytes)):
        return len(value) <= MAX_TEXT
    if depth < 2 and isinstance(value, (list, tuple)):
        return len(value) <= 8 and all(_simple(v, depth + 1) for v in value)
    if depth < 2 and isinstance(value, dict):
        return len(value) <= 8 and all(isinstance(k, str) and _simple(v, depth + 1) for k, v in value.items())
    return False


def _from_text(text: str) -> list:
    """Quoted tokens and numbers in prose, such as a docstring's 'human_to_bytes("10M")' or '"1-5,8,10-12" => [...]'."""
    out: list = [m.group(2) for m in QUOTED.finditer(text)]
    out += [m.group(0) for m in STRUCTURED.finditer(text)]
    for m in NUMBER.finditer(text):
        token = m.group(0)
        out.append(float(token) if "." in token else int(token))
    return out


def _walk_literals(tree: ast.AST) -> list:
    out: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)) and not isinstance(node.value, bool):
            if isinstance(node.value, str):
                if len(node.value) <= MAX_TEXT:
                    out.append(node.value)
                else:
                    out += _from_text(node.value)
            else:
                out.append(node.value)
        elif isinstance(node, ast.Call):
            value = _decimal_call(node)
            if value is not _NO:
                out.append(value)
    return out


def _parse(text: str):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _dedent_parse(text: str):
    import textwrap
    return _parse(textwrap.dedent(text))


def _calls_in(tree: ast.AST, name: str, params: list[str]) -> list[list[dict]]:
    """Whole calls to `name` whose arguments are all literals, as the runner's argument lists."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "id", None) != name and getattr(func, "attr", None) != name:
            continue
        args = []
        for i, a in enumerate(node.args):
            value = _literal(a) if not isinstance(a, ast.Starred) else _NO
            if value is _NO or i >= len(params):
                args = None
                break
            args.append((params[i], "pos", value))
        if args is None:
            continue
        for kw in node.keywords:
            value = _literal(kw.value) if kw.arg else _NO
            if value is _NO or kw.arg not in params:
                args = None
                break
            args.append((kw.arg, "kw", value))
        if args:
            out.append(args)
    return out


def _mentions(tree: ast.AST, name: str) -> list[ast.AST]:
    """Test functions (and module level) that mention the name: their literals are the cases a parametrized test feeds it."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any((getattr(n, "id", None) == name or getattr(n, "attr", None) == name) for n in ast.walk(node)):
                hits.append(node)
    return hits


def calling_files(conn, name: str, own_file: str) -> list[str]:
    """Files the index says call this name, tests first. Name matches can over-match; that only adds unhelpful values."""
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT file, COUNT(*) AS n FROM calls WHERE called_name = ? GROUP BY file", (name,)).fetchall()
    except Exception:  # noqa: BLE001 - examples are a bonus; a missing or old index must not stop the check
        return []
    files = [(r[0], r[1]) for r in rows if r[0] != own_file]
    files.sort(key=lambda fr: (0 if "test" in fr[0].lower() else 1, -fr[1], fr[0]))
    return [f for f, _ in files[:MAX_FILES]]


def collect(root: str, own_text: str, name: str, params: list[str], files: list[str]) -> dict:
    """{"pool": example values, "calls": whole example calls}. Own text first, then callers."""
    pool: list = []
    calls: list = []
    own = _dedent_parse(own_text)
    if own is not None:
        pool += _walk_literals(own)
        doc = next((ast.get_docstring(n) for n in ast.walk(own) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if doc:
            pool += _from_text(doc)
            for m in re.finditer(rf"\b{re.escape(name)}\([^()\n]*\)", doc):
                doc_tree = _parse(m.group(0))
                if doc_tree is not None:
                    calls += _calls_in(doc_tree, name, params)
    else:
        pool += _from_text(own_text)
    # Comments often carry the example that explains a subtle line ("for example 2.675 == 2.6749999999999998").
    for line in own_text.splitlines():
        if "#" in line:
            pool += _from_text(line.split("#", 1)[1])
    for rel in files:
        path = Path(root) / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            tree = _parse(path.read_text(errors="replace"))
        except OSError:
            continue
        if tree is None:
            continue
        calls += _calls_in(tree, name, params)
        for fn in _mentions(tree, name):
            pool += _walk_literals(fn)
            for dec in fn.decorator_list:
                pool += _walk_literals(dec)
    base = _unique(pool)[:MAX_POOL]
    # Each value is followed by its near neighbours, so a short prefix of the pool still holds both kinds.
    mixed: list = []
    for v in base:
        mixed += [v, *_variants([v])]
    return {"pool": _unique(mixed)[:MAX_POOL * 4], "calls": _unique_calls(calls)[:MAX_CALLS]}


FIRST_NUMBER = re.compile(r"\d+(?:\.\d+)?")


MAX_SELF_CALLS = 5
MAX_SCAN = 400
_SKIP_DIRS = {".git", ".legacyguard", ".venv", "venv", "env", "node_modules", "__pycache__", "site-packages", "build", "dist"}


def files_naming(root: str, name: str) -> list[str]:
    """Without an index: up to MAX_FILES Python files that mention the name, tests first. Bounded, so it stays fast."""
    hits: list[str] = []
    scanned = 0
    for path in sorted(Path(root).rglob("*.py")):
        if scanned >= MAX_SCAN or any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        scanned += 1
        try:
            if path.stat().st_size <= MAX_FILE_BYTES and name in path.read_text(errors="replace"):
                hits.append(path.relative_to(root).as_posix())
        except OSError:
            continue
    hits.sort(key=lambda r: (0 if "test" in r.lower() else 1, r))
    return hits[:MAX_FILES]


def constructor_calls(root: str, own_file: str, class_name: str, params: list[str], files: list[str]) -> list:
    """Calls like Money(Decimal("1.00"), "USD") written in the repo, tests first, as argument lists (roadmap B3).

    They are how the project itself builds the object, so they are the most likely instance to run a method on."""
    calls: list = []
    if not files:
        files = files_naming(root, class_name)
    for rel in [*files, own_file]:
        path = Path(root) / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            tree = _parse(path.read_text(errors="replace"))
        except OSError:
            continue
        if tree is not None:
            calls += _calls_in(tree, class_name, params)
    return _unique_calls(calls)[:MAX_SELF_CALLS]


def _variants(values: list) -> list:
    """Near neighbours of the example values. Rounding and boundary bugs show only between the round numbers
    people write in examples: '10M' and '2K' never tell int(x) from round(x), but '10.5M' does."""
    out: list = []
    for v in values:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out += [v + 1, v - 1, v + 0.5]
        elif isinstance(v, float):
            out += [v + 0.005, round(v, 2) + 0.005]
        elif isinstance(v, str) and len(v) <= 20:
            # One part more and one part fewer. Code that splits a value usually handles the documented shape
            # and decides something about the others ("1:2:3:4" where "h:m:s" is expected).
            for sep in SEPARATORS:
                parts = v.split(sep)
                if 2 <= len(parts) <= 5 and all(parts):
                    out.append(sep.join(parts + [parts[-1]]))
                    if len(parts) > 2:
                        out.append(sep.join(parts[:-1]))
                    # Zero is the usual default, so a part that is zero cannot show whether it was read:
                    # '00:20:00:00' gives 0 seconds whether the third part is used or ignored.
                    nonzero = [q if q.strip("0") else "7" for q in parts]
                    if nonzero != parts:
                        out += [sep.join(nonzero), sep.join(nonzero + [nonzero[-1]])]
                    break
            m = FIRST_NUMBER.search(v)
            if m and "." not in m.group(0):
                # Three fractions, because which one lands past the half depends on the multiplier that follows
                # (x.7 KB and x.3 MB both do; x.5 of either is a whole number of bytes).
                out += [v[:m.start()] + m.group(0) + frac + v[m.end():] for frac in (".7", ".3", ".5")]
    return out


def _unique(values: list) -> list:
    seen, out = set(), []
    for v in values:
        key = (type(v).__name__, repr(v))
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _unique_calls(calls: list) -> list:
    seen, out = set(), []
    for c in calls:
        key = repr(c)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
