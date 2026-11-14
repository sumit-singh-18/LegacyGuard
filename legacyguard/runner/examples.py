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

from runner import lg_codec

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

def _literal(node, max_text: int = MAX_TEXT):
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _decimal_call(node)
    return value if _simple(value, max_text=max_text) else _NO


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


def _simple(value, depth: int = 0, max_text: int = MAX_TEXT) -> bool:
    from decimal import Decimal
    if value is None or isinstance(value, (bool, int, float, Decimal)):
        return True
    if isinstance(value, (str, bytes)):
        return len(value) <= max_text
    if depth < 2 and isinstance(value, (list, tuple)):
        return len(value) <= 8 and all(_simple(v, depth + 1, max_text) for v in value)
    if depth < 2 and isinstance(value, dict):
        return len(value) <= 8 and all(isinstance(k, str) and _simple(v, depth + 1, max_text) for k, v in value.items())
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


# ---- arguments written through variables, loops, parametrize and fixtures (roadmap 3c, Sprint 5 B) ----
#
# Tests often name a value before passing it (`inp = '"a\\nb"'; js_to_json(inp)`), loop over cases, or build objects
# (`match(Command('php -s localhost:8000', ''))`). A small walk over each test function's straight-line code follows
# those names. It is an approximation: branches are taken in order and a loop variable is unbound after its loop.

MAX_CALL_TEXT = 2000     # a whole-call argument may be longer than a pool value: a JSON document, an HTML page
MAX_ENVS = 24            # combinations of parametrize rows and loop values followed per test function
MAX_BINDINGS = 40
# Objects from these modules are test machinery, not values the code under test is meant to take.
CONSTRUCT_SKIP = ("unittest", "mock", "pytest", "_pytest", "builtins", "hypothesis")


class _Context:
    """What a file's imports and module-level code say about names: classes to build, module constants, fixtures."""

    def __init__(self, tree: ast.AST, rel: str):
        self.modules: dict[str, str] = {}
        self.names: dict[str, str] = {}
        pkg = rel[:-3].split("/")[:-1] if rel.endswith(".py") else []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.modules[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        self.modules[top] = top
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level - 1 > len(pkg):
                        continue
                    mod = ".".join(pkg[:len(pkg) - (node.level - 1)] + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                if not mod:
                    continue
                for a in node.names:
                    if a.name != "*":
                        self.names[a.asname or a.name] = f"{mod}:{a.name}"
        self.module_env: dict = {}
        self.fixtures: dict = {}

    def class_ref(self, func) -> str | None:
        """'module:Class' for a call target that names an imported class (capitalized, by convention), else None."""
        chain: list[str] = []
        while isinstance(func, ast.Attribute):
            chain.insert(0, func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        if not chain:
            ref = self.names.get(func.id)
        else:
            if func.id in self.modules:
                dotted = ".".join([self.modules[func.id], *chain])
            elif func.id in self.names:
                dotted = ".".join([self.names[func.id].replace(":", "."), *chain])
            else:
                return None
            module, _, qual = dotted.rpartition(".")
            ref = f"{module}:{qual}" if module else None
        if not ref:
            return None
        module, qual = ref.split(":", 1)
        if not qual[:1].isupper() or module.split(".")[0] in CONSTRUCT_SKIP or qual == "Decimal":
            return None
        return ref


def _callee(func) -> str | None:
    return getattr(func, "id", None) or getattr(func, "attr", None)


def _resolve(node, env: dict, ctx: _Context, depth: int = 0):
    """The value an argument expression has, given the names bound so far; _NO when it cannot be known."""
    value = _literal(node, MAX_CALL_TEXT)
    if value is not _NO or depth > 4:
        return value
    if isinstance(node, ast.Name):
        return env.get(node.id, _NO)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _resolve(node.left, env, ctx, depth + 1), _resolve(node.right, env, ctx, depth + 1)
        if isinstance(left, str) and isinstance(right, str) and len(left) + len(right) <= MAX_CALL_TEXT:
            return left + right
        return _NO
    if isinstance(node, (ast.List, ast.Tuple)):
        if len(node.elts) > 16 or any(isinstance(e, ast.Starred) for e in node.elts):
            return _NO
        items = [_resolve(e, env, ctx, depth + 1) for e in node.elts]
        if any(i is _NO for i in items):
            return _NO
        return items if isinstance(node, ast.List) else tuple(items)
    if isinstance(node, ast.Call):
        ref = ctx.class_ref(node.func)
        if ref is None or any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            return _NO
        args = [_resolve(a, env, ctx, depth + 1) for a in node.args]
        kwargs = {k.arg: _resolve(k.value, env, ctx, depth + 1) for k in node.keywords}
        if any(a is _NO for a in args) or any(v is _NO for v in kwargs.values()):
            return _NO
        return lg_codec.Construct(ref, args, kwargs)
    return _NO


def _target_names(target) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in _target_names(e)]
    return []


def _bind_value(target, value, env: dict) -> dict:
    if isinstance(target, ast.Name):
        if value is _NO:
            env.pop(target.id, None)
        else:
            env[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)):
        if isinstance(value, (list, tuple)) and len(value) == len(target.elts):
            for t, v in zip(target.elts, value):
                _bind_value(t, v, env)
        else:
            for n in _target_names(target):
                env.pop(n, None)
    return env


def _bind(target, value_node, env: dict, ctx: _Context) -> dict:
    env = dict(env)
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value_node, (ast.Tuple, ast.List)) and len(target.elts) == len(value_node.elts):
        values = [_resolve(v, env, ctx) for v in value_node.elts]
        for t, v in zip(target.elts, values):
            _bind_value(t, v, env)
        return env
    return _bind_value(target, _resolve(value_node, env, ctx), env)


