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
from runner import feedback
from runner import inputs as input_lib
from runner import lg_codec
from runner import mutants
from runner import noise
from runner import os_sandbox
from runner import scratch
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
    new_source_start: int = 0                      # first line of `new_source` in the edited file (Task 6.8e)
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
    # Sprint 7.2a: the gate is the first body statement because only the decorators changed ("got past the decorators").
    # A difference with that gate unreached comes from the decorators themselves, so it carries no "may come from
    # elsewhere" hedge. For a caller or a route, true when every gated edited function it reaches has such a gate.
    entry_gate: bool = False
    # Sprint 7.2a, kind "route": {"module", "qualname"} of the handler, traced to tell whether a request entered it.
    handler: dict = field(default_factory=dict)
    # Sprint 7.2b, routes of a deleted or renamed handler: the handler in the code before the edit, traced in the old tree
    # in place of `handler` (which names the renamed function, or is empty for a deleted one); and a note added to the
    # result's reason (the path read from a decorator may lack a router or blueprint prefix).
    old_handler: dict = field(default_factory=dict)
    route_note: str = ""


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
    # Task 6.8d: why OLD-A and OLD-B disagreed on the dropped inputs. causes: cause -> inputs (noise.ADDRESSES, ...);
    # detail: "cause in where" -> inputs, e.g. {"time readings in self": 3}.
    nondeterministic_causes: dict = field(default_factory=dict)
    nondeterministic_detail: dict = field(default_factory=dict)
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
    # Task 6.8c: how the placeholder `self` was built (worker "self_recipe" with "self_weak"), and the compared inputs
    # that ran on it. Such inputs count as guesses; the note, wake, ledger and CI say the instance was a placeholder.
    self_placeholder: str = ""
    placeholder_inputs: int = 0
    placeholder_note: str = ""      # for a difference: it may come from the placeholder state, not the edit
    clock_note: str = ""            # Task 6.8d: every difference is confined to frozen clock readings
    baseline_unrunnable: bool = False  # untested because every own, non-guessed input raised or failed setup on the baseline (roadmap 6.5)
    # Task 6.6: inputs that timed out only in the new code while the old runs finished fast (to be confirmed), and the
    # confirmed ones: {"call", "new_limit_s", "old_seconds", "source"}. Confirmed slowdowns set status SLOWDOWN unless
    # the result is already different or broken.
    slowdown_candidates: list = field(default_factory=list)
    slowdowns: list = field(default_factory=list)
    # Task 6.8e: changed statements no input ran ([first, last, in the old tree]); for each, what must hold for it to
    # run, in words; and the second pass aimed at those conditions: inputs added, changed statements it reached that the
    # first pass had not, seconds it took, and why there was none when there was none.
    unreached_ranges: list = field(default_factory=list)
    unreached_chains: list = field(default_factory=list)
    feedback_tried: int = 0
    feedback_reached: int = 0
    feedback_seconds: float = 0.0
    feedback_note: str = ""
    feedback_raised: int = 0        # changed statements only second-pass inputs ran, on which both versions raised (not counted)
    feedback_dropped: int = 0       # ... ran only by second-pass inputs dropped otherwise (old disagreed with itself, set-up failed)
    feedback_only: bool = False     # every difference is on a second-pass input (a guess); CI still fails, the wake hedges
    compared: list = field(default_factory=list)  # positions of the inputs that were compared (compare_one's own drops left out)
    # Sprint 7.2a, routes: compared requests that ran at least one changed statement, compared requests that entered the
    # handler in the edited code, and the compared requests' status codes in the code before the edit ({"401": 4}).
    route: bool = False
    requests_reached: int = 0
    requests_entered: int = 0
    status_counts: dict = field(default_factory=dict)
    route_deleted: bool = False     # Sprint 7.2b: the route's handler was deleted; the gate is the old handler's body

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
        if c.kind in ("body_changed", "signature_changed", "decorators_changed") and c.after.kind != "class":
            if same_code(before, after, c.before, c.after):
                continue  # comments, whitespace or the docstring only: running it cannot show anything
            old = c.before
            old_gate, new_gate = examples.gates(before, after, c.before, c.after)
            entry = False
            if not old_gate and not new_gate and c.before.decorators != c.after.decorators:
                # Sprint 7.2a: only the decorators changed. A pass needs some input that got through them into the body.
                new_gate = examples.entry_gate(after, c.after)
                entry = bool(new_gate)
            out[c.qualname] = Target(rel, c.qualname, c.after.kind, old.params,
                                     symbols.sha(c.after.node_text + ("|decorators|" + c.after.decorators_hash
                                                                      if c.before.decorators != c.after.decorators else "")),
                                     old_gate=old_gate, new_gate=new_gate, source=old.node_text, source_start=old.line_start,
                                     new_source=c.after.node_text, new_source_start=c.after.line_start, entry_gate=entry)
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
    # Task 7.4: the working folder is a throwaway one per tree per check (scratch.for_trees in run), shared by every
    # run on that tree, so OLD-A and OLD-B see the same os.getcwd().
    with scratch.current(cfg, root) as cwd, tempfile.TemporaryDirectory(prefix="lgrun") as tmp:
        req, res = Path(tmp) / "req.json", Path(tmp) / "res.json"
        # scratch: where writes during import and input setup are redirected for this run; deleted with tmp.
        request = {"root": root, "cwd": cwd.path, "cwd_root": cwd.folder or cwd.tree or cwd.path, "sys_path": sys_path, "protect": [str(cfg.root)], "functions": functions,
                   "scratch": str(Path(tmp) / "scratch"),
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
        writable = [tempfile.gettempdir(), tmp] + ([cwd.path] if cwd.strategy != "off" else [])
        cmd = os_sandbox.wrap([py, "-B", str(WORKER), str(req), str(res)], py, writable=writable,
                              readonly=[str(cfg.root), root], mode=str(bd.get("os_sandbox", "auto")))
        try:
            done = subprocess.run(cmd, cwd=cwd.path, env=env, capture_output=True, text=True, timeout=timeout)
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


def _clock_only(reads_old, reads_new, ca: dict, cn: dict) -> bool:
    """True when old and new read the frozen clock a different number of times and every differing piece of their
    outcomes is a number (Task 6.8d): the difference may be the clock shift alone."""
    if reads_old is None or reads_new is None or reads_old == reads_new:
        return False
    try:
        return noise.numbers_only(ca, cn)
    except Exception:  # noqa: BLE001
        return False


def _note_disagreement(r: Result, ca: dict, cb: dict | None, env_a: dict | None, env_b: dict | None) -> None:
    """Count why the two old runs disagreed on one input (Task 6.8d). Each cause and each "cause in where" counts once
    per input."""
    try:
        found = noise.causes(ca, cb, env_a, env_b)
    except Exception:  # noqa: BLE001 - a diagnosis must never cost the comparison
        found = [(noise.OTHER, "the outcome")]
    for cause in {c for c, _ in found}:
        r.nondeterministic_causes[cause] = r.nondeterministic_causes.get(cause, 0) + 1
    for label in {f"{c} in {w}" for c, w in found}:
        r.nondeterministic_detail[label] = r.nondeterministic_detail.get(label, 0) + 1


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
    # Task 6.8c: a placeholder `self` (placeholder constructor arguments, or made without the constructor). Inputs run on
    # it are guesses. If old and new code did not build it the same way (the edit changed the constructor), the inputs
    # on it are not compared: they would differ by the instance, not by the method.
    weak_self = bool(a.get("self_weak") or n.get("self_weak"))
    if weak_self:
        r.self_placeholder = n.get("self_recipe") or a.get("self_recipe") or "a placeholder instance"
    self_mismatch = weak_self and a.get("self_recipe") != n.get("self_recipe")
    reads_a, reads_n = a.get("clock_reads") or [], n.get("clock_reads") or []
    for i, (item, oa, on, ob) in enumerate(zip(items, a["results"], n["results"], (b or {"results": a["results"]})["results"])):
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
        on_placeholder = weak_self and not item.get("has_self")
        if on_placeholder and self_mismatch and oa["outcome"] != "setup_failed" and on["outcome"] != "setup_failed":
            setup_failures += 1
            setup_detail = setup_detail or (f"the placeholder instance was built differently by the old code "
                                            f"({a.get('self_recipe') or 'none'}) and the new code ({n.get('self_recipe') or 'none'})")
            continue
        if oa["outcome"] == "setup_failed" or on["outcome"] == "setup_failed":
            setup_failures += 1
            setup_detail = setup_detail or oa.get("detail") or on.get("detail") or ""
            continue
        ca, cn = _comparable(oa), _comparable(on)
        if ca is None or cn is None:
            continue
        if (item.get("weak") or on_placeholder) and ca["k"] == "raise" and cn["k"] == "raise":
            # Both versions reject a guessed value: it was never a valid call, so two different rejections prove nothing.
            # If new code accepts what old code rejected, that is a real change and stays.
            r.dropped_invalid += 1
            continue
        if use_self_diff:
            cb = _comparable(ob)
            if cb is None or _key(ca) != _key(cb):
                r.dropped_nondeterministic += 1
                _note_disagreement(r, ca, cb, a.get("env"), (b or {}).get("env"))
                continue
        r.inputs_compared += 1
        r.compared.append(i)
        r.placeholder_inputs += on_placeholder
        if ca.get("io"):
            r.inputs_with_io += 1
        if ca["k"] == "return":
            r.inputs_completed += 1
        if _key(ca) != _key(cn):
            same_state = _key(ca.get("s") or {}) == _key(cn.get("s") or {}) and _key({"x": ca.get("io")}) == _key({"x": cn.get("io")})
            r.differences.append({"call": show_call(target, item), "before": _show_outcome(ca, cn), "after": _show_outcome(cn, ca), "source": item["source"],
                                  **({"base": item["base"]} if item.get("base") else {}), "args": item["args"], "has_self": item.get("has_self", False), "before_raw": ca, "after_raw": cn,
                                  "special": _has_special(item), "state": not same_state, "placeholder_self": on_placeholder,
                                  "noise": same_state and ca["k"] == "return" and cn["k"] == "return" and _float_noise(ca["v"], cn["v"]),
                                  "clock_only": _clock_only(reads_a[i] if i < len(reads_a) else None,
                                                             reads_n[i] if i < len(reads_n) else None, ca, cn)})
    # A pass must rest on inputs that ran every changed statement (D-043). Guessed values for an untyped parameter often
    # leave by an early return or an error, and old and new code then agree without the edit ever being exercised.
    # A caller is held to the edited functions' statements: agreeing on inputs that never reach them says nothing.
    route = target.kind == "route"
    parts = gate_parts(target, a, n, r.compared if route else None)
    untraced = any(executed is None for _, executed, _ in parts)
    hit, missed, missed_old = [], [], []
    if parts and not untraced:
        for gate, executed, old_tree in parts:
            h, m = examples.coverage(gate, executed)
            hit += h
            (missed_old if old_tree else missed).extend(m)
        r.statements_reached, r.statements_total = len(hit), sum(len(g) for g, _, _ in parts)
        r.unreached_ranges = [[s, e, False] for s, e in missed] + [[s, e, True] for s, e in missed_old]
    facts = _route_facts(r, target, a, n, parts) if route else None
    if facts and facts["new_route"]:
        # The code before the edit answered every request 404/405 before any handler ran, and the edited code enters the
        # handler: the route is new in this edit (a path changed in its decorator). An addition is not a finding.
        r.differences = []
        r.reason = (f"the route is new in this edit: the code before it answered all {r.inputs_compared} requests "
                    f"{facts['codes_old']} before any handler ran, so there is no old behaviour to compare")
        return r
    if r.declared_failures and not r.differences:
        r.status = DIFFERENT
        return r
    if r.differences:
        r.status = DIFFERENT
        # No hedge for a route whose gate is its own handler's entry gate after a decorator-only edit: requests that never
        # got past the changed decorators show what the decorators do. A caller, or a route whose handler only calls the
        # edited function, may never have called it at all, so the note stays, worded for decorators.
        own_entry = route and target.entry_gate and not _route_via(target)
        if (target.via or route) and not own_entry and r.statements_total and not r.statements_reached:
            who = "requests" if route else "inputs"
            if target.entry_gate:
                r.unreached_note = (f"The {who} never got past the changed decorators of {callee_names(target.callees)}, or never "
                                    f"called it (reached 0 of {r.statements_total}), so this difference may come from those "
                                    "decorators or from elsewhere: an earlier edit this session, another function, or non-determinism")
            else:
                r.unreached_note = (f"The {who} never ran the changed statements of {callee_names(target.callees)} (reached 0 of {r.statements_total}), "
                                    "so this difference may come from elsewhere: an earlier edit this session, another function, or non-determinism")
        # The example a person reads first: from real tests or real calls recorded from callers, then the repo's own examples, ordinary values before
        # NaN and infinity, a returned value before an error, and short before long.
        on_ph = sum(1 for d in r.differences if d.get("placeholder_self"))
        if on_ph:
            share = "Every differing input" if on_ph == len(r.differences) else f"{on_ph} of the {len(r.differences)} differing inputs"
            r.placeholder_note = (f"{share} ran on a placeholder instance ({r.self_placeholder}), not one built by the project's own code, "
                                  "so the difference may come from the placeholder state rather than from the edit")
        r.differences.sort(key=lambda d: (d["noise"], d.get("placeholder_self", False), d["source"] not in ("tests", "carved"), d["source"] != "examples", d["special"],
                                          d["before_raw"]["k"] != "return" or d["after_raw"]["k"] != "return",
                                          len(d["call"]) + len(d["before"]) + len(d["after"])))
        r.only_special = all(d["special"] for d in r.differences)
        r.only_float_noise = all(d["noise"] for d in r.differences)
        if all(d.get("clock_only") for d in r.differences):
            r.clock_note = ("The difference is confined to numbers, and the two versions read the frozen clock a different number "
                            "of times: the clock moves 1 ms per reading, so an edit that adds or removes a clock read shifts "
                            "every later reading and may account for it")
        return r
    if blocked:
        r.reason = " and ".join(sorted(BLOCK_REASON.get(k, k) for k in blocked))
        return r
    if r.dropped_nondeterministic and r.dropped_nondeterministic >= r.inputs_compared:
        # One input like dice(0) agreeing with itself must not turn a random function into a Verified pass.
        r.reason = f"non-deterministic: old code disagreed with itself on {r.dropped_nondeterministic} of {r.dropped_nondeterministic + r.inputs_compared} inputs"
        if r.nondeterministic_detail:
            r.reason += f": {noise.summary(r.nondeterministic_detail)}"
        return r
    slow = sum(1 for o in a["results"] if o.get("outcome") in ("timeout", "skipped_slow"))
    loops = sum(1 for o in a["results"] if o.get("outcome") == "timeout" and o.get("clock_loop"))
    if r.inputs_completed == 0:
        r.reason = ("no input ran to completion" if not setup_failures else f"inputs could not be set up ({setup_detail[:400]})") + \
                   (f"; {slow} inputs ran past the time limit" if slow else "") + \
                   (f"; {loops} of them after many frozen clock readings (a loop waiting for a deadline on the frozen clock, "
                    "which moves 1 ms per reading and only waits it knows of move it further)" if loops else "") + \
                   (f"; the instance was a placeholder ({r.self_placeholder})" if r.self_placeholder and not self_mismatch else "")
        # On the baseline every input of the caller's own that is not a guess (no declared, carved, weak or edit-derived
        # input) raised or could not be set up: the cause is not this edit (engine ranking, roadmap 6.5).
        old = [oa.get("outcome") for item, oa in zip(items, a["results"])
               if item.get("expect") is None and item.get("source") != "carved" and not _is_guess(item)]
        r.baseline_unrunnable = bool(old) and all(o in ("raise", "setup_failed") for o in old)
        return r
    if route:
        return _route_verdict(r, target, facts, untraced, missed, missed_old)
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


EARLY_STATUS = range(400, 500)


def _deleted_handler(target: Target) -> bool:
    """Sprint 7.2b: a route whose handler this edit deleted (no handler in the edited code, the old one traced)."""
    return target.kind == "route" and not target.handler and bool(target.old_handler)


def _route_via(target: Target) -> bool:
    """A route whose handler is only a caller of the edited function (its gate is that function's), not the handler."""
    handlers = {(h or {}).get("qualname", "") for h in (target.handler, target.old_handler)} - {""}
    return not (target.callee_file == target.file and len(target.callees) == 1 and target.callees[0] in handlers)


def _codes(counts: dict) -> str:
    """'401' when every request got one code, else '401 x3, 403 x1'."""
    if len(counts) == 1:
        return next(iter(counts))
    return ", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _route_facts(r: Result, target: Target, a: dict, n: dict, parts: list) -> dict:
    """Sprint 7.2a: what the compared requests of a route did in each tree. Fills r.route, r.status_counts,
    r.requests_entered and r.requests_reached. An early response is one produced while no line of the handler ran in
    that tree (validation, dependencies, before-request hooks, decorators, routing, middleware); entry is None when the
    handler could not be resolved."""
    r.route = True
    r.route_deleted = _deleted_handler(target)
    sa, sn = a.get("statuses") or [], n.get("statuses") or []
    ea, en = a.get("entered"), n.get("entered")
    pairs = [(i, sa[i] if i < len(sa) else None, sn[i] if i < len(sn) else None,
              ea[i] if ea is not None and i < len(ea) else None, en[i] if en is not None and i < len(en) else None) for i in r.compared]
    counts: dict = {}
    for _, so, _, _, _ in pairs:
        if so is not None:
            counts[str(so)] = counts.get(str(so), 0) + 1
    r.status_counts = counts
    r.requests_entered = sum(1 for p in pairs if p[4] is True)
    gate_new = [x for g, _, old in parts if not old for x in g]
    gate_old = [x for g, _, old in parts if old for x in g]

    def ran_any(run: dict, gate: list, i: int) -> bool:
        per = run.get("request_gate") or []
        lines = set(per[i]) if i < len(per) else set()
        return any(line in lines for s, e in gate for line in range(s, e + 1))
    r.requests_reached = sum(1 for p in pairs if (gate_new and ran_any(n, gate_new, p[0])) or (gate_old and ran_any(a, gate_old, p[0])))
    known = bool(pairs) and all(p[3] is not None and p[4] is not None for p in pairs)
    same_early = bool(pairs) and all(so is not None and so == sn_ and so in EARLY_STATUS and (not known or (ea_ is False and en_ is False))
                                     for _, so, sn_, ea_, en_ in pairs)
    # The new-route rule needs the old 404/405 to come from routing itself: no route of the old app matched the method
    # and path (the worker's per-request "routed"). A 404 from a dependency, hook or decorator the edit removed is a
    # real change, never an addition.
    ra = a.get("routed") or []
    unrouted = all(i < len(ra) and ra[i] is False for i, *_ in pairs)
    new_route = bool(pairs) and known and unrouted and all(so in (404, 405) and ea_ is False and en_ is True
                                                           for _, so, _, ea_, en_ in pairs)
    # Sprint 7.2b: whether the edited code still serves the path (a handler of its app matched, or the traced handler ran).
    rn = n.get("routed") or []
    served_new = any((i < len(rn) and rn[i] is True) or en_ is True for i, _, _, _, en_ in pairs)
    new_counts: dict = {}
    for _, _, sn_, _, _ in pairs:
        if sn_ is not None:
            new_counts[str(sn_)] = new_counts.get(str(sn_), 0) + 1
    return {"same_early": same_early, "known": known, "new_route": new_route, "codes_old": _codes(counts) if counts else "",
            "served_new": served_new, "codes_new": _codes(new_counts) if new_counts else "",
            "any_entered_new": any(p[4] is True for p in pairs), "entry_unknown": not pairs or any(p[4] is None for p in pairs)}


def _route_verdict(r: Result, target: Target, facts: dict, untraced: bool, missed: list, missed_old: list) -> Result:
    """Sprint 7.2a: the status of a route with no difference. With changed statements to reach (M > 0) the statement rule
    decides: verified only when the compared requests ran every one of them; the early-4xx wording is added to the text
    when they ran none. Gate lines can run outside the handler (a dependency or before-request hook calling the edited
    function) while the handler is never entered, so "early" alone never decides when M > 0. With no gate (M == 0: the
    handler only reads a changed constant) the same-early-4xx rule decides, then some compared request must have entered
    the handler in the edited code."""
    name = callee_names(target.callees) if target.callees else (target.handler or {}).get("qualname", "the handler").rsplit(".", 1)[-1]
    count = r.inputs_compared
    early = (f"; every request was answered {facts['codes_old']} before the handler ran, in both versions"
             + ("" if facts["known"] else "; the handler could not be traced, so it may never have run")) if facts["same_early"] else ""
    if untraced and (target.callee_new_gate or target.callee_old_gate):
        r.reason = f"the changed lines of {name} could not be traced while the requests ran"
        return r
    if _deleted_handler(target):
        return _deleted_verdict(r, target, facts, name)
    if r.statements_total:
        if not r.statements_reached:
            r.reason = (f"none of the {count} requests reached the changed lines of {name}, so the edit itself was not "
                        f"exercised (reached 0 of {r.statements_total} changed statements)" + early)
            if not early and facts["codes_old"]:
                r.reason += f"; responses before the edit: {_codes(r.status_counts)}"
            return r
        if missed or missed_old:
            never = []
            if missed:
                never.append(f"{_lines_word(missed)} {examples.show_ranges(missed)} never ran")
            if missed_old:
                never.append(f"{_lines_word(missed_old)} {examples.show_ranges(missed_old)} (in the code before the edit) never ran")
            r.reason = (f"requests reached {r.statements_reached} of {r.statements_total} changed statements of {name}; "
                        + "; ".join(never) + f" ({r.requests_reached} of {count} requests reached any)")
            return r
    else:
        if facts["same_early"]:
            r.reason = f"every request was answered {facts['codes_old']} before the handler ran, in both versions" + \
                       ("" if facts["known"] else "; the handler could not be traced, so it may never have run")
            return r
        if not facts["entry_unknown"] and not facts["any_entered_new"]:
            r.reason = (f"no request reached the handler {name} in the edited code"
                        + (f" (responses before the edit: {_codes(r.status_counts)})" if r.status_counts else ""))
            return r
        if facts["entry_unknown"]:
            # Passed on responses alone: whether any request ran the handler is not known.
            r.status = DONE
            r.reason = (f"entry unknown: the handler {name} could not be traced, so whether any request ran it is not "
                        f"known (responses before the edit: {_codes(r.status_counts)})" if r.status_counts else
                        f"entry unknown: the handler {name} could not be traced")
            return r
    r.status = DONE
    return r


def _deleted_verdict(r: Result, target: Target, facts: dict, name: str) -> Result:
    """Sprint 7.2b: a route whose handler was deleted, with no difference. It passes only when the old handler ran and
    the edited code still serves the path (a handler moved to another file): some compared request matched a route of
    the edited app, or entered a handler there. Agreeing responses alone prove nothing: a handler that answers 404 for
    an unknown id gives the same 404 as a router that no longer has the route."""
    count = r.inputs_compared
    route = target.qualname
    if not r.statements_reached:
        r.reason = (f"none of the {count} requests ran the old handler {name} (deleted in this edit), so the route was "
                    f"not exercised" + (f"; responses before the edit: {_codes(r.status_counts)}" if r.status_counts else ""))
        return r
    if not facts["served_new"]:
        both = (f"every request was answered {facts['codes_old']} in both versions" if facts["codes_old"] == facts["codes_new"]
                else f"responses: {facts['codes_old'] or 'none'} before the edit, {facts['codes_new'] or 'none'} after")
        r.reason = (f"the edited code no longer serves {route}: no request matched a route there; {both} (the old handler "
                    f"{name} ran on {r.requests_reached} of {count} requests and answered them itself), so the responses "
                    "agreeing shows nothing about the route")
        return r
    r.status = DONE
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
    gated = [t for t in edited if t.new_gate or t.old_gate]
    return {"callees": [t.qualname for t in edited], "callee_file": edited[0].file,
            "callee_new_gate": [g for t in edited for g in t.new_gate],
            "callee_old_gate": [g for t in edited if not t.new_gate for g in t.old_gate],
            "entry_gate": bool(gated) and all(t.entry_gate for t in gated)}


def _request_lines(run: dict, compared: list | None) -> list | None:
    """Sprint 7.2a, routes: the gate lines run by the compared requests only (each request's own lines are traced), so a
    request dropped as non-deterministic or failed cannot make a statement count as reached. None when not traced."""
    per = run.get("request_gate")
    if per is None:
        return None
    keep = range(len(per)) if compared is None else compared
    return sorted({line for i in keep if i < len(per) for line in per[i]})


def gate_parts(target: Target, a: dict, n: dict, compared: list | None = None) -> list[tuple[list, list | None, bool]]:
    """[(gate, executed lines, gate is in the old tree)]. A function's own gate is in the new tree, or the old one when
    the edit only deleted code. A caller is judged by the edited functions' statements, traced while it ran. A route
    (Sprint 7.2a) is judged like a caller, by the handler's changed statements or the edited function it reaches: the
    new-tree statements by the new tree's requests and, for a deleted-code gate (callee_old_gate), the old tree's."""
    if target.kind == "route":
        parts = []
        if target.callee_new_gate:
            parts.append((target.callee_new_gate, _request_lines(n, compared), False))
        if target.callee_old_gate:
            parts.append((target.callee_old_gate, _request_lines(a, compared), True))
        return parts
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
    if r.route_deleted:
        ran = "the old handler ran" if r.statements_reached else "the old handler never ran"
        return f"{ran} ({r.requests_reached} of {r.inputs_compared} requests ran it)" if r.inputs_compared else ran
    text = f"reached {r.statements_reached} of {r.statements_total} changed statements"
    if r.route and r.inputs_compared:
        text += f" ({r.requests_reached} of {r.inputs_compared} requests reached them)"
    return text


def _tree_specs(specs: list[dict], runnable: dict, old: bool) -> list[dict]:
    """The specs for one tree, with the edited function's changed statements to trace for each caller."""
    out = []
    for spec in specs:
        t, _ = runnable[f"{spec['module']}:{spec['qualname']}"]
        ranges = (t.callee_old_gate if old else t.callee_new_gate) if (t.via or t.kind == "route") and t.callee_file else []
        spec = {**spec, "gate": {"file": t.callee_file, "ranges": ranges}} if ranges else spec
        if old and t.kind == "route" and t.old_handler:
            spec = {**spec, "handler": t.old_handler}  # Sprint 7.2b: a renamed or deleted handler has its old name there
        out.append(spec)
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


def placeholder_only(r: Result) -> bool:
    """A difference found only on inputs run on a placeholder instance (Task 6.8c): it wakes Claude, hedged, but does
    not fail CI on its own."""
    return (r.status == DIFFERENT and bool(r.differences) and not r.declared_failures
            and all(d.get("placeholder_self") for d in r.differences))


def placeholder_text(r: Result) -> str:
    """For a pass: how many inputs ran on a placeholder instance (Task 6.8c), or ''."""
    if not r.placeholder_inputs:
        return ""
    return (f"{r.placeholder_inputs} of the {r.inputs_compared} inputs ran on a placeholder instance ({r.self_placeholder}), not one "
            "built by the project's own code, so agreement on them is weaker evidence")


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


def _self_types(t: Target, harvest: dict | None) -> dict:
    """{param: {type name: count}} that real test runs passed to the method's class __init__ (harvested, roadmap B4).
    The worker uses them to pick placeholder constructor arguments first (Task 6.8c)."""
    if t.kind != "method" or "." not in t.qualname or not harvest:
        return {}
    rec = harvest.get(f"{t.file}::{t.qualname.rsplit('.', 1)[0]}.__init__") or {}
    types = rec.get("types") or {}
    return {k: v for k, v in types.items() if isinstance(v, dict) and k != "self"}


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


# ---- second pass (Task 6.8e): inputs aimed at the conditions around changed statements the first inputs missed ----
FEEDBACK_MIN_S = 3.0          # seconds of the check's limit a second pass needs left to start building inputs
FEEDBACK_RUN_MIN_S = 1.5      # seconds it needs left, once they are built, to run them
FEEDBACK_ONLY_TEXT = ("The difference shows only on second-pass guesses (inputs built to reach changed lines the first "
                      "inputs missed), not on any input from tests, examples or callers; confirm with a realistic input")
FEEDBACK_BUILD_S = 2.0        # diagnosing and building the second pass's inputs, all functions together
FEEDBACK_MIN_FN_S = 0.5       # of a function's own time (lg_worker FUNCTION_BUDGET_S) still left to run it again
FUNCTION_BUDGET_S = 10.0      # matches lg_worker.FUNCTION_BUDGET_S
# Untested reasons a second pass can help with: some changed statement never ran, or no input ran to completion (the
# guesses all raised before it); not a blocked call, a setup failure or non-determinism.
REACH_REASONS = ("none of the ", "inputs reached ", "no input ran to completion")


def _with_input_lines(specs: list[dict], runnable: dict) -> list[dict]:
    """Specs where each edited function (not a caller or a route) also reports every input's own traced lines."""
    out = []
    for spec in specs:
        t, _ = runnable[f"{spec['module']}:{spec['qualname']}"]
        out.append({**spec, "input_lines": True} if not t.via and t.kind != "route" and spec.get("kind") != "http" else spec)
    return out


def _wants_second_pass(t: Target, r: Result) -> bool:
    return (not t.via and t.kind != "route" and r.status == UNTESTED and bool(r.unreached_ranges)
            and r.reason.startswith(REACH_REASONS))


def _merge_run(first: dict | None, second: dict | None, keep: list | None = None) -> dict | None:
    """One function's first-pass run with its second-pass run appended: outcomes and per-input facts in order, traced
    lines joined. The first run stands alone when the second did not run the same way (status, or `self` built
    differently).

    keep: per second-pass input, whether its lines count. Only second-pass inputs that compare_one compared count:
    one it dropped (both versions raised on a guess, no comparable outcome such as a timeout, old disagreed with itself,
    set-up failed) never makes a changed statement reached. When keep is missing or its length, or that of the
    per-input lines, does not match the second pass's inputs, no second-pass line counts."""
    if not first or not second or first.get("status") != "ran" or second.get("status") != "ran" \
            or first.get("self_recipe") != second.get("self_recipe") or bool(first.get("self_weak")) != bool(second.get("self_weak")):
        return first
    out = dict(first)
    k1, k2 = len(first.get("results") or []), len(second.get("results") or [])
    for k in ("results", "seconds", "limits", "clock_reads", "input_lines"):
        if k in first or k in second:
            out[k] = list(first.get(k) or [None] * k1) + list(second.get(k) or [None] * k2)
    if first.get("lines") is not None and second.get("lines") is not None:
        per = second.get("input_lines")
        if keep is not None and len(keep) == k2 and isinstance(per, list) and len(per) == k2:
            counted = {x for i, lines in enumerate(per) if keep[i] for x in (lines or [])}
        else:
            counted = set()
        out["lines"] = sorted(set(first["lines"]) | counted)
    if first.get("gate_lines") is not None and second.get("gate_lines") is not None:
        out["gate_lines"] = sorted(set(first["gate_lines"]) | set(second["gate_lines"]))
    return out


def _second_compare(t: Target, items: list, fb_items: list, a_runs: tuple, b_runs: tuple, n_runs: tuple, use_self: bool):
    """compare_one on the first and second pass joined, with a changed statement counted as reached only when some
    second-pass input that compare_one actually compared ran it. First every second-pass line counts, to learn which
    second-pass positions compare_one compares (it leaves out both-raise guesses, inputs without a comparable outcome
    such as timeouts, set-up failures, placeholder mismatches and old-vs-old disagreements); then only those count.
    Returns (result, result with every second-pass line, keep, (merged a, b, n)), or None when the runs do not join."""
    every = [True] * len(fb_items)
    use_b = use_self and b_runs[0] is not None and b_runs[1] is not None
    ma, mn = _merge_run(*a_runs, every), _merge_run(*n_runs, every)
    mb = _merge_run(*b_runs, every) if use_b else None
    if ma is a_runs[0] or mn is n_runs[0] or (use_b and mb is b_runs[0]):
        return None
    joined = items + fb_items
    ran_all = compare_one(t, joined, ma, mb, mn, use_self)
    done = set(ran_all.compared)
    keep = [len(items) + i in done for i in range(len(fb_items))]
    ma, mn = _merge_run(*a_runs, keep), _merge_run(*n_runs, keep)
    mb = _merge_run(*b_runs, keep) if use_b else None
    return compare_one(t, joined, ma, mb, mn, use_self), ran_all, keep, (ma, mb, mn)


def _reached_count(t: Target, a: dict | None, n: dict | None) -> int:
    """Changed statements the traced lines of these runs reached (the gate of compare_one, without comparing)."""
    total = 0
    for gate, executed, _ in gate_parts(t, a or {}, n or {}):
        got = examples.coverage(gate, executed)
        total += len(got[0]) if got else 0
    return total


def _spent(runs: list, key: str) -> tuple[float, int]:
    """(seconds the function's calls took in the slowest first-pass run, timeouts in the run with most)."""
    secs, outs = 0.0, 0
    for run in runs:
        got = (run or {}).get(key) or {}
        secs = max(secs, sum(s for s in got.get("seconds") or [] if s))
        outs = max(outs, sum(1 for o in got.get("results") or [] if o.get("outcome") in ("timeout", "skipped_slow")))
    return secs, outs


def _explain_unreached(r: Result, t: Target, run: dict | None) -> None:
    """Add to an untested reason what keeps each unreached changed statement from running (Task 6.8e)."""
    if not r.unreached_ranges or not r.reason.startswith(REACH_REASONS):
        return
    old = all(o for _, _, o in r.unreached_ranges)
    source, start = (t.source, t.source_start) if old else (t.new_source, t.new_source_start)
    ranges = [[s, e] for s, e, o in r.unreached_ranges if o == old]
    try:
        plans = feedback.diagnose(source, start, ranges, (run or {}).get("lines"))
    except Exception:  # noqa: BLE001 - the reason stays as it was
        plans = []
    r.unreached_chains = [p["text"] for p in plans]
    if r.unreached_chains:
        r.reason += "; " + "; ".join(r.unreached_chains)
    if r.feedback_tried:
        parts = [f"reached {r.feedback_reached} more of them"] if r.feedback_reached or not (r.feedback_raised or r.feedback_dropped) else []
        if r.feedback_raised:
            parts.append(f"ran {r.feedback_raised} more but both versions raised there, so "
                         f"{'it does' if r.feedback_raised == 1 else 'they do'} not count")
        if r.feedback_dropped:
            parts.append(f"ran {r.feedback_dropped} more only on inputs that were not compared (the old code disagreed with "
                         f"itself, or set-up failed), so {'it does' if r.feedback_dropped == 1 else 'they do'} not count")
        r.reason += f"; a second pass of {r.feedback_tried} inputs aimed at these conditions " + "; it ".join(parts)
    if r.feedback_note:
        r.reason += f"; {r.feedback_note}"


def second_pass(cfg, py: str, baseline_root: str, work_root: str, paths_old: list[str], paths_new: list[str],
                specs_old: list[dict], specs_n: list[dict], runnable: dict, syms: dict, results: dict,
                a: dict, b: dict | None, n: dict, use_self: bool, left_s: float, warm_names: list[str], parse_cache: dict) -> None:
    """Task 6.8e. For each edited function left untested because some changed statement never ran: work out what must
    hold for it to run (feedback.diagnose), build at most feedback.MAX_FEEDBACK_ITEMS inputs aimed at that, starting
    from the first-pass inputs that got furthest (feedback.build), and run them once more in all three runs, within
    what the check's limit and the function's own time limit left. The second pass's outcomes are appended to the first
    pass's and compared again by compare_one, so the status rules are unchanged: verified only when every changed
    statement was run. specs_old, specs_n and runnable are updated in place to the joined inputs. Every untested
    result whose statements were not all reached gets the conditions in its reason."""
    started = time.monotonic()
    build_deadline = started + FEEDBACK_BUILD_S
    planned: dict = {}  # key -> second-pass inputs
    for key, (t, items) in runnable.items():
        r = results.get(t.qualname)
        if r is None or not _wants_second_pass(t, r):
            continue
        old = all(o for _, _, o in r.unreached_ranges)
        run = (a if old else n).get(key) or {}
        source, start = (t.source, t.source_start) if old else (t.new_source, t.new_source_start)
        if left_s < FEEDBACK_MIN_S:
            r.feedback_note = f"no second pass: {max(left_s, 0):.1f} s of the check's limit were left"
            continue
        secs, outs = _spent([a, b, n], key)
        if FUNCTION_BUDGET_S - secs < FEEDBACK_MIN_FN_S:
            r.feedback_note = "no second pass: the first inputs used up the function's time"
            continue
        plans: list = []
        notes: list = []
        try:
            plans = feedback.diagnose(source, start, [[s, e] for s, e, o in r.unreached_ranges if o == old], run.get("lines"))
            got = _parsed(parse_cache, baseline_root if old else work_root, t.file)
            fb_items = feedback.build(syms.get(key) or {"params": t.params}, items, run.get("input_lines"), plans,
                                      got[0] if got else None, feedback.MAX_FEEDBACK_ITEMS, build_deadline, notes)
        except Exception:  # noqa: BLE001
            fb_items = []
        if notes:
            r.feedback_note = "; ".join(dict.fromkeys(notes))
        if not fb_items:
            r.feedback_note = ("no second pass: the unreached lines cannot run" if plans and all(p.get("dead") for p in plans)
                               else f"no second pass: {r.feedback_note}" if notes
                               else "no second pass: no input could be aimed at these conditions")
            continue
        planned[key] = (fb_items, secs, outs)
    # What the check's limit has left once the inputs are built: the extra round never runs past it.
    limit = left_s - (time.monotonic() - started)
    if planned and limit < FEEDBACK_RUN_MIN_S:
        for key, (fb_items, _, _) in planned.items():
            note = f"no second pass: {max(limit, 0):.1f} s of the check's limit were left to run its {len(fb_items)} inputs"
            r = results[runnable[key][0].qualname]
            r.feedback_note = f"{r.feedback_note}; {note}" if r.feedback_note else note
        planned = {}
    if planned:
        def second(specs: list[dict]) -> list[dict]:
            out = []
            for spec in specs:
                key = f"{spec['module']}:{spec['qualname']}"
                if key in planned:
                    fb_items, secs, outs = planned[key]
                    first = len(runnable[key][1])
                    out.append({**{k: v for k, v in spec.items() if k != "mutants"}, "inputs": fb_items,
                                "indexes": list(range(first, first + len(fb_items))),
                                "budget": max(FEEDBACK_MIN_FN_S, FUNCTION_BUDGET_S - secs), "timeouts_before": outs})
            return out
        s_old, s_new = second(specs_old), second(specs_n)
        with cf.ThreadPoolExecutor(3) as pool:
            fa = pool.submit(_run_worker, cfg, py, baseline_root, paths_old, s_old, limit, warm_names)
            fn = pool.submit(_run_worker, cfg, py, work_root, paths_new, s_new, limit, warm_names)
            fb = pool.submit(_run_worker, cfg, py, baseline_root, paths_old, s_old, limit, warm_names) if use_self else None
            a2, n2 = fa.result(), fn.result()
            b2 = fb.result() if fb else None
        failed = next((x[FAILED] for x in (a2, n2, b2) if x and FAILED in x), None) if a2 and n2 else "no result"
        elapsed = time.monotonic() - started
        by_old = {f"{s['module']}:{s['qualname']}": s for s in specs_old}
        by_new = {f"{s['module']}:{s['qualname']}": s for s in specs_n}
        for key, (fb_items, _, _) in planned.items():
            t, items = runnable[key]
            r1 = results[t.qualname]
            if failed:
                r1.feedback_note = f"a second pass of {len(fb_items)} inputs did not finish ({failed})"
                continue
            joined = items + fb_items
            got = _second_compare(t, items, fb_items, (a.get(key), a2.get(key)), (b.get(key) if b else None, b2.get(key) if b2 else None),
                                  (n.get(key), n2.get(key)), use_self)
            if got is None:
                r1.feedback_note = f"a second pass of {len(fb_items)} inputs could not be run the same way as the first"
                continue
            r2, ran_all, keep, (ma, mb, mn) = got
            r2.feedback_tried = len(fb_items)
            r2.feedback_reached = max(0, r2.statements_reached - r1.statements_reached)
            # Statements only dropped second-pass inputs ran: both versions raised (a guess neither accepts), or dropped
            # for another reason (old code disagreed with itself, set-up failed). Named apart in the reason.
            outs = [(x or {}).get("results") or [] for x in (a2.get(key), n2.get(key))]
            raised = [all(i < len(o) and o[i].get("outcome") == "raise" for o in outs) for i in range(len(fb_items))]
            with_raised = _reached_count(t, _merge_run(a.get(key), a2.get(key), [k or z for k, z in zip(keep, raised)]),
                                         _merge_run(n.get(key), n2.get(key), [k or z for k, z in zip(keep, raised)]))
            r2.feedback_raised = max(0, with_raised - r2.statements_reached)
            r2.feedback_dropped = max(0, ran_all.statements_reached - max(with_raised, r2.statements_reached))
            r2.feedback_note = r1.feedback_note
            r2.feedback_seconds = elapsed
            r2.feedback_only = bool(r2.differences) and all(d.get("source") == "feedback" for d in r2.differences)
            results[t.qualname] = r2
            runnable[key] = (t, joined)
            by_old[key]["inputs"] = joined
            by_new[key]["inputs"] = joined
            a[key], n[key] = ma, mn
            if mb is not None:
                b[key] = mb
    for key, (t, _) in runnable.items():
        r = results.get(t.qualname)
        if r is not None and not t.via and t.kind != "route" and r.status == UNTESTED:
            old = bool(r.unreached_ranges) and all(o for _, _, o in r.unreached_ranges)
            _explain_unreached(r, t, (a if old else n).get(key))


def run(cfg, baseline_root: str, work_root: str, targets: list[Target], harvest: dict | None = None) -> list[Result]:
    """Run old twice and new once, in parallel subprocesses, on the same inputs. Each tree gets one throwaway working
    folder for the whole check (Task 7.4), or the enclosing for_trees block's when the caller opened one."""
    with scratch.for_trees(cfg, baseline_root, work_root):
        return _run(cfg, baseline_root, work_root, targets, harvest)


def _run(cfg, baseline_root: str, work_root: str, targets: list[Target], harvest: dict | None = None) -> list[Result]:
    check_started = time.monotonic()
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
    syms: dict = {}   # key -> the signature its inputs were built for (Task 6.8e builds second-pass inputs for it)
    recorded_s = 0.0
    for t in targets[:MAX_FUNCTIONS]:
        if t.kind == "route":
            app = cfg.raw.get("app") or ""
            if not t.requests or not app:
                results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason="no requests could be built for this route")
                continue
            app_module = app.split(":", 1)[0]
            specs.append({"module": app_module, "qualname": t.qualname, "kind": "http", "app": app, "inputs": t.requests,
                          **({"handler": t.handler} if t.handler else {})})
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
        self_types = _self_types(t, harvest)
        if self_types:
            spec["self_types"] = self_types
        specs.append(spec)
        runnable[f"{module}:{t.qualname}"] = (t, items)
        syms[f"{module}:{t.qualname}"] = sym
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
        specs_old = _with_input_lines(_tree_specs(specs, runnable, old=True), runnable)
        specs_a = _with_mutants(specs_old, runnable, int(bd.get("mutants", 8)), baseline_root)
        specs_n = _with_input_lines(_tree_specs(specs, runnable, old=False), runnable)
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
            # Task 6.8e: a bounded second pass aimed at the changed statements no input ran, within the check's limit.
            # Everything so far counts: building inputs, recording callers' calls, and the three runs.
            left = float(bd["timeout_s"]) - (time.monotonic() - check_started)
            try:
                second_pass(cfg, py, baseline_root, work_root, paths_for(baseline_root), paths_for(work_root), specs_old, specs_n,
                            runnable, syms, results, a, b, n, use_self, left, warm_names, parse_cache)
            except Exception:  # noqa: BLE001 - a second pass that cannot run leaves the first results as they were
                pass
            confirm_slowdowns(cfg, py, baseline_root, work_root, paths_for(baseline_root), paths_for(work_root), specs_old, specs_n, runnable, results)
    for t in targets:
        results[t.qualname].via, results[t.qualname].via_file = t.via, t.via_file
        results[t.qualname].callees = list(t.callees)
        if t.route_note and t.route_note not in results[t.qualname].reason:
            r = results[t.qualname]
            r.reason = f"{r.reason}; {t.route_note}" if r.reason else t.route_note
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
# so a candidate needs the old runs to have finished (returned or raised) within a small share of the full limit, and is reported only after
# each version is re-run alone on that input with the full limit: the new code must time out again and the old code
# must still be fast. A reduced limit in the first run never decides anything by itself; the re-run does.
CALL_TIMEOUT_S = 5            # matches lg_worker.CALL_TIMEOUT_S, the full per-call limit
SLOWDOWN_OLD_SHARE = 0.1      # the old runs finished within this share of the full limit
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
        out.append({"index": i, "reduced": limits[i] < CALL_TIMEOUT_S, "old_seconds": max(old), "old_runs": len(old), "source": item.get("source") or ""})
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
                                    "old_seconds": max(old_s, c["old_seconds"]), "source": c["source"], "old_runs": c.get("old_runs", 1)})
                if r.status not in (DIFFERENT, BROKEN):
                    r.status = SLOWDOWN
    except Exception:  # noqa: BLE001 - a slowdown check that cannot run must never change or hide the diff's result
        return


