"""Behavioral diff (FR-14): the same inputs through old and new code, in separate processes, compared by execution."""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from guardlib import config as cfgmod
from guardlib import symbol_diff, symbols
from indexer.coverage_map import clean_env, load_harvest
from runner import examples
from runner import inputs as input_lib
from runner import lg_codec
from runner import mutants
from runner import os_sandbox
from runner import warm

WORKER = Path(__file__).resolve().parent / "lg_worker.py"
MAX_FUNCTIONS = 12
MAX_SHOWN = 3
SELF_VARIANTS = 4  # matches lg_worker.SELF_VARIANTS
DONE, DIFFERENT, BROKEN, UNTESTED = "verified", "different", "broken", "untested"
SLOWDOWN = "slowdown"           # Task 6.6: an input timed out only in the new code; never a difference, never a proof
BLOCK_REASON = {"database": "needs a database", "network": "needs the network", "file write": "writes files", "subprocess": "starts a subprocess"}


@dataclass
class Target:
    file: str
    qualname: str
    kind: str
    params: list[dict]
    code_hash: str
    old_gate: list = field(default_factory=list)  # statements the edit changed, as line ranges in the baseline file
    new_gate: list = field(default_factory=list)  # the same, in the edited file
    source: str = ""                               # the function's text before the edit, for example values
    new_source: str = ""                           # its text after the edit: with source, the edit's own values
    via: str = ""                                  # not edited itself: "calls states_in_range" (FR-41)
    via_file: str = ""                             # the file whose edit made this function worth running
    source_start: int = 0                          # first line of `source` in the baseline file
    requests: list = field(default_factory=list)   # kind "route": the HTTP requests to send (roadmap C3)
    declared: list = field(default_factory=list)   # inputs with an "expect" from declare_change (roadmap D1)
    # A caller (via) passes only if its inputs ran the edited function's changed statements. Two steps away, that is
    # still the edited function, not the caller in between.
    # Every edited function this caller reaches counts (see caller_gate): new-tree statements, plus old-tree ones for
    # edited functions whose edit only deleted code.
    callees: list = field(default_factory=list)    # the edited functions' qualnames
    callee_file: str = ""                          # their file
    callee_old_gate: list = field(default_factory=list)
    callee_new_gate: list = field(default_factory=list)


@dataclass
class Result:
    file: str
    qualname: str
    status: str
    code_hash: str
    reason: str = ""
    inputs_tried: int = 0
    inputs_compared: int = 0
    inputs_completed: int = 0
    dropped_nondeterministic: int = 0
    dropped_invalid: int = 0
    differences: list[dict] = field(default_factory=list)
    only_special: bool = False  # every differing input contains NaN or infinity; ordinary values agree
    only_float_noise: bool = False  # every difference is in the last digits of a float
    inputs_with_io: int = 0         # inputs whose run made database or HTTP calls (answered by stand-ins, roadmap C1)
    declared_failures: list = field(default_factory=list)  # declared examples that do not hold on the new code (roadmap D1)
    declared_checked: int = 0
    mutants_total: int = 0          # planted bugs run after a pass (roadmap B6)
    mutants_killed: int = 0
    mutants_survived: list = field(default_factory=list)
    via: str = ""
    via_file: str = ""
    statements_reached: int = 0     # changed statements run by at least one input (the gate, D-043)
    statements_total: int = 0       # 0 when there was no gate to judge by
    callees: list = field(default_factory=list)  # for a caller: the edited functions whose statements it was judged by
    unreached_note: str = ""        # a caller's difference although its inputs ran none of the changed statements
    carved_captured: int = 0        # distinct calls into this function recorded from its callers on the baseline (Sprint 5 task E)
    carved_added: int = 0           # of those, added to the inputs (not already among them, within max_inputs)
    carved_already: int = 0         # recorded calls that were already among the inputs
    carved_replayed: int = 0        # added calls that ran to a return or an exception in both versions
    carved_unstored: int = 0        # calls (repeats included) not recorded because a value could not be encoded
    carved_note: str = ""           # why no calls were recorded, when the recording run failed or timed out
    carved_weak: int = 0            # of the added calls, those recorded only while guessed caller inputs ran (weak)
    baseline_unrunnable: bool = False  # untested because every own, non-guessed input raised or failed setup on the baseline (roadmap 6.5)
    # Task 6.6: inputs that timed out only in the new code while the old runs finished fast (to be confirmed), and the
    # confirmed ones: {"call", "new_limit_s", "old_seconds", "source"}. Confirmed slowdowns set status SLOWDOWN unless
    # the result is already different or broken.
    slowdown_candidates: list = field(default_factory=list)
    slowdowns: list = field(default_factory=list)

    @property
    def subject(self) -> str:
        return f"{self.file}::{self.qualname}"