_BODIES = ("body", "orelse", "finalbody", "handlers", "cases", "decorator_list")


def _own_exprs(stmt) -> list:
    """The expressions a statement evaluates itself; the statements in its bodies are visited on their own."""
    out = []
    for name, value in ast.iter_fields(stmt):
        if name in _BODIES:
            continue
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, ast.AST):
                out.append(v)
    return out


def _run_block(stmts: list, envs: list[dict], ctx: _Context, on_call) -> list[dict]:
    for st in stmts:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for expr in _own_exprs(st):
            for node in ast.walk(expr):
                if isinstance(node, ast.Call):
                    on_call(node, envs)
        if isinstance(st, ast.Assign):
            for target in st.targets:
                envs = [_bind(target, st.value, env, ctx) for env in envs]
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            envs = [_bind(st.target, st.value, env, ctx) for env in envs]
        elif isinstance(st, ast.AugAssign) and isinstance(st.target, ast.Name):
            node = ast.BinOp(left=ast.Name(id=st.target.id, ctx=ast.Load()), op=st.op, right=st.value)
            envs = [_bind(st.target, node, env, ctx) for env in envs]
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            inner = []
            for env in envs:
                seq = _resolve(st.iter, env, ctx)
                if isinstance(seq, (list, tuple)) and seq:
                    inner += [_bind_value(st.target, item, dict(env)) for item in seq]
                else:
                    inner.append(_bind_value(st.target, _NO, dict(env)))
            _run_block(st.body, inner[:MAX_ENVS], ctx, on_call)
            _run_block(st.orelse, envs, ctx, on_call)
            envs = [_bind_value(st.target, _NO, dict(e)) for e in envs]
        else:
            for field in ("body", "orelse", "finalbody"):
                block = getattr(st, field, None)
                if isinstance(block, list):
                    envs = _run_block(block, envs, ctx, on_call)
            for h in getattr(st, "handlers", None) or []:
                envs = _run_block(h.body, envs, ctx, on_call)
            for case in getattr(st, "cases", None) or []:
                _run_block(case.body, envs, ctx, on_call)
    return envs