def slowdown_text(s: dict) -> str:
    return (f"the new code ran past {s['new_limit_s']:g} s on {s['call']} while the old code finished in {max(s['old_seconds'], 0.001):.3f} s; "
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
            reach = (f"; {reached_text(r)}" if r.route_deleted else f"; the inputs {reached_text(r)}") if r.statements_total else ""
            if r.feedback_reached:
                reach += (f", {r.feedback_reached} of them only by {r.feedback_tried} inputs from a second pass aimed at the "
                          "conditions around lines the first inputs missed")
            carved = carved_text(r)
            lines.append(f"LegacyGuard ran {r.subject} before and after this edit: no difference on {r.inputs_compared} inputs "
                         f"({r.inputs_completed} returned a value{reach}). " + (f"{carved}. " if carved else "") + (f"{conf}. " if conf else "") + io +
                         (f"{placeholder_text(r)[:1].upper()}{placeholder_text(r)[1:]}. " if r.placeholder_inputs else "") +
                         "The inputs are the repo's own examples plus guesses, so this is not a proof; values it did not try can still differ.")
        elif r.status == UNTESTED:
            carved = carved_text(r)
            lines.append(f"LegacyGuard could not check {r.subject} by running it: {r.reason}. " + (f"{carved}. " if carved else "") +
                         "Nothing is known about this edit from execution.")
        elif r.status == SLOWDOWN and r.slowdowns:
            extra = len(r.slowdowns) - 1
            more = f" ({extra} more such input{'s' if extra > 1 else ''})" if extra else ""
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
    if any(d.get("source") == "feedback" for d in result.differences[:MAX_SHOWN]):
        lines.append("Inputs marked [feedback] were built in a second pass aimed at the conditions guarding changed lines the first "
                     "inputs never ran; they are guesses, so values the function would never get in real use are possible.")
    if result.feedback_only:
        lines.append(FEEDBACK_ONLY_TEXT + ".")
    if result.only_special:
        lines.append("Every differing input contains NaN or infinity. On the ordinary values tried, old and new code agree.")
    if result.only_float_noise:
        lines.append("Every difference found is in the last digits of a float (the two results agree to nine digits). That can be plain float "
                     "arithmetic, or the visible edge of a rounding change: a value on a rounding boundary can still come out a whole step different.")
    if result.unreached_note:
        lines.append(f"{result.unreached_note}.")
    if result.placeholder_note:
        lines.append(f"{result.placeholder_note}.")
    if result.clock_note:
        lines.append(f"{result.clock_note}.")
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
        lines.append(f"Also on {s['call']}: the new code ran past {s['new_limit_s']:g} s, the old code finished in {max(s['old_seconds'], 0.001):.3f} s.")
    # With self-diff off there was one old run in the first pass, not two.
    runs = "Both runs of the code before the edit" if first.get("old_runs", 1) >= 2 else "The run of the code before the edit"
    lines.append(f"{runs} finished on this input within {SLOWDOWN_OLD_SHARE:.0%} of the {CALL_TIMEOUT_S} s per-call "
                 "limit; each version was then re-run alone on it with the full limit, and the new code timed out again while the old code "
                 "finished again. Timing depends on the machine. This is not counted as a behavior difference and does not block the edit.")
    if result.reason:
        lines.append(f"Also: {result.reason}.")
    lines.append("Common causes are a regular expression that backtracks (nested or overlapping quantifiers) and a loop that no longer "
                 "ends early. If the slowdown is not intended, revising the code clears this and the check re-runs by itself; "
                 "if it is expected, nothing needs to be done (this is reported once per version of the code).")
    return "\n".join(lines)