def module_for(rel: str, root: str) -> tuple[str, str]:
    """(dotted module, folder that must be on sys.path), found by walking up through __init__.py files."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    i = len(rel.split("/")) - 1  # index of the file name
    pkg_start = i
    while pkg_start > 0 and (Path(root) / "/".join(rel.split("/")[:pkg_start]) / "__init__.py").exists():
        pkg_start -= 1
    # A folder without __init__.py can still be part of the import name (odoo/ is a namespace package: the code
    # says `from odoo.tools import ...`). Stopping below it would put odoo/ itself on sys.path, where odoo/http.py
    # then hides the standard library's http. The repo's own import lines decide.
    while pkg_start > 0 and _imported_with_prefix(root, rel.split("/")[pkg_start - 1], parts[pkg_start] if pkg_start < len(parts) else ""):
        pkg_start -= 1
    module = ".".join(parts[pkg_start:])
    return module, "/".join(rel.split("/")[:pkg_start])


_PREFIX_CACHE: dict = {}


def _imported_with_prefix(root: str, folder: str, top: str) -> bool:
    """True if the repo imports `folder.top` more often than bare `top`."""
    if not folder.isidentifier() or not top.isidentifier():
        return False
    key = (root, folder, top)
    if key not in _PREFIX_CACHE:
        def count(pattern: str) -> int:
            try:
                res = subprocess.run(["git", "-C", root, "grep", "-h", "-E", "-e", pattern, "--", "*.py"], capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                return 0
            return len(res.stdout.splitlines())
        sp = "[[:space:]]"
        with_prefix = count(rf"^{sp}*(from|import){sp}+{folder}\.{top}(\.|{sp}|$)")
        bare = count(rf"^{sp}*(from|import){sp}+{top}(\.|{sp}|$)")
        _PREFIX_CACHE[key] = with_prefix > bare
    return _PREFIX_CACHE[key]


def changed_targets(rel: str, before: str | None, after: str | None) -> list[Target]:
    """Functions whose code changed, plus functions in the file that read a changed module-level constant."""
    if after is None or before is None:
        return []
    now = {s.qualname: s for s in symbols.extract(after)[0]}
    out: dict[str, Target] = {}
    for c in symbol_diff.diff(before, after):
        if c.kind in ("body_changed", "signature_changed") and c.after.kind != "class":
            if same_code(before, after, c.before, c.after):
                continue  # comments, whitespace or the docstring only: running it cannot show anything
            old = c.before
            old_gate, new_gate = examples.gates(before, after, c.before, c.after)
            out[c.qualname] = Target(rel, c.qualname, c.after.kind, old.params, symbols.sha(c.after.node_text),
                                     old_gate=old_gate, new_gate=new_gate, source=old.node_text, source_start=old.line_start,
                                     new_source=c.after.node_text)
        elif c.kind in ("renamed", "visibility_changed") and c.before.kind != "class":
            pass  # the old name is gone; static checks report it, and callers show up at Stop
    changed_names = _changed_constants(before, after)
    if changed_names:
        for q, s in now.items():
            if s.kind == "class" or q in out:
                continue
            if _reads_any(s.node_text, changed_names):
                before_sym = next((b for b in symbols.extract(before)[0] if b.qualname == q), None)
                if before_sym is not None:
                    out[q] = Target(rel, q, s.kind, before_sym.params, symbols.sha(s.node_text + "|".join(sorted(changed_names))))
    return sorted(out.values(), key=lambda t: t.qualname)


def _strip_own_docstring(node):
    """The function without its own top docstring. Nested functions and classes keep theirs."""
    import ast
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        node.body = body[1:] or [ast.Pass()]
    return node


def _function_dump(source: str, line: int, keep_docstring: bool) -> str | None:
    """The parsed function defined at `line`, decorators included, without positions. None if not found."""
    import ast
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    node = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno == line), None)
    if node is None:
        return None
    return ast.dump(node if keep_docstring else _strip_own_docstring(node))


# Code that reads docstrings at run time: there a docstring is behaviour (ply grammars, docopt usage, click help).
DOC_READERS = re.compile(r"__doc__|^[ \t]*(?:import|from)[ \t]+(?:ply|docopt|click)\b", re.M)


def same_code(before: str, after: str, before_sym, after_sym) -> bool:
    """True if the two versions of a function parse to the same code once comments and whitespace are set aside, and
    its own docstring too unless the file reads docstrings at run time. Such an edit cannot change what the function
    does (validation run 1, tqdm 9)."""
    keep = bool(DOC_READERS.search(before) or DOC_READERS.search(after))
    old, new = _function_dump(before, before_sym.line_start, keep), _function_dump(after, after_sym.line_start, keep)
    return old is not None and old == new


def _module_assignments(source: str) -> dict[str, str]:
    out = {}
    for stmt in symbols.parse(source).root_node.children:
        if stmt.type == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "assignment":
            left = stmt.named_children[0].child_by_field_name("left")
            if left is not None and left.type == "identifier":
                out[symbols.text(left)] = symbols.text(stmt)
    return out


def _changed_constants(before: str, after: str) -> set[str]:
    a, b = _module_assignments(before), _module_assignments(after)
    return {k for k in a.keys() & b.keys() if a[k] != b[k]}


def _reads_any(source: str, names: set[str]) -> bool:
    found = False

    def walk(node):
        nonlocal found
        if node.type == "identifier" and symbols.text(node) in names:
            found = True
        if not found:
            for c in node.children:
                walk(c)
    walk(symbols.parse(source).root_node)
    return found


FAILED = "__failed__"


def _run_worker(cfg, py: str, root: str, sys_path: list[str], functions: list[dict], timeout: float, warm_names: list[str] | None = None) -> dict:
    bd = cfg.behavioral_diff
    with tempfile.TemporaryDirectory(prefix="lgrun") as tmp:
        req, res = Path(tmp) / "req.json", Path(tmp) / "res.json"
        request = {"root": root, "sys_path": sys_path, "protect": [str(cfg.root)], "functions": functions,
                   "django_settings": cfg.raw.get("django_settings"), "deterministic": bool(bd.get("deterministic", True)),
                   "io": str(bd.get("io", "stub")),
                   "warm": warm_names or []}
        req.write_text(json.dumps(request))
        env = clean_env(cfg)
        env.pop("PYTHONPATH", None)
        mode = "baseline" if os.path.realpath(root) == os.path.realpath(str(Path(cfg.state_dir) / "baseline")) else "work"
        fast = warm.run(cfg, py, root, mode, request, str(res), timeout, env)
        if fast is not None:
            return fast
        cmd = os_sandbox.wrap([py, "-B", str(WORKER), str(req), str(res)], py, writable=[tempfile.gettempdir(), tmp],
                              readonly=[str(cfg.root), root], mode=str(bd.get("os_sandbox", "auto")))
        try:
            done = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {FAILED: "timed out"}
        except OSError as exc:
            return {FAILED: f"could not start the project's Python ({py}): {exc}"}
        try:
            return json.loads(res.read_text())
        except (OSError, ValueError):
            # The worker stopped before it wrote results. Saying "timed out" here would be a wrong reason.
            last = (done.stderr or "").strip().splitlines()[-1:] or [f"exit code {done.returncode}"]
            return {FAILED: f"the check could not run in the project's Python ({py}): {last[0][:200]}"}


def _key(o: dict) -> str:
    return json.dumps(o, sort_keys=True)


def _comparable(o: dict) -> dict | None:
    """What gets compared for one input: return value or exception type and message, and what the call left in its
    mutable arguments and in self. None if not comparable."""
    if o["outcome"] == "return":
        out = {"k": "return", "v": o["value"]}
    elif o["outcome"] == "raise":
        out = {"k": "raise", "type": o["type"], "message": o["message"]}
    else:
        return None
    if o.get("state"):
        out["s"] = o["state"]
    if o.get("io"):
        out["io"] = o["io"]
    return out


def _show_io(event: dict) -> str:
    if event.get("io") == "sql":
        params = f" {json.dumps(event['params'], default=str)}" if event.get("params") not in (None, [], {}) else ""
        return f"SQL {event['sql'][:160]}{params}"
    extra = "".join(f" {k}={json.dumps(event[k], default=str)}" for k in ("params", "body") if event.get(k) not in (None, "", {}))
    return f"HTTP {event.get('method')} {event.get('url')}{extra}"[:240]


SHOWN_VALUE = 60   # characters of one value in a state difference
SHOWN_ITEMS = 3    # differing attributes, keys or positions shown per name


def _short(enc) -> str:
    text = "<removed>" if enc.get("t") == "removed" else lg_codec.display(enc, plain_decimal=True)
    return text if len(text) <= SHOWN_VALUE else text[:SHOWN_VALUE - 1] + "…"


def _keyed(enc) -> dict | None:
    """{shown key: value} for an object's attributes or a dict's items; None for other shapes."""
    if enc.get("t") == "obj" and (enc.get("state") or {}).get("t") == "dict":
        pairs = enc["state"]["v"]
    elif enc.get("t") == "dict":
        pairs = enc["v"]
    else:
        return None
    return {lg_codec.display(k): v for k, v in pairs}


def _state_parts(name: str, mine: dict | None, theirs: dict | None) -> list[str]:
    """What one argument (or self) was left as by this version, where it differs from the other version: the
    attributes, keys or positions that differ, each value cut short. For self as a call's effect (the "effect" mark),
    an attribute the call did not touch is "unchanged"; a self the call did not touch at all is "self unchanged"."""
    if mine is None:
        return [f"{name} unchanged" if name == "self" else f"{name} not readable"]
    if theirs is None:
        theirs = {}
    shown_more = lambda parts, total: parts[:SHOWN_ITEMS] + ([f"+{total - SHOWN_ITEMS} more"] if total > SHOWN_ITEMS else [])  # noqa: E731
    a = _keyed(mine)
    b = _keyed(theirs) if theirs else ({} if a is not None else None)
    if a is not None and b is not None and mine.get("cls") == theirs.get("cls", mine.get("cls")):
        obj = mine.get("t") == "obj"
        keys = sorted(k for k in set(a) | set(b) if _key(a.get(k) or {}) != _key(b.get(k) or {}))
        if keys:
            missing = "unchanged" if mine.get("effect") or not theirs else ("not set" if obj else "absent")
            parts = []
            for k in keys:
                label = f"{name}.{k.strip(chr(39))}" if obj and k.startswith("'") else f"{name}[{k}]"
                if k not in a:
                    parts.append(f"{label} {missing}")
                elif a[k].get("t") == "removed":
                    parts.append(f"{label} removed")
                else:
                    parts.append(f"{label}={_short(a[k])}")
            return shown_more(parts, len(parts))
    if mine.get("t") in ("list", "tuple") and theirs.get("t") == mine.get("t") and len(mine["v"]) == len(theirs.get("v") or []):
        idx = [i for i, (x, y) in enumerate(zip(mine["v"], theirs["v"])) if _key(x) != _key(y)]
        if idx:
            return shown_more([f"{name}[{i}]={_short(mine['v'][i])}" for i in idx], len(idx))
    return [f"{name}={_short(mine)}"]


def _show_outcome(o: dict, other: dict | None = None) -> str:
    """The outcome as people read it. State is shown only for names whose state differs from the other version."""
    text = lg_codec.display(o["v"], plain_decimal=True) if o["k"] == "return" else f"raises {o['type']}: {o['message']}"
    mine, theirs = o.get("s") or {}, (other or {}).get("s") or {}
    changed = [n for n in sorted(set(mine) | set(theirs)) if _key(mine.get(n) or {}) != _key(theirs.get(n) or {})]
    if changed:
        parts = [p for n in changed for p in _state_parts(n, mine.get(n), theirs.get(n))]
        text += ", then " + ", ".join(parts)
    mine_io, theirs_io = o.get("io") or [], (other or {}).get("io") or []
    if _key({"x": mine_io}) != _key({"x": theirs_io}):
        # The first call where the two versions part ways: that is the line a person needs to see.
        i = next((i for i, (x, y) in enumerate(zip(mine_io, theirs_io)) if _key(x) != _key(y)), min(len(mine_io), len(theirs_io)))
        text += f"; call {i + 1}: " + (_show_io(mine_io[i]) if i < len(mine_io) else "(none)")
        if len(mine_io) != len(theirs_io):
            text += f" ({len(mine_io)} calls in all)"
    return text


def show_call(target: Target, item: dict) -> str:
    if target.kind == "route":
        a = {x["name"]: lg_codec.decode(x["value"]) for x in item["args"]}
        extra = "".join(f" {k}={json.dumps(a[k], default=str)}" for k in ("params", "json") if a.get(k) is not None)
        return f"{a.get('method')} {a.get('path')}{extra}"
    name = target.qualname.rsplit(".", 1)[-1]
    parts = []
    for a in item["args"]:
        if a["name"] == "self":
            continue
        if a.get("kind") == "varargs":
            parts += [lg_codec.display(v) for v in a["value"]["v"]]
        elif a.get("kind") == "varkw":
            parts += [f"{lg_codec.display(k).strip(chr(39))}={lg_codec.display(v)}" for k, v in a["value"]["v"]]
        else:
            parts.append(f"{a['name']}={lg_codec.display(a['value'])}")
    return f"{name}({', '.join(parts)})"


FLOAT_NOISE = 1e-9


def _float_noise(a, b) -> bool:
    """True if two results have the same shape and differ only in floats that agree to nine digits."""
    if isinstance(a, dict) and isinstance(b, dict):
        if a.get("t") == "float" and b.get("t") == "float":
            try:
                x, y = float(a["v"]), float(b["v"])
            except (TypeError, ValueError):
                return False
            if x == y or (x != x and y != y):
                return True
            scale = max(abs(x), abs(y))
            return scale != float("inf") and abs(x - y) <= FLOAT_NOISE * max(scale, 1e-300)
        return a.keys() == b.keys() and all(_float_noise(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_float_noise(x, y) for x, y in zip(a, b))
    return a == b


def _has_special(item: dict) -> bool:
    for a in item.get("args", []):
        try:
            if input_lib.is_special(lg_codec.decode(a["value"])):
                return True
        except Exception:  # noqa: BLE001 - a value we cannot decode here is simply not counted as special
            continue
    return False


def compare_one(target: Target, items: list[dict], a: dict | None, b: dict | None, n: dict | None, use_self_diff: bool) -> Result:
    r = Result(target.file, target.qualname, UNTESTED, target.code_hash, inputs_tried=len(items))
    if a is None or n is None or (use_self_diff and b is None):
        r.reason = "timed out"
        return r
    if n["status"] == "import_error" and a["status"] == "ran":
        r.status, r.reason = BROKEN, f"the module no longer imports after the edit: {n.get('reason')}"
        return r
    if a["status"] == "missing":
        r.reason = "new function, so there is no old version to compare"
        return r
    if n["status"] == "missing":
        r.reason = "the function no longer exists under this name"
        return r
    if a["status"] != "ran" or n["status"] != "ran":
        bad = a if a["status"] != "ran" else n
        r.reason = {"uncallable": "a nested function cannot be called directly", "import_blocked": f"importing the module {bad.get('reason')}"}.get(
            bad["status"], f"could not run: {bad.get('reason') or bad['status']}")
        return r
    r.slowdown_candidates = slowdown_candidates(items, a, b, n, use_self_diff)
    replayed = set()
    for item, oa, on in zip(items, a["results"], n["results"]):
        if item.get("source") == "carved" and oa.get("outcome") in ("return", "raise") and on.get("outcome") in ("return", "raise"):
            replayed.add(item.get("carved_id"))
    r.carved_replayed = len(replayed)
    blocked: set[str] = set()
    setup_failures = 0
    setup_detail = ""
    for item, oa, on, ob in zip(items, a["results"], n["results"], (b or {"results": a["results"]})["results"]):
        if item.get("expect") is not None:
            # A declared example: the new code is checked against what was declared, not against the old code.
            from guardlib import intent
            r.declared_checked += 1
            if not intent.matches(item["expect"], on):
                r.declared_failures.append({"call": item.get("call", show_call(target, item)), "expected": intent.describe(item["expect"]),
                                            "got": intent.got_text(on)})
            continue
        blocked.update(oa.get("blocked_kinds", []))
        blocked.update(on.get("blocked_kinds", []))
        if oa["outcome"] == "setup_failed" or on["outcome"] == "setup_failed":
            setup_failures += 1
            setup_detail = setup_detail or oa.get("detail") or on.get("detail") or ""
            continue
        ca, cn = _comparable(oa), _comparable(on)
        if ca is None or cn is None:
            continue
        if item.get("weak") and ca["k"] == "raise" and cn["k"] == "raise":
            # Both versions reject a guessed value: it was never a valid call, so two different rejections prove nothing.
            # If new code accepts what old code rejected, that is a real change and stays.
            r.dropped_invalid += 1
            continue
        if use_self_diff:
            cb = _comparable(ob)
            if cb is None or _key(ca) != _key(cb):
                r.dropped_nondeterministic += 1
                continue
        r.inputs_compared += 1
        if ca.get("io"):
            r.inputs_with_io += 1
        if ca["k"] == "return":
            r.inputs_completed += 1
        if _key(ca) != _key(cn):
            same_state = _key(ca.get("s") or {}) == _key(cn.get("s") or {}) and _key({"x": ca.get("io")}) == _key({"x": cn.get("io")})
            r.differences.append({"call": show_call(target, item), "before": _show_outcome(ca, cn), "after": _show_outcome(cn, ca), "source": item["source"],
                                  **({"base": item["base"]} if item.get("base") else {}), "args": item["args"], "has_self": item.get("has_self", False), "before_raw": ca, "after_raw": cn,
                                  "special": _has_special(item), "state": not same_state,
                                  "noise": same_state and ca["k"] == "return" and cn["k"] == "return" and _float_noise(ca["v"], cn["v"])})
    # A pass must rest on inputs that ran every changed statement (D-043). Guessed values for an untyped parameter often
    # leave by an early return or an error, and old and new code then agree without the edit ever being exercised.
    # A caller is held to the edited functions' statements: agreeing on inputs that never reach them says nothing.
    parts = gate_parts(target, a, n)
    untraced = any(executed is None for _, executed, _ in parts)
    hit, missed, missed_old = [], [], []
    if parts and not untraced:
        for gate, executed, old_tree in parts:
            h, m = examples.coverage(gate, executed)
            hit += h
            (missed_old if old_tree else missed).extend(m)
        r.statements_reached, r.statements_total = len(hit), sum(len(g) for g, _, _ in parts)
    if r.declared_failures and not r.differences:
        r.status = DIFFERENT
        return r
    if r.differences:
        r.status = DIFFERENT
        if target.via and r.statements_total and not r.statements_reached:
            r.unreached_note = (f"The inputs never ran the changed statements of {callee_names(target.callees)} (reached 0 of {r.statements_total}), "
                                "so this difference may come from elsewhere: an earlier edit this session, another function, or non-determinism")
        # The example a person reads first: from real tests or real calls recorded from callers, then the repo's own examples, ordinary values before
        # NaN and infinity, a returned value before an error, and short before long.
        r.differences.sort(key=lambda d: (d["noise"], d["source"] not in ("tests", "carved"), d["source"] != "examples", d["special"],
                                          d["before_raw"]["k"] != "return" or d["after_raw"]["k"] != "return",
                                          len(d["call"]) + len(d["before"]) + len(d["after"])))
        r.only_special = all(d["special"] for d in r.differences)
        r.only_float_noise = all(d["noise"] for d in r.differences)
        return r
    if blocked:
        r.reason = " and ".join(sorted(BLOCK_REASON.get(k, k) for k in blocked))
        return r
    if r.dropped_nondeterministic and r.dropped_nondeterministic >= r.inputs_compared:
        # One input like dice(0) agreeing with itself must not turn a random function into a Verified pass.
        r.reason = f"non-deterministic: old code disagreed with itself on {r.dropped_nondeterministic} of {r.dropped_nondeterministic + r.inputs_compared} inputs"
        return r
    slow = sum(1 for o in a["results"] if o.get("outcome") in ("timeout", "skipped_slow"))
    if r.inputs_completed == 0:
        r.reason = ("no input ran to completion" if not setup_failures else f"inputs could not be set up ({setup_detail[:120]})") + \
                   (f"; {slow} inputs ran past the time limit" if slow else "")
        # On the baseline every input of the caller's own that is not a guess (no declared, carved, weak or edit-derived
        # input) raised or could not be set up: the cause is not this edit (engine ranking, roadmap 6.5).
        old = [oa.get("outcome") for item, oa in zip(items, a["results"])
               if item.get("expect") is None and item.get("source") != "carved" and not _is_guess(item)]
        r.baseline_unrunnable = bool(old) and all(o in ("raise", "setup_failed") for o in old)
        return r
    where = f" of {callee_names(target.callees)}" if target.via and target.callees else ""
    if untraced and target.via:
        r.reason = f"the changed lines{where} could not be traced while it ran"
        return r
    if r.statements_total and not r.statements_reached:
        r.reason = (f"none of the {r.inputs_compared} inputs tried reached the changed lines{where}, so the edit itself was not exercised"
                    f" (reached 0 of {r.statements_total} changed statements)")
        return r
    if missed or missed_old:
        never = []
        if missed:
            never.append(f"{_lines_word(missed)} {examples.show_ranges(missed)} never ran")
        if missed_old:
            never.append(f"{_lines_word(missed_old)} {examples.show_ranges(missed_old)} (in the code before the edit) never ran")
        r.reason = f"inputs reached {r.statements_reached} of {r.statements_total} changed statements{where}; " + "; ".join(never)
        return r
    r.status = DONE
    m = a.get("mutation") or {}
    if m.get("applicable"):
        r.mutants_total, r.mutants_killed, r.mutants_survived = m["total"], m["killed"], m.get("survived", [])
    return r


def _lines_word(ranges: list) -> str:
    return "lines" if len(ranges) > 1 or ranges[0][0] != ranges[0][1] else "line"


def callee_names(callees: list) -> str:
    """'a, b' from edited functions' qualnames."""
    return ", ".join(dict.fromkeys(c.rsplit(".", 1)[-1] for c in callees)) or "the edited function"