def _is_fixture(fn) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _callee(target) == "fixture":
            return True
    return False


def _param_rows(fn, ctx: _Context) -> list[dict]:
    """One dict per case of the test's @pytest.mark.parametrize decorators (stacked ones are combined)."""
    rows: list[dict] = [{}]
    for dec in fn.decorator_list:
        if not isinstance(dec, ast.Call) or _callee(dec.func) != "parametrize" or len(dec.args) < 2:
            continue
        names = _resolve(dec.args[0], ctx.module_env, ctx)
        if isinstance(names, str):
            names = [n.strip() for n in names.split(",") if n.strip()]
        if not isinstance(names, (list, tuple)) or not names or not all(isinstance(n, str) for n in names):
            continue
        values = dec.args[1]
        if isinstance(values, ast.Name):
            resolved = ctx.module_env.get(values.id)
            if isinstance(resolved, (list, tuple)):
                cases = [{n: v for n, v in zip(names, (r if len(names) > 1 and isinstance(r, (list, tuple)) else [r]))} for r in resolved]
                rows = [{**r, **c} for r in rows for c in cases][:MAX_ENVS]
            continue
        if not isinstance(values, (ast.List, ast.Tuple)):
            continue
        cases = []
        for elt in values.elts:
            if isinstance(elt, ast.Call) and _callee(elt.func) == "param":
                parts = list(elt.args)
            elif len(names) == 1:
                parts = [elt]
            elif isinstance(elt, (ast.Tuple, ast.List)):
                parts = list(elt.elts)
            else:
                whole = _resolve(elt, ctx.module_env, ctx)
                parts = list(whole) if isinstance(whole, (list, tuple)) else []
            case = {}
            for n, part in zip(names, parts):
                v = _resolve(part, ctx.module_env, ctx) if isinstance(part, ast.AST) else part
                if v is not _NO:
                    case[n] = v
            cases.append(case)
        if cases:
            rows = [{**r, **c} for r in rows for c in cases][:MAX_ENVS]
    return rows


class _Scan:
    """Every call to one of `names` in a file, resolved: (callee, positional values, keyword values). Also the objects
    (Construct recipes) bound to a name anywhere in the file's tests, for parameters of that name elsewhere."""

    def __init__(self, tree: ast.AST, rel: str, names: set[str]):
        self.calls: list[tuple[str, list, dict]] = []
        self.bindings: list[tuple[str, object]] = []
        self._seen: set[str] = set()
        self.tree = tree
        ctx = _Context(tree, rel)
        self.ctx = ctx
        body = getattr(tree, "body", [])
        ctx.module_env = (_run_block(body, [{}], ctx, self._on_call(names)) or [{}])[0]
        self._note_bindings(ctx.module_env)
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture(node):
                stmts = [s for s in node.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
                if len(stmts) == 1 and isinstance(stmts[0], ast.Return) and stmts[0].value is not None:
                    value = _resolve(stmts[0].value, ctx.module_env, ctx)
                    if value is not _NO:
                        ctx.fixtures[node.name] = value
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]
            base = {k: v for k, v in ctx.module_env.items() if k not in args}
            base.update({a: ctx.fixtures[a] for a in args if a in ctx.fixtures})
            envs = [{**base, **row} for row in _param_rows(node, ctx)]
            for env in envs:
                self._note_bindings(env)
            _run_block(node.body, envs, ctx, self._on_call(names))

    def _note_bindings(self, env: dict) -> None:
        for k, v in env.items():
            if isinstance(v, lg_codec.Construct) and len(self.bindings) < MAX_BINDINGS:
                key = f"{k}={v!r}"
                if key not in self._seen:
                    self._seen.add(key)
                    self.bindings.append((k, v))

    def _on_call(self, names: set[str]):
        def on_call(node: ast.Call, envs: list[dict]) -> None:
            callee = _callee(node.func)
            if callee not in names:
                return
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                return
            for env in envs:
                self._note_bindings(env)
                pos = [_resolve(a, env, self.ctx) for a in node.args]
                kw = {k.arg: _resolve(k.value, env, self.ctx) for k in node.keywords}
                self.calls.append((callee, pos, kw))
        return on_call