def caller_gate(edited: list[Target]) -> dict:
    """Target fields that gate a caller on every edited function it reaches (all in one file): their changed
    statements in the new tree, or in the old tree for an edit that only deleted code."""
    edited = [t for t in edited if t is not None]
    if not edited:
        return {}
    return {"callees": [t.qualname for t in edited], "callee_file": edited[0].file,
            "callee_new_gate": [g for t in edited for g in t.new_gate],
            "callee_old_gate": [g for t in edited if not t.new_gate for g in t.old_gate]}


def gate_parts(target: Target, a: dict, n: dict) -> list[tuple[list, list | None, bool]]:
    """[(gate, executed lines, gate is in the old tree)]. A function's own gate is in the new tree, or the old one when
    the edit only deleted code. A caller is judged by the edited functions' statements, traced while it ran."""
    if target.via:
        parts = []
        if target.callee_new_gate:
            parts.append((target.callee_new_gate, n.get("gate_lines"), False))
        if target.callee_old_gate:
            parts.append((target.callee_old_gate, a.get("gate_lines"), True))
        return parts
    if target.new_gate:
        return [(target.new_gate, n.get("lines"), False)]
    if target.old_gate:
        return [(target.old_gate, a.get("lines"), True)]
    return []


def reached_text(r: Result) -> str:
    """'reached 2 of 2 changed statements', or '' without a gate."""
    if not r.statements_total:
        return ""
    return f"reached {r.statements_reached} of {r.statements_total} changed statements"


def _tree_specs(specs: list[dict], runnable: dict, old: bool) -> list[dict]:
    """The specs for one tree, with the edited function's changed statements to trace for each caller."""
    out = []
    for spec in specs:
        t, _ = runnable[f"{spec['module']}:{spec['qualname']}"]
        ranges = (t.callee_old_gate if old else t.callee_new_gate) if t.via and t.callee_file else []
        out.append({**spec, "gate": {"file": t.callee_file, "ranges": ranges}} if ranges else spec)
    return out


def carved_text(r: Result) -> str:
    """What recording the callers' calls gave this function (Sprint 5 task E), or '' when it had no callers to record."""
    if r.carved_note:
        return r.carved_note[:1].upper() + r.carved_note[1:]
    unstored = f"{r.carved_unstored} calls (repeats included) had a value that could not be stored" if r.carved_unstored else ""
    if not r.carved_captured:
        return f"No call from its callers could be recorded: {unstored}" if unstored else ""
    parts = [f"{r.carved_already} already among its inputs"] if r.carved_already else []
    cut = r.carved_captured - r.carved_already - r.carved_added
    if cut > 0:
        parts.append(f"{cut} left out at the input limit")
    if r.carved_unstored:
        parts.append(f"{r.carved_unstored} more calls, repeats included, had a value that could not be stored")
    extra = f" ({'; '.join(parts)})" if parts else ""
    weak = (f"; {r.carved_weak} of the {r.carved_added} added were recorded only while guessed caller inputs ran, so two different "
            f"errors on them are not counted") if r.carved_weak else ""
    return (f"{r.carved_captured} calls recorded from its callers on the code before the edit{extra}; "
            f"{r.carved_replayed} of the {r.carved_added} added ran to a return or an exception in both versions{weak}")


def confidence_text(r: Result) -> str:
    """'7 of 8 planted bugs caught' with a plain verdict, or '' when no bugs were planted."""
    if not r.mutants_total:
        return ""
    share = r.mutants_killed / r.mutants_total
    verdict = "strong inputs" if share >= 0.75 else "fair inputs" if share >= 0.4 else "weak inputs: this pass says little"
    return f"{r.mutants_killed} of {r.mutants_total} planted bugs in the changed lines were caught by the same inputs ({verdict})"


ALIAS = re.compile(r"^[ \t]*(?:type[ \t]+)?([A-Z]\w*)[ \t]*(?::[ \t]*(?:typing\.)?TypeAlias)?[ \t]*=[ \t]*([^\n#]+?)[ \t]*(?:#.*)?$", re.M)
ALIAS_VALUE = re.compile(r"[\w.\[\], |\"']+")
BUILDABLE = re.compile(r"\b(int|float|str|bool|Decimal|date|datetime|list|tuple|dict|set|None|Sequence|Iterable|Mapping)\b")


def _aliases(text: str) -> dict[str, str]:
    """Type names defined in the same file, such as `NumberOrString: TypeAlias = float | str` (FR-40).

    Only definitions that are made of types the input builder knows are kept, so a constant is never mistaken for one."""
    out = {}
    for name, value in ALIAS.findall(text):
        value = value.strip().strip("\"'")
        if ALIAS_VALUE.fullmatch(value) and BUILDABLE.search(value) and not value[0].isdigit():
            out[name] = value.replace(" ", "")
    return out


def _resolve_aliases(params: list[dict], aliases: dict[str, str]) -> list[dict]:
    if not aliases:
        return params
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, aliases)) + r")\b")
    out = []
    for p in params:
        ann = p.get("annotation")
        out.append({**p, "annotation": pattern.sub(lambda m: aliases[m.group(1)], ann)} if ann else p)
    return out


def _index(cfg):
    from indexer import db
    try:
        return db.connect(cfg.root) if db.exists(cfg.root) else None
    except Exception:  # noqa: BLE001 - example values are a bonus
        return None


def _merged_edit(edits: list[dict]) -> dict | None:
    """One edit_info from several (the edited functions a caller reaches): each part's values, in order, repeats dropped."""
    edits = [e for e in edits if e]
    if not edits:
        return None
    out: dict = {}
    for part in ("literals", "fragments", "numbers", "regexes"):
        seen, values = set(), []
        for e in edits:
            for v in e.get(part) or []:
                sig = json.dumps([type(v).__name__, v], sort_keys=True, default=repr)
                if sig not in seen:
                    seen.add(sig)
                    values.append(v)
        out[part] = values
    return out


def _edit_of(t: Target, cache: dict) -> dict | None:
    """examples.edit_info for an edited function, worked out once per check; None for a caller or on any failure."""
    key = (t.file, t.qualname)
    if key not in cache:
        cache[key] = None
        if t.source and t.new_source and not t.via:
            try:
                cache[key] = examples.edit_info(t.source, t.new_source)
            except Exception:  # noqa: BLE001 - edit inputs are a bonus
                pass
    return cache[key]


def _callee_edit(t: Target, targets: list[Target], cache: dict) -> dict | None:
    """For a caller: the merged edit info of the edited functions in this check that it reaches (Sprint 6 task 6.1)."""
    if not t.via or not t.callee_file:
        return None
    return _merged_edit([_edit_of(e, cache) for e in targets
                         if not e.via and e.kind != "route" and e.file == t.callee_file and e.qualname in t.callees])


def _examples(cfg, conn, t: Target, edit: dict | None = None, regex_cache: dict | None = None) -> dict | None:
    """Example values and calls from the function's own text and from its callers, tests first (FR-36).

    edit: the edit info its edit-derived inputs are built from (the function's own edit, or for a caller the edits of
    the edited functions it reaches); None for an edited function means its own edit, worked out here. regex_cache: shared with the edited function, so a caller reuses its regex samples
    and does not start another sampling child; a caller's regex work then stays within its own seed budget."""
    if not t.source:
        return None
    name = t.qualname.rsplit(".", 1)[-1]
    params = [p["name"] for p in t.params if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]
    # One time budget for collecting examples, objects and edit inputs (regex sampling draws on it too).
    deadline = time.monotonic() + input_lib.SEED_BUDGET_S
    try:
        # Without an index, files that import the function from its own module come first (Sprint 6 task 6.2).
        files = examples.calling_files(conn, name, t.file) or examples.files_naming(cfg.root, name, own_file=t.file)
        out = examples.collect(cfg.root, t.source, name, params, files, deadline=deadline)
    except Exception:  # noqa: BLE001
        return None
    # Both are bonuses: a failure leaves the examples as they were.
    out["seed_deadline"] = deadline
    try:
        out["objects"] = examples.param_objects(cfg.root, t.file, name, t.params, files, deadline)
    except Exception:  # noqa: BLE001
        pass
    if edit is None and not t.via:
        edit = _edit_of(t, {})
    if edit:
        out["edit"] = edit
        if regex_cache is not None:
            out["regex_cache"] = regex_cache
        if t.via:
            out["regex_floor"] = False
    return out