def _as_call(pos: list, kw: dict, params: list[str]) -> list | None:
    """The runner's argument list for a resolved call, or None when an argument is unknown or does not fit."""
    if len(pos) > len(params) or any(v is _NO for v in pos) or any(v is _NO for v in kw.values()) or any(k not in params for k in kw):
        return None
    out = [(params[i], "pos", v) for i, v in enumerate(pos)] + [(k, "kw", v) for k, v in kw.items()]
    return out or None


def _scan_file(root: str, rel: str, names: set[str]) -> _Scan | None:
    path = Path(root) / rel
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        tree = _parse(path.read_text(errors="replace"))
    except OSError:
        return None
    if tree is None:
        return None
    try:
        return _Scan(tree, rel, names)
    except RecursionError:
        return None


def _file_calls(scan: _Scan, name: str, params: list[str]) -> list:
    out = []
    for callee, pos, kw in scan.calls:
        if callee == name:
            call = _as_call(pos, kw, params)
            if call:
                out.append(call)
    return out


MAX_OBJECTS = 6


_TEST_FILES: dict[str, tuple[float, list[str]]] = {}
TEST_FILES_TTL_S = 60.0  # one check; a hook process is short-lived, so this only spans the functions of one edit


def _test_files(root: str, deadline: float | None = None) -> list[str]:
    """The project's test files (paths with "test" in them), sorted, from one walk of the tree per check. Virtualenvs,
    node_modules, build output and the guard's own state are pruned before descending, not filtered afterwards."""
    import os
    import time
    cached = _TEST_FILES.get(root)
    if cached is not None and time.monotonic() - cached[0] < TEST_FILES_TTL_S:
        return cached[1]
    found: list[str] = []
    seen = 0
    complete = True
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for n in sorted(names):
            if not n.endswith(".py"):
                continue
            seen += 1
            rel = os.path.relpath(os.path.join(folder, n), root).replace(os.sep, "/")
            if "test" in rel.lower():
                found.append(rel)
        if seen >= MAX_SCAN * 10 or (deadline is not None and time.monotonic() > deadline):
            complete = False
            break
    found.sort()
    if complete:
        _TEST_FILES[root] = (time.monotonic(), found)
    return found


def _test_files_with(root: str, terms: dict[str, list[str]], deadline: float | None = None) -> dict[str, list[str]]:
    """For each key, the test files that mention it and one of its terms. Each file is read once for all keys."""
    import time
    hits: dict[str, list[str]] = {k: [] for k in terms}
    for scanned, rel in enumerate(_test_files(root, deadline)):
        if scanned >= MAX_SCAN or (deadline is not None and time.monotonic() > deadline):
            break
        if all(len(h) >= MAX_FILES * 2 for h in hits.values()):
            break
        path = Path(root) / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for must, any_of in terms.items():
            if len(hits[must]) < MAX_FILES * 2 and must in text and any(w and w in text for w in any_of):
                hits[must].append(rel)
    return hits