def _self_calls(cfg, conn, t: Target, work_root: str) -> list[dict]:
    """Constructor calls for a method's class, from the repo, in the worker's input format (roadmap B3)."""
    if t.kind != "method" or "." not in t.qualname:
        return []
    cls_qual = t.qualname.rsplit(".", 1)[0]
    cls_name = cls_qual.rsplit(".", 1)[-1]
    try:
        text = (Path(work_root) / t.file).read_text(errors="replace")
        defs = {s.qualname: s for s in symbols.extract(text)[0]}
    except OSError:
        return []
    init = defs.get(f"{cls_qual}.__init__")
    if init is not None:
        params = [p["name"] for p in init.params if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]
    elif cls_qual in defs:
        from guardlib import checks
        params = list(checks._class_fields(defs[cls_qual]))  # a dataclass: its fields, in order, are the parameters
    else:
        return []
    try:
        calls = examples.constructor_calls(cfg.root, t.file, cls_name, params, examples.calling_files(conn, cls_name, t.file))
    except Exception:  # noqa: BLE001 - a bonus: without it the worker falls back to other recipes
        return []
    out = []
    for call in calls:
        try:
            out.append({"args": [{"name": n, "kind": k, "value": lg_codec.encode_input(v)} for n, k, v in call]})
        except Exception:  # noqa: BLE001
            continue
    return out


def _with_mutants(specs: list[dict], runnable: dict, limit: int, old_root: str | None = None) -> list[dict]:
    """The specs for the first baseline run, with planted bugs for each edited function (roadmap B6). Only that run
    gets them: the mutants are compared with the old function's own results, so the other runs stay as fast as before.
    old_root: the baseline tree, whose module text tells the planner about `from re import sub` style aliases."""
    if limit <= 0:
        return specs
    out = []
    texts: dict = {}
    for spec in specs:
        t, _ = runnable[f"{spec['module']}:{spec['qualname']}"]
        if t.kind == "route":
            out.append(spec)
            continue
        plan = None
        if t.source and t.source_start and not t.via:
            if t.file not in texts:
                try:
                    texts[t.file] = (Path(old_root) / t.file).read_text(errors="replace") if old_root else None
                except (OSError, ValueError):
                    texts[t.file] = None
            try:
                plan = mutants.generate(t.source, t.source_start, t.old_gate, limit, module_source=texts[t.file])
            except Exception:  # noqa: BLE001 - planted bugs are a bonus: without them the pass has no confidence score
                plan = None
        out.append({**spec, "mutants": plan} if plan else spec)
    return out


MAX_CARVED = 40          # recorded calls per edited function, distinct by encoded value
CARVE_SHARE = 0.25       # of the per-run time limit; the main runs get what the capture pass left
CARVE_CALLER_INPUTS = 100  # caller inputs sent to the capture pass (the worker stops early once the cap is reached)


def _carve_plan(specs: list[dict], runnable: dict) -> tuple[list[dict], dict]:
    """(capture specs for the baseline, {edited key: [caller keys]}). A capture spec is a caller's own spec with the
    edited functions it reaches to watch. Only edited functions in this check with a caller in this check count."""
    edited = {key: t for key, (t, _) in runnable.items() if not t.via and t.kind != "route"}
    by_caller: dict[str, list[dict]] = {}
    for key, (t, _) in runnable.items():
        if not t.via or not t.callee_file:
            continue
        for ekey, e in edited.items():
            if e.file == t.callee_file and e.qualname in t.callees:
                by_caller.setdefault(key, []).append({"module": ekey.split(":", 1)[0], "qualname": e.qualname, "max": MAX_CARVED})
    out = []
    for spec in specs:
        key = f"{spec['module']}:{spec['qualname']}"
        if key in by_caller and spec.get("kind") != "http":
            inputs = [{**i, "weak": _is_guess(i)} for i in spec["inputs"][:CARVE_CALLER_INPUTS]]
            out.append({k: v for k, v in spec.items() if k not in ("mutants", "gate")} | {"inputs": inputs, "capture": by_caller[key]})
    return out, by_caller


def _is_guess(item: dict) -> bool:
    """True exactly where compare_one treats the caller's own input as weak: inputs marked weak (generated values for
    untyped parameters, objects tried on parameters nothing else fills) and edit-derived values. Generated values for
    typed parameters, a no-argument call, tests, examples, declared and carved inputs are not guesses. A call recorded
    while a guess runs is a guess too: it is replayed as a weak carved input."""
    return item.get("expect") is None and (bool(item.get("weak")) or item.get("source") == "edit")


def _carved_items(results: list[dict], key: str) -> tuple[list[dict], int]:
    """(the distinct calls recorded for one edited function across its callers' capture results, as inputs without an
    id yet; calls skipped because a value could not be encoded, repeats included).

    A call recorded only while guessed caller inputs ran is weak (two different rejections of it prove nothing); one
    also recorded under a caller input the project wrote, by any caller, is not."""
    items, seen, skipped = [], {}, 0
    for res in results:
        for call in (res.get("captured") or {}).get(key, []):
            sig = _key({"a": call.get("args"), "s": bool(call.get("has_self"))})
            weak = bool(call.get("weak"))
            if sig in seen:
                if not weak:
                    seen[sig].pop("weak", None)
                continue
            if len(items) >= MAX_CARVED:
                continue
            item = {"args": call["args"], "has_self": bool(call.get("has_self")), "source": "carved"}
            if weak:
                item["weak"] = True
            seen[sig] = item
            items.append(item)
        skipped += int((res.get("unstored") or {}).get(key, 0) or 0)
    return items, skipped


def _without_self(item: dict) -> dict:
    """The input with its recorded `self` left out, so the worker builds a fresh instance for it."""
    if not item.get("has_self"):
        return item
    return {**item, "args": [a for a in item["args"] if a.get("name") != "self"], "has_self": False}


def _merge_carved(items: list[dict], carved: list[dict], max_inputs: int) -> tuple[list[dict], dict]:
    """(inputs, counts). Carved calls go after declared examples and inputs harvested from tests, ahead of everything
    else; the whole stays within max_inputs, and what does not fit is cut from the carved calls, never from the head.
    Calls already among the inputs are not repeated. Each carved call that is added gets a carved_id.
    counts: {"already": calls already among the inputs, "cut": calls left out at the input limit, "added": calls added}."""
    have = {_key({"a": i.get("args"), "s": bool(i.get("has_self"))}) for i in items}
    fresh, seen = [], set()
    for c in carved:
        sig = _key({"a": c["args"], "s": bool(c.get("has_self"))})
        if sig not in have and sig not in seen:
            seen.add(sig)
            fresh.append(c)
    already = len(carved) - len(fresh)
    k = 0
    while k < len(items) and (items[k].get("expect") is not None or items[k].get("source") == "tests"):
        k += 1
    head, rest = items[:k], items[k:]
    room = max(0, max_inputs - len(head))
    added = [{**c, "carved_id": i} for i, c in enumerate(fresh[:room])]
    merged = head + added + rest[:max(0, max_inputs - len(head) - len(added))]
    return merged, {"already": already, "cut": len(fresh) - len(added), "added": len(added)}


def _parsed(cache: dict, root: str, rel: str):
    """(source, ast tree) of a file in a tree, parsed once per check; None if it cannot be read or parsed."""
    key = (root, rel)
    if key not in cache:
        import ast
        try:
            text = (Path(root) / rel).read_text(errors="replace")
            cache[key] = (text, ast.parse(text))
        except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
            cache[key] = None
    return cache[key]


def _class_texts(cache: dict, root: str, rel: str) -> dict | None:
    """{class qualname: its source text, with each method's text as a separate entry under "Class.method"}."""
    got = _parsed(cache, root, rel)
    if got is None:
        return None
    import ast
    text, tree = got
    out: dict = {}

    def walk(nodes, prefix):
        for n in nodes:
            if isinstance(n, ast.ClassDef):
                q = f"{prefix}{n.name}"
                out[q] = ast.get_source_segment(text, n) or ""
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out[f"{q}.{m.name}"] = ast.get_source_segment(text, m) or ""
                walk(n.body, q + ".")
    walk(tree.body, "")
    return out


def _imported_bases(cache: dict, root: str, rel: str, cls: str) -> list[tuple[str, str]]:
    """[(file, class name)] for the bases of cls defined in the same file or imported with `from x import Name` from a
    module in the same tree. Bases reached any other way (module attributes, star imports, installed packages) are not followed."""
    got = _parsed(cache, root, rel)
    if got is None:
        return []
    import ast
    _, tree = got
    node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls.split(".")[0]), None)
    for part in cls.split(".")[1:]:
        node = next((n for n in (node.body if node else []) if isinstance(n, ast.ClassDef) and n.name == part), None)
    if node is None:
        return []
    names = {b.id for b in node.bases if isinstance(b, ast.Name)}
    # Bases defined in the same file: compared already, but their own imported bases are followed from there.
    out = [(rel, n.name) for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    for imp in tree.body:
        if not isinstance(imp, ast.ImportFrom):
            continue
        for alias in imp.names:
            if (alias.asname or alias.name) not in names:
                continue
            parts = (imp.module or "").split(".") if imp.module else []
            if imp.level:
                base = Path(rel).parent
                for _ in range(imp.level - 1):
                    base = base.parent
                candidates = [base.joinpath(*parts)] if parts else [base]
            else:
                # Absolute imports resolve from the tree root, the file's folder, and the import root of the file's
                # package (src layouts: src/pkg/base.py is imported as pkg.base).
                try:
                    pkg_root = module_for(rel, root)[1]
                except Exception:  # noqa: BLE001
                    pkg_root = ""
                candidates = ([Path(*parts), Path(rel).parent.joinpath(*parts)] +
                              ([Path(pkg_root).joinpath(*parts)] if pkg_root else [])) if parts else []
            for c in candidates:
                for f in (f"{c}.py", f"{c}/__init__.py"):
                    if (Path(root) / f).is_file():
                        out.append((f, alias.name))
                        break
                else:
                    continue
                break
    return out


def _class_reshaped(baseline_root: str, work_root: str, t: Target, cache: dict | None = None) -> bool:
    """True if a `self` recorded on the baseline may have a shape no instance of the new code has. pickle skips
    __init__, so such an input could show a difference real calls never do (a field the new __init__ adds).

    Conservative: true when the class that owns method t changed anywhere other than in t itself, when any other class
    in the same file changed (a base class, or a class whose instances end up in self), or when a base class imported
    with `from x import Base` from the same tree changed (followed up to three steps). A base class reached another way
    (`import x` then `x.Base`, a star import, an installed package) is not covered."""
    if t.kind != "method" or "." not in t.qualname:
        return False
    cache = {} if cache is None else cache
    cls = t.qualname.rsplit(".", 1)[0]
    old, new = _class_texts(cache, baseline_root, t.file), _class_texts(cache, work_root, t.file)
    if old is None or new is None or cls not in old or cls not in new:
        return True

    def rest(texts: dict) -> dict:
        out = {}
        for q, text in texts.items():
            if q == t.qualname:
                continue
            out[q] = text.replace(texts.get(t.qualname, "\0"), "", 1) if q == cls else text
        return out
    if rest(old) != rest(new):
        return True
    todo, seen = [(t.file, cls, 0)], set()
    while todo:
        rel, name, depth = todo.pop()
        if depth >= 3 or (rel, name) in seen:
            continue
        seen.add((rel, name))
        for f, base in _imported_bases(cache, work_root, rel, name):
            a, b = _class_texts(cache, baseline_root, f), _class_texts(cache, work_root, f)
            if a is None or b is None or a.get(base) != b.get(base) or \
                    any(a.get(q) != b.get(q) for q in set(a) | set(b) if q.startswith(base + ".")):
                return True
            todo.append((f, base, depth + 1))
    return False


def _carve(cfg, py: str, baseline_root: str, sys_path: list[str], specs: list[dict], runnable: dict, limit_s: float,
           warm_names: list[str], max_inputs: int, reshaped: set) -> dict:
    """The capture pass (Sprint 5 task E): run the callers' inputs on the baseline, record the calls they make into the
    edited functions, and put those calls into the edited functions' inputs as source "carved". A recorded `self` is
    left out for a method whose class changed outside the method (reshaped keys, see _class_reshaped).

    Returns {edited key: {"captured", "unstored", "already", "cut", "added", "note"}}, with "__elapsed__": seconds the
    pass took. Fails open: any problem leaves the inputs as they were, and the note says why nothing was recorded."""
    started = time.monotonic()
    out: dict = {}
    try:
        capture_specs, by_caller = _carve_plan(specs, runnable)
        if not capture_specs:
            return {}
        edited_keys = list(dict.fromkeys(f"{e['module']}:{e['qualname']}" for entries in by_caller.values() for e in entries))
        per_caller = max(0.5, limit_s * 0.6 / len(capture_specs))
        capture_specs = [{**c, "budget": per_caller} for c in capture_specs]
        got = _run_worker(cfg, py, baseline_root, sys_path, capture_specs, limit_s, warm_names)
        if not got or FAILED in got:
            why = (got or {}).get(FAILED) or "no result"
            note = f"the run that records its callers' calls stopped after {time.monotonic() - started:.1f} s ({why}), so none were recorded"
            out = {k: {"captured": 0, "unstored": 0, "already": 0, "cut": 0, "added": 0, "note": note} for k in edited_keys}
            return out
        for ekey in edited_keys:
            callers = [got.get(c) or {} for c, entries in by_caller.items() if any(f"{e['module']}:{e['qualname']}" == ekey for e in entries)]
            items, skipped = _carved_items(callers, ekey)
            info = {"captured": len(items), "unstored": skipped, "already": 0, "cut": 0, "added": 0, "note": ""}
            if items:
                if ekey in reshaped:
                    items = [_without_self(i) for i in items]
                spec = next(s for s in specs if f"{s['module']}:{s['qualname']}" == ekey)
                spec["inputs"], counts = _merge_carved(spec["inputs"], items, max_inputs)
                info.update(counts)
                info["weak"] = sum(1 for i in spec["inputs"] if i.get("source") == "carved" and i.get("weak"))
            out[ekey] = info
        return out
    except Exception:  # noqa: BLE001 - carving is a bonus; the check runs on the inputs it had
        return out
    finally:
        if out:
            out["__elapsed__"] = time.monotonic() - started


def _add_carved_edit_items(cfg, specs: list[dict], carved: dict, seeds: dict, max_inputs: int) -> None:
    """Edit-derived variants of the carved calls (Sprint 6 task 6.1), added to the specs in place before the three main
    runs, so OLD-A, OLD-B and NEW get the same inputs. Built only for edited functions that got carved calls and have an
    edit to draw on; regex work stays within each function's seed budget. Fails open: a problem leaves the inputs as
    they were."""
    for spec in specs:
        key = f"{spec['module']}:{spec['qualname']}"
        seed = seeds.get(key)
        if not seed or not (carved.get(key) or {}).get("added"):
            continue
        try:
            spec["inputs"], added = input_lib.with_carved_edit_items(seed["sym"], spec["inputs"], seed["edit"], cfg.state_dir,
                                                                     max_inputs, seed["deadline"], seed["cache"])
        except Exception:  # noqa: BLE001
            continue
        carved[key]["edit_added"] = added


def _finish_specs(specs: list[dict], runnable: dict, results: dict) -> tuple[list[dict], dict]:
    """Specs ready to run: an edited function that is still without inputs is untested, and a method with few inputs
    has them repeated so each way of building its instance runs."""
    out_specs, out_runnable = [], {}
    for spec in specs:
        key = f"{spec['module']}:{spec['qualname']}"
        t, _ = runnable[key]
        items = spec["inputs"]
        if not items:
            results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason="no inputs could be built for its signature")
            continue
        if t.kind == "method" and len(items) < SELF_VARIANTS:
            # Each input runs on a differently built instance, so a method with few inputs is repeated to see each one.
            items = [dict(item) for item in (items * SELF_VARIANTS)[:SELF_VARIANTS]]
            spec = {**spec, "inputs": items}
        out_specs.append(spec)
        out_runnable[key] = (t, items)
    return out_specs, out_runnable