def param_objects(root: str, own_file: str, name: str, params: list[dict], files: list[str], deadline: float | None = None) -> dict[str, list]:
    """Objects the project's tests build, as candidates for parameters nothing else can fill (roadmap 3c, B).

    A parameter without a hint or default, or with a class hint, gets the objects that tests pass in the same position
    to a function of the same name (every `match(command)` rule in a project), bind to a name equal to the parameter's,
    or build from the hinted class. They are guesses for this function, so the inputs made from them are marked weak."""
    plain = [p for p in params if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]
    wanted: dict[str, tuple[int, str | None]] = {}
    for i, p in enumerate(plain):
        ann = (p.get("annotation") or "").strip("\"' ")
        cls = ann.rsplit(".", 1)[-1] if ann and ann.replace(".", "").isidentifier() and ann.rsplit(".", 1)[-1][:1].isupper() else None
        if (not ann and p.get("default") is None) or cls:
            wanted[p["name"]] = (i, cls)
    if not wanted:
        return {}
    out: dict[str, list] = {k: [] for k in wanted}
    seen: set[str] = set()
    import time
    per_param = _test_files_with(root, {p: [name, cls or name] for p, (_, cls) in wanted.items()}, deadline)
    candidates = list(dict.fromkeys([*files, *[f for p in wanted for f in per_param[p]]]))
    for rel in candidates:
        if deadline is not None and time.monotonic() > deadline:
            break  # what was found so far is kept; the check must not wait on a large repo
        if rel == own_file:
            continue
        scan = _scan_file(root, rel, {name})
        if scan is None:
            continue
        found: list[tuple[str, object]] = []
        for _, pos, kw in scan.calls:
            for p, (i, _) in wanted.items():
                v = pos[i] if i < len(pos) else kw.get(p, _NO)
                found.append((p, v))
        for bound, v in scan.bindings:
            for p, (_, cls) in wanted.items():
                if bound == p or (cls and isinstance(v, lg_codec.Construct) and v.cls.rsplit(":", 1)[-1].rsplit(".", 1)[-1] == cls):
                    found.append((p, v))
        for p, v in found:
            if not isinstance(v, lg_codec.Construct) or len(out[p]) >= MAX_OBJECTS:
                continue
            cls = wanted[p][1]
            if cls and v.cls.rsplit(":", 1)[-1].rsplit(".", 1)[-1] != cls:
                continue
            key = f"{p}={v.cls}{v!r}"
            if key not in seen:
                seen.add(key)
                out[p].append(v)
    return {k: v for k, v in out.items() if v}


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
        scan = _scan_file(root, rel, {name})
        if scan is None:
            continue
        tree = scan.tree
        calls += _file_calls(scan, name, params)
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
        scan = _scan_file(root, rel, {class_name})
        if scan is not None:
            calls += _file_calls(scan, class_name, params)
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


# ---- values from the edit itself (roadmap 3c, Sprint 5 B; our heuristic, not from the papers) ----
#
# The constants an edit touched say where the two versions part: a literal it added or removed, a number it moved,
# a regex it rewrote. These give inputs aimed at the change, built on examples that already reach it.

RE_FUNCS = {"match": 2, "search": 2, "fullmatch": 2, "findall": 2, "finditer": 2, "split": 3, "sub": 4, "subn": 4, "compile": 1}
MAX_EDIT_LITERALS = 16
MAX_FRAGMENTS = 8
REGEX_META = set("[]()?*+^$\\{}|.")


def _changed_spans(text: str, changed: set[int]) -> list[tuple[int, int]]:
    spans = _statement_spans(text) or []
    out = []
    for n in sorted(changed):
        holding = [s for s in spans if s[0] <= n <= s[1] and not s[2]]
        if holding:
            start, end, _ = min(holding, key=lambda s: s[1] - s[0])
            if (start, end) not in out:
                out.append((start, end))
    return out


def _in_spans(node, spans) -> bool:
    return any(start <= node.lineno <= end for start, end in spans)