def run(cfg, baseline_root: str, work_root: str, targets: list[Target], harvest: dict | None = None) -> list[Result]:
    """Run old twice and new once, in parallel subprocesses, on the same inputs."""
    # The test-file list is cached per root for one check; a long-lived caller (backtest, the validation
    # harness) may have checked out another commit since, so every check starts from a fresh list.
    examples._TEST_FILES.clear()
    if harvest is None:
        harvest = load_harvest(cfg)
    bd = cfg.behavioral_diff
    py = cfgmod.target_python(cfg)
    results: dict[str, Result] = {}
    specs, runnable, shown = [], {}, []
    roots: set[str] = set()
    extra = cfg.raw.get("source_roots") or []
    conn = _index(cfg)
    alias_cache: dict[str, dict] = {}
    edit_cache: dict = {}
    regex_caches: dict = {}  # (file, qualname) of an edited function -> its regex samples, shared with its callers
    seeds: dict = {}  # an edited function's key -> what its edit inputs were built from, for carved bases (task 6.1)
    for t in targets[:MAX_FUNCTIONS]:
        if t.kind == "route":
            app = cfg.raw.get("app") or ""
            if not t.requests or not app:
                results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason="no requests could be built for this route")
                continue
            app_module = app.split(":", 1)[0]
            specs.append({"module": app_module, "qualname": t.qualname, "kind": "http", "app": app, "inputs": t.requests})
            runnable[f"{app_module}:{t.qualname}"] = (t, t.requests)
            continue
        if t.file not in alias_cache:
            try:
                alias_cache[t.file] = _aliases((Path(work_root) / t.file).read_text(errors="replace"))
            except OSError:
                alias_cache[t.file] = {}
        params = _resolve_aliases(t.params, alias_cache[t.file])
        if t.via:
            edit = _callee_edit(t, targets, edit_cache)
            # A caller never draws regex samples of its own: it reuses those of the edited function it reaches, when there
            # is exactly one and its samples are already drawn, and otherwise has none (its splices are still screened).
            reached = [(e.file, e.qualname) for e in targets if not e.via and e.file == t.callee_file and e.qualname in t.callees]
            got = regex_caches.get(reached[0]) if len(reached) == 1 else None
            regex_cache = {"samples": list(got["samples"]) if got and "samples" in got else []}
        else:
            edit = _edit_of(t, edit_cache)
            regex_cache = regex_caches.setdefault((t.file, t.qualname), {})
        ex = _examples(cfg, conn, t, edit, regex_cache)
        sym = {"qualname": t.qualname, "kind": t.kind, "params": params}
        items, reason = input_lib.build(sym, harvest.get(f"{t.file}::{t.qualname}"), int(bd["max_inputs"]), cfg.state_dir,
                                        examples=ex, stub_db=str(bd.get("io", "stub")) == "stub")
        if reason:
            results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason=reason)
            continue
        if t.declared:
            items = [dict(i) for i in t.declared] + items
        if not items and not (not t.via and any(c.via and c.callee_file == t.file and t.qualname in c.callees for c in targets[:MAX_FUNCTIONS])):
            results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason="no inputs could be built for its signature")
            continue
        # An edited function with callers in this check stays even without inputs of its own: the calls its callers
        # make may give it some (carving, below).
        module, sys_rel = module_for(t.file, work_root)
        spec = {"module": module, "qualname": t.qualname, "inputs": items}
        self_calls = _self_calls(cfg, conn, t, work_root)
        if self_calls:
            spec["self_calls"] = self_calls
        specs.append(spec)
        runnable[f"{module}:{t.qualname}"] = (t, items)
        roots.add(sys_rel)
        if not t.via and edit:
            seeds[f"{module}:{t.qualname}"] = {"sym": sym, "edit": edit, "deadline": (ex or {}).get("seed_deadline"), "cache": regex_cache}
    for t in targets[MAX_FUNCTIONS:]:
        results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason=f"more than {MAX_FUNCTIONS} functions changed at once")
    carved: dict = {}  # edited function's key -> (calls recorded from its callers, calls skipped as unencodable)
    if specs:
        def paths_for(base: str) -> list[str]:
            ordered = list(dict.fromkeys([*extra, *sorted(roots), ""]))
            return [str(Path(base) / p) if p else base for p in ordered]
        deadline = float(bd["timeout_s"])
        use_self = bool(bd.get("self_diff", True))
        warm_names = warm.third_party_imports(work_root, sorted({t.file for t, _ in runnable.values()}), extra)
        # A `self` recorded on the baseline (from tests or from callers) is unpickled without __init__, so it keeps the
        # old class's shape. When the class changed outside the method, such inputs get a freshly built instance instead.
        parse_cache: dict = {}
        reshaped = {key for key, (t, _) in runnable.items() if _class_reshaped(baseline_root, work_root, t, parse_cache)}
        for spec in specs:
            if f"{spec['module']}:{spec['qualname']}" in reshaped:
                spec["inputs"] = [_without_self(i) for i in spec["inputs"]]
        carved = _carve(cfg, py, baseline_root, paths_for(baseline_root), specs, runnable, deadline * CARVE_SHARE, warm_names,
                        int(bd["max_inputs"]), reshaped)
        recorded_s = carved.pop("__elapsed__", 0.0)
        if any(info["added"] for info in carved.values()):
            # Carved inputs to run: the recording time comes out of the same limit, and the three runs get what it left.
            # A recording run that gave nothing leaves the limit as it was.
            deadline = max(deadline - recorded_s, deadline * (1 - CARVE_SHARE))
        _add_carved_edit_items(cfg, specs, carved, seeds, int(bd["max_inputs"]))
        specs, runnable = _finish_specs(specs, runnable, results)
    if specs:
        specs_old = _tree_specs(specs, runnable, old=True)
        specs_a = _with_mutants(specs_old, runnable, int(bd.get("mutants", 8)), baseline_root)
        specs_n = _tree_specs(specs, runnable, old=False)
        with cf.ThreadPoolExecutor(3) as pool:
            fa = pool.submit(_run_worker, cfg, py, baseline_root, paths_for(baseline_root), specs_a, deadline, warm_names)
            fn = pool.submit(_run_worker, cfg, py, work_root, paths_for(work_root), specs_n, deadline, warm_names)
            fb = pool.submit(_run_worker, cfg, py, baseline_root, paths_for(baseline_root), specs_old, deadline, warm_names) if use_self else None
            a, n = fa.result(), fn.result()
            b = fb.result() if fb else None
        failed = next((x[FAILED] for x in (a, n, b) if x and FAILED in x), None)
        setup_note = next((x["__setup_error__"] for x in (n, a) if x and "__setup_error__" in x), None)
        for key, (t, items) in runnable.items():
            if failed:
                reason = failed
                if failed == "timed out" and deadline < float(bd["timeout_s"]):
                    reason = (f"timed out: the runs had {deadline:.0f} s of the {float(bd['timeout_s']):.0f} s limit, after "
                              f"{recorded_s:.1f} s spent recording its callers' calls")
                results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, inputs_tried=len(items), reason=reason)
                continue
            results[t.qualname] = compare_one(t, items, a.get(key) if a else None, b.get(key) if b else None, n.get(key) if n else None, use_self)
            if setup_note and results[t.qualname].status == UNTESTED:
                results[t.qualname].reason += f" (the project's Django settings did not load: {setup_note})"
        if not failed and a and n:
            confirm_slowdowns(cfg, py, baseline_root, work_root, paths_for(baseline_root), paths_for(work_root), specs_old, specs_n, runnable, results)
    for t in targets:
        results[t.qualname].via, results[t.qualname].via_file = t.via, t.via_file
        results[t.qualname].callees = list(t.callees)
    for key, info in carved.items():
        r = results.get(key.split(":", 1)[1])
        if r is not None:
            r.carved_captured, r.carved_unstored, r.carved_note = info["captured"], info["unstored"], info["note"]
            r.carved_added, r.carved_already = info["added"], info["already"]
            r.carved_weak = info.get("weak", 0)
    return [results[t.qualname] for t in targets]


# ---- slowdown (Task 6.6, roadmap 3d) ----
# An input that times out only in the new code (youtube-dl 10: the buggy regex backtracks for 23 s, the old code returns
# at once). Timing is noisy (the per-call limit drops to 0.5 s after two timeouts, and three workers share the machine),
# so a candidate needs both old runs to have returned within a small share of the full limit, and is reported only after
# each version is re-run alone on that input with the full limit: the new code must time out again and the old code
# must still be fast. A reduced limit in the first run never decides anything by itself; the re-run does.
CALL_TIMEOUT_S = 5            # matches lg_worker.CALL_TIMEOUT_S, the full per-call limit
SLOWDOWN_OLD_SHARE = 0.1      # both old runs returned within this share of the full limit
MAX_SLOWDOWN_CHECKS = 2       # inputs re-run per check (each can take the full limit in the new code)
SLOWDOWN_MARGIN_S = 30        # process start and imports, on top of the calls themselves


def _fast_in(run: dict | None, i: int) -> float | None:
    """Seconds the call on input i took in this run, when it returned or raised within the slowdown share; else None."""
    if not run or run.get("status") != "ran":
        return None
    outcomes, secs = run.get("results") or [], run.get("seconds") or []
    if i >= len(outcomes) or i >= len(secs) or secs[i] is None or outcomes[i].get("outcome") not in ("return", "raise"):
        return None
    return secs[i] if secs[i] <= CALL_TIMEOUT_S * SLOWDOWN_OLD_SHARE else None


def slowdown_candidates(items: list[dict], a: dict, b: dict | None, n: dict, use_self_diff: bool) -> list[dict]:
    """Inputs that timed out in the new run while OLD-A (and OLD-B, when self-diff is on) returned fast. Each is only a
    candidate: "reduced" says the new run had the 0.5 s limit then, which alone proves nothing."""
    out = []
    limits = n.get("limits") or []
    for i, (item, on) in enumerate(zip(items, n.get("results") or [])):
        if item.get("expect") is not None or on.get("outcome") != "timeout" or i >= len(limits) or limits[i] is None:
            continue
        old = [_fast_in(a, i)] + ([_fast_in(b, i)] if use_self_diff else [])
        if any(s is None for s in old):
            continue
        out.append({"index": i, "reduced": limits[i] < CALL_TIMEOUT_S, "old_seconds": max(old), "source": item.get("source") or ""})
    return out


def confirm_slowdowns(cfg, py: str, baseline_root: str, work_root: str, paths_old: list[str], paths_new: list[str],
                      specs_old: list[dict], specs_n: list[dict], runnable: dict, results: dict) -> None:
    """Re-run at most MAX_SLOWDOWN_CHECKS candidate inputs alone with the full limit, old first, then new, and record the
    confirmed ones on their Result. Fails open: any error leaves the results as they were."""
    try:
        picked = []  # (key, target, candidate)
        for key, (t, items) in runnable.items():
            r = results.get(t.qualname)
            if r is None or r.status == BROKEN:
                continue
            seen = set()  # the same call from two sources (tests and examples) is re-run once
            for c in sorted(r.slowdown_candidates, key=lambda c: (c["source"] not in ("tests", "carved", "examples"), c["index"])):
                shown = show_call(t, items[c["index"]]) if c["index"] < len(items) else None
                if shown is None or shown in seen or len(picked) >= MAX_SLOWDOWN_CHECKS:
                    continue
                seen.add(shown)
                picked.append((key, t, c))
        if not picked:
            return
        keys = list(dict.fromkeys(key for key, _, _ in picked))

        def alone(specs: list[dict]) -> list[dict]:
            by_key = {f"{s['module']}:{s['qualname']}": s for s in specs}
            out = []
            for key in keys:
                spec = {k: v for k, v in by_key[key].items() if k != "mutants"}
                mine = [c["index"] for k, _, c in picked if k == key]
                out.append({**spec, "inputs": [spec["inputs"][i] for i in mine], "indexes": mine, "full_limit": True})
            return out
        limit = CALL_TIMEOUT_S * len(picked) + SLOWDOWN_MARGIN_S
        old = _run_worker(cfg, py, baseline_root, paths_old, alone(specs_old), limit)
        if not old or FAILED in old:
            return
        new = _run_worker(cfg, py, work_root, paths_new, alone(specs_n), limit)
        if not new or FAILED in new:
            return
        by_key = {f"{s['module']}:{s['qualname']}": s for s in specs_n}
        for key in keys:
            mine = [(t, c) for k, t, c in picked if k == key]
            ro, rn = old.get(key) or {}, new.get(key) or {}
            for j, (t, c) in enumerate(mine):
                outcomes, limits = rn.get("results") or [], rn.get("limits") or []
                if rn.get("status") != "ran" or j >= len(outcomes) or outcomes[j].get("outcome") != "timeout" or j >= len(limits) \
                        or limits[j] != CALL_TIMEOUT_S:
                    continue
                old_s = _fast_in(ro, j)
                if old_s is None:
                    continue
                r = results[t.qualname]
                r.slowdowns.append({"call": show_call(t, by_key[key]["inputs"][c["index"]]), "new_limit_s": CALL_TIMEOUT_S,
                                    "old_seconds": max(old_s, c["old_seconds"]), "source": c["source"]})
                if r.status not in (DIFFERENT, BROKEN):
                    r.status = SLOWDOWN
    except Exception:  # noqa: BLE001 - a slowdown check that cannot run must never change or hide the diff's result
        return


def slowdown_text(s: dict) -> str:
    return (f"the new code ran past {s['new_limit_s']:g} s on {s['call']} while the old code returned in {max(s['old_seconds'], 0.001):.3f} s; "
            "this is a slowdown, not a proof of a wrong result")


def format_note(results: list[Result]) -> str:
    """One factual line per function that was checked without finding a difference, or could not be checked."""
    lines = []
    own = [r for r in results if not r.via]
    callers = [r for r in results if r.via and r.status == DONE]
    for r in own[:4]:
        if r.status == DONE:
            conf = confidence_text(r)
            io = (f"Database and HTTP calls were answered by stand-ins (no rows, HTTP 200 with an empty body), so for "
                  f"{r.inputs_with_io} inputs what agreed is the calls made, not their real results. ") if r.inputs_with_io else ""
            reach = f"; the inputs {reached_text(r)}" if r.statements_total else ""
            carved = carved_text(r)
            lines.append(f"LegacyGuard ran {r.subject} before and after this edit: no difference on {r.inputs_compared} inputs "
                         f"({r.inputs_completed} returned a value{reach}). " + (f"{carved}. " if carved else "") + (f"{conf}. " if conf else "") + io +
                         "The inputs are the repo's own examples plus guesses, so this is not a proof; values it did not try can still differ.")
        elif r.status == UNTESTED:
            carved = carved_text(r)
            lines.append(f"LegacyGuard could not check {r.subject} by running it: {r.reason}. " + (f"{carved}. " if carved else "") +
                         "Nothing is known about this edit from execution.")
        elif r.status == SLOWDOWN and r.slowdowns:
            more = f" ({len(r.slowdowns) - 1} more such input)" if len(r.slowdowns) > 1 else ""
            lines.append(f"LegacyGuard ran {r.subject} before and after this edit: {slowdown_text(r.slowdowns[0])}{more}. "
                         "Each version was re-run alone on that input with the full limit and did the same again.")
    if len(own) > 4:
        lines.append(f"+{len(own) - 4} more functions; /legacyguard:status lists them.")
    if callers:
        names = ", ".join(c.qualname + (f" ({reached_text(c)} of {callee_names(c.callees)})" if c.statements_total
                                         else " (not gated: the edit left no statements to check)") for c in callers[:5])
        lines.append(f"{len(callers)} function{'s that call' if len(callers) != 1 else ' that calls'} the edited code {'were' if len(callers) != 1 else 'was'} also run "
                     f"({names}): no difference on the inputs tried. Callers that could not be run are not listed.")
    return "\n".join(lines)


def format_wake(result: Result, finding_id: str) -> str:
    """The counterexample text Claude receives (FR-15). Factual, replayable, within the context limit."""
    total = result.inputs_compared
    if result.status == SLOWDOWN and result.slowdowns:
        return format_slowdown_wake(result, finding_id)
    if result.declared_failures:
        lines = [f"LegacyGuard declared change not holding [{finding_id}] in {result.subject}: "
                 f"{len(result.declared_failures)} of {result.declared_checked} declared examples fail on the current code."]
        lines += [f"{d['call']}  declared {d['expected']}  got {d['got']}" for d in result.declared_failures[:MAX_SHOWN]]
        lines.append("Revise the code, or call declare_change again if the intended change itself is different.")
        return "\n".join(lines)
    lines = [f"LegacyGuard behavior difference [{finding_id}] in {result.subject}: {len(result.differences)} of {total} inputs differ between the session baseline and the current code."]
    if result.via:
        lines.append(f"This function was not edited. It {result.via}, which was edited in {result.via_file}.")
    for d in result.differences[:MAX_SHOWN]:
        lines.append(f"{d['call']}  before {d['before']}  after {d['after']}  [{d['source']}]")
    if any(d.get("source") == "carved" for d in result.differences[:MAX_SHOWN]):
        lines.append("Inputs marked [carved] are calls a caller of this function made on the code before the edit, recorded and replayed.")
    if result.only_special:
        lines.append("Every differing input contains NaN or infinity. On the ordinary values tried, old and new code agree.")
    if result.only_float_noise:
        lines.append("Every difference found is in the last digits of a float (the two results agree to nine digits). That can be plain float "
                     "arithmetic, or the visible edge of a rounding change: a value on a rounding boundary can still come out a whole step different.")
    if result.unreached_note:
        lines.append(f"{result.unreached_note}.")
    if result.slowdowns:
        lines.append(f"Separately, {slowdown_text(result.slowdowns[0])}.")
    if result.reason:
        lines.append(f"Also: {result.reason}.")
    lines.append(f"Whether the request intended this is unknown. If it did not, revising the code clears this and the check re-runs by itself. "
                 f"If it did, call the declare_change tool with the function, what changes, and a few examples of the new results: "
                 f"the guard checks them and records the change as intended. The developer can also accept it with /legacyguard:waive {finding_id} \"<reason>\".")
    return "\n".join(lines)


def format_slowdown_wake(result: Result, finding_id: str) -> str:
    """Task 6.6: the text for a confirmed slowdown. It never blocks and never claims a wrong result."""
    first = result.slowdowns[0]
    lines = [f"LegacyGuard slowdown [{finding_id}] in {result.subject}: {slowdown_text(first)}."]
    if result.via:
        lines.append(f"This function was not edited. It {result.via}, which was edited in {result.via_file}.")
    for s in result.slowdowns[1:MAX_SHOWN]:
        lines.append(f"Also on {s['call']}: the new code ran past {s['new_limit_s']:g} s, the old code returned in {max(s['old_seconds'], 0.001):.3f} s.")
    lines.append(f"Both runs of the code before the edit returned on this input within {SLOWDOWN_OLD_SHARE:.0%} of the {CALL_TIMEOUT_S} s per-call "
                 "limit; each version was then re-run alone on it with the full limit, and the new code timed out again while the old code "
                 "returned again. Timing depends on the machine. This is not counted as a behavior difference and does not block the edit.")
    if result.reason:
        lines.append(f"Also: {result.reason}.")
    lines.append("Common causes are a regular expression that backtracks (nested or overlapping quantifiers) and a loop that no longer "
                 "ends early. If the slowdown is not intended, revising the code clears this and the check re-runs by itself; "
                 "if it is expected, nothing needs to be done (this is reported once per version of the code).")
    return "\n".join(lines)