def _flags(node) -> int | None:
    """The value of a flags argument written as re.I, re.IGNORECASE | re.M, or a number."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "re":
        flag = getattr(re, node.attr, None)
        return int(flag) if isinstance(flag, re.RegexFlag) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        a, b = _flags(node.left), _flags(node.right)
        return a | b if a is not None and b is not None else None
    return None


def _edit_side(text: str, changed: set[int]) -> tuple[list, list[dict]]:
    """(literals, regex calls) in the statements of one version that the edit changed, in source order."""
    tree = _dedent_parse(text)
    if tree is None:
        return [], []
    import textwrap
    spans = _changed_spans(textwrap.dedent(text), changed)
    literals, regexes = [], []
    for node in ast.walk(tree):
        if not hasattr(node, "lineno") or not _in_spans(node, spans):
            continue
        if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, (str, int, float, bool))):
            literals.append((node.lineno, node.col_offset, node.value))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re" and node.func.attr in RE_FUNCS and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            index = RE_FUNCS[node.func.attr]
            flag_node = node.args[index] if len(node.args) > index else next((k.value for k in node.keywords if k.arg == "flags"), None)
            flags = _flags(flag_node) if flag_node is not None else 0
            if flags is not None:
                regexes.append((node.lineno, node.col_offset, {"fn": node.func.attr, "pattern": node.args[0].value, "flags": flags}))
    literals.sort(key=lambda t: (t[0], t[1]))
    regexes.sort(key=lambda t: (t[0], t[1]))
    return [v for _, _, v in literals], [r for _, _, r in regexes]


def _fragments(a: str, b: str) -> list[str]:
    """The pieces of text one string has and the other lacks: '&' for '&([^;]+;)' against '&([^&;]+;)'."""
    out = []
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        for piece in (a[i1:i2], b[j1:j2]):
            if not piece or len(piece) > 12:
                continue
            out.append(piece)
            plain = "".join(ch for ch in piece if ch not in REGEX_META)
            if plain and plain != piece:
                out.append(plain)
    return out


def edit_info(old_text: str, new_text: str) -> dict:
    """What an edit changed, from the two versions of one function's text.

    {"literals": str/int/float/bool/None constants in the changed statements of either version,
     "fragments": text one changed string literal has and its counterpart lacks,
     "numbers": [old, new] pairs of numeric constants that changed,
     "regexes": [{"fn", "old", "new", "flags"}] re calls whose pattern literal changed}"""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    changed_old: set[int] = set()
    changed_new: set[int] = set()
    matcher = difflib.SequenceMatcher(None, [l.rstrip() for l in old_lines], [l.rstrip() for l in new_lines], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            changed_old.update(range(i1 + 1, i2 + 1))
            changed_new.update(range(j1 + 1, j2 + 1))
    old_lits, old_res = _edit_side(old_text, changed_old)
    new_lits, new_res = _edit_side(new_text, changed_new)
    literals = _unique(old_lits + new_lits)
    literals = [v for v in literals if not isinstance(v, str) or len(v) <= MAX_CALL_TEXT][:MAX_EDIT_LITERALS]
    fragments: list[str] = []
    numbers: list[list] = []
    old_strs = [v for v in old_lits if isinstance(v, str)]
    new_strs = [v for v in new_lits if isinstance(v, str)]
    gone = [v for v in old_strs if v not in new_strs]
    added = [v for v in new_strs if v not in old_strs]
    for a, b in zip(gone, added):
        fragments += _fragments(a, b)
    def numeric(values):
        return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    old_nums, new_nums = numeric(old_lits), numeric(new_lits)
    for a, b in zip([v for v in old_nums if v not in new_nums], [v for v in new_nums if v not in old_nums]):
        numbers.append([a, b])
    regexes = []
    for a, b in zip([r for r in old_res if r not in new_res], [r for r in new_res if r not in old_res]):
        if a["pattern"] != b["pattern"]:
            # The semantics come from the new call (the same as the old one unless the edit changed both).
            regexes.append({"fn": b["fn"], "old": a["pattern"], "new": b["pattern"], "flags_old": a["flags"], "flags_new": b["flags"]})
    return {"literals": literals, "fragments": _unique(fragments)[:MAX_FRAGMENTS], "numbers": numbers[:6], "regexes": regexes[:4]}
