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
    via: str = ""                                  # not edited itself: "calls states_in_range" (FR-41)
    via_file: str = ""                             # the file whose edit made this function worth running
    source_start: int = 0                          # first line of `source` in the baseline file


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
    mutants_total: int = 0          # planted bugs run after a pass (roadmap B6)
    mutants_killed: int = 0
    mutants_survived: list = field(default_factory=list)
    via: str = ""
    via_file: str = ""

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
            old = c.before
            old_gate, new_gate = examples.gates(before, after, c.before, c.after)
            out[c.qualname] = Target(rel, c.qualname, c.after.kind, old.params, symbols.sha(c.after.node_text),
                                     old_gate=old_gate, new_gate=new_gate, source=old.node_text, source_start=old.line_start)
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
    return out


def _show_outcome(o: dict, other: dict | None = None) -> str:
    """The outcome as people read it. State is shown only for names whose state differs from the other version."""
    text = lg_codec.display(o["v"], plain_decimal=True) if o["k"] == "return" else f"raises {o['type']}: {o['message']}"
    mine, theirs = o.get("s") or {}, (other or {}).get("s") or {}
    changed = [n for n in sorted(set(mine) | set(theirs)) if _key(mine.get(n) or {}) != _key(theirs.get(n) or {})]
    if changed:
        parts = [f"{n}={lg_codec.display(mine[n], plain_decimal=True)}" if n in mine else f"{n} not readable" for n in changed]
        text += ", then " + ", ".join(parts)
    return text


def show_call(target: Target, item: dict) -> str:
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
    blocked: set[str] = set()
    setup_failures = 0
    setup_detail = ""
    for item, oa, on, ob in zip(items, a["results"], n["results"], (b or {"results": a["results"]})["results"]):
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
        if ca["k"] == "return":
            r.inputs_completed += 1
        if _key(ca) != _key(cn):
            same_state = _key(ca.get("s") or {}) == _key(cn.get("s") or {})
            r.differences.append({"call": show_call(target, item), "before": _show_outcome(ca, cn), "after": _show_outcome(cn, ca), "source": item["source"],
                                  "args": item["args"], "has_self": item.get("has_self", False), "before_raw": ca, "after_raw": cn,
                                  "special": _has_special(item), "state": not same_state,
                                  "noise": same_state and ca["k"] == "return" and cn["k"] == "return" and _float_noise(ca["v"], cn["v"])})
    if r.differences:
        r.status = DIFFERENT
        # The example a person reads first: from real tests, then the repo's own examples, ordinary values before
        # NaN and infinity, a returned value before an error, and short before long.
        r.differences.sort(key=lambda d: (d["noise"], d["source"] != "tests", d["source"] != "examples", d["special"],
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
        return r
    # A pass must rest on inputs that ran the changed code (D-043). Guessed values for an untyped parameter often
    # leave by an early return or an error, and old and new code then agree without the edit ever being exercised.
    hit = examples.reached(target.new_gate, n.get("lines")) if target.new_gate else examples.reached(target.old_gate, a.get("lines"))
    if hit is False:
        r.reason = f"none of the {r.inputs_compared} inputs tried reached the changed lines, so the edit itself was not exercised"
        return r
    r.status = DONE
    m = a.get("mutation") or {}
    if m.get("applicable"):
        r.mutants_total, r.mutants_killed, r.mutants_survived = m["total"], m["killed"], m.get("survived", [])
    return r


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


def _examples(cfg, conn, t: Target) -> dict | None:
    """Example values and calls from the function's own text and from its callers, tests first (FR-36)."""
    if not t.source:
        return None
    name = t.qualname.rsplit(".", 1)[-1]
    params = [p["name"] for p in t.params if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]
    try:
        files = examples.calling_files(conn, name, t.file) or [f for f in examples.files_naming(cfg.root, name) if f != t.file]
        return examples.collect(cfg.root, t.source, name, params, files)
    except Exception:  # noqa: BLE001
        return None


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


def _with_mutants(specs: list[dict], runnable: dict, limit: int) -> list[dict]:
    """The specs for the first baseline run, with planted bugs for each edited function (roadmap B6). Only that run
    gets them: the mutants are compared with the old function's own results, so the other runs stay as fast as before."""
    if limit <= 0:
        return specs
    out = []
    for spec in specs:
        t, _ = runnable[f"{spec['module']}:{spec['qualname']}"]
        plan = mutants.generate(t.source, t.source_start, t.old_gate, limit) if t.source and t.source_start and not t.via else None
        out.append({**spec, "mutants": plan} if plan else spec)
    return out


def run(cfg, baseline_root: str, work_root: str, targets: list[Target], harvest: dict | None = None) -> list[Result]:
    """Run old twice and new once, in parallel subprocesses, on the same inputs."""
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
    for t in targets[:MAX_FUNCTIONS]:
        if t.file not in alias_cache:
            try:
                alias_cache[t.file] = _aliases((Path(work_root) / t.file).read_text(errors="replace"))
            except OSError:
                alias_cache[t.file] = {}
        params = _resolve_aliases(t.params, alias_cache[t.file])
        items, reason = input_lib.build({"qualname": t.qualname, "kind": t.kind, "params": params}, harvest.get(f"{t.file}::{t.qualname}"), int(bd["max_inputs"]), cfg.state_dir,
                                        examples=_examples(cfg, conn, t))
        if reason:
            results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason=reason)
            continue
        if not items:
            results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason="no inputs could be built for its signature")
            continue
        module, sys_rel = module_for(t.file, work_root)
        if t.kind == "method" and 0 < len(items) < SELF_VARIANTS:
            # Each input runs on a differently built instance, so a method with few inputs is repeated to see each one.
            items = [dict(item) for item in (items * SELF_VARIANTS)[:SELF_VARIANTS]]
        spec = {"module": module, "qualname": t.qualname, "inputs": items}
        self_calls = _self_calls(cfg, conn, t, work_root)
        if self_calls:
            spec["self_calls"] = self_calls
        specs.append(spec)
        runnable[f"{module}:{t.qualname}"] = (t, items)
        roots.add(sys_rel)
    for t in targets[MAX_FUNCTIONS:]:
        results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, reason=f"more than {MAX_FUNCTIONS} functions changed at once")
    if specs:
        def paths_for(base: str) -> list[str]:
            ordered = list(dict.fromkeys([*extra, *sorted(roots), ""]))
            return [str(Path(base) / p) if p else base for p in ordered]
        deadline = float(bd["timeout_s"])
        use_self = bool(bd.get("self_diff", True))
        warm_names = warm.third_party_imports(work_root, sorted({t.file for t, _ in runnable.values()}), extra)
        specs_a = _with_mutants(specs, runnable, int(bd.get("mutants", 8)))
        with cf.ThreadPoolExecutor(3) as pool:
            fa = pool.submit(_run_worker, cfg, py, baseline_root, paths_for(baseline_root), specs_a, deadline, warm_names)
            fn = pool.submit(_run_worker, cfg, py, work_root, paths_for(work_root), specs, deadline, warm_names)
            fb = pool.submit(_run_worker, cfg, py, baseline_root, paths_for(baseline_root), specs, deadline, warm_names) if use_self else None
            a, n = fa.result(), fn.result()
            b = fb.result() if fb else None
        failed = next((x[FAILED] for x in (a, n, b) if x and FAILED in x), None)
        setup_note = next((x["__setup_error__"] for x in (n, a) if x and "__setup_error__" in x), None)
        for key, (t, items) in runnable.items():
            if failed:
                results[t.qualname] = Result(t.file, t.qualname, UNTESTED, t.code_hash, inputs_tried=len(items), reason=failed)
                continue
            results[t.qualname] = compare_one(t, items, a.get(key) if a else None, b.get(key) if b else None, n.get(key) if n else None, use_self)
            if setup_note and results[t.qualname].status == UNTESTED:
                results[t.qualname].reason += f" (the project's Django settings did not load: {setup_note})"
    for t in targets:
        results[t.qualname].via, results[t.qualname].via_file = t.via, t.via_file
    return [results[t.qualname] for t in targets]


def format_note(results: list[Result]) -> str:
    """One factual line per function that was checked without finding a difference, or could not be checked."""
    lines = []
    own = [r for r in results if not r.via]
    callers = [r for r in results if r.via and r.status == DONE]
    for r in own[:4]:
        if r.status == DONE:
            conf = confidence_text(r)
            lines.append(f"LegacyGuard ran {r.subject} before and after this edit: no difference on {r.inputs_compared} inputs "
                         f"({r.inputs_completed} returned a value). " + (f"{conf}. " if conf else "") +
                         "The inputs are the repo's own examples plus guesses, so this is not a proof; values it did not try can still differ.")
        elif r.status == UNTESTED:
            lines.append(f"LegacyGuard could not check {r.subject} by running it: {r.reason}. Nothing is known about this edit from execution.")
    if len(own) > 4:
        lines.append(f"+{len(own) - 4} more functions; /legacyguard:status lists them.")
    if callers:
        names = ", ".join(c.qualname for c in callers[:5])
        lines.append(f"{len(callers)} function{'s that call' if len(callers) != 1 else ' that calls'} the edited code {'were' if len(callers) != 1 else 'was'} also run "
                     f"({names}): no difference on the inputs tried. Callers that could not be run are not listed.")
    return "\n".join(lines)


def format_wake(result: Result, finding_id: str) -> str:
    """The counterexample text Claude receives (FR-15). Factual, replayable, within the context limit."""
    total = result.inputs_compared
    lines = [f"LegacyGuard behavior difference [{finding_id}] in {result.subject}: {len(result.differences)} of {total} inputs differ between the session baseline and the current code."]
    if result.via:
        lines.append(f"This function was not edited. It {result.via}, which was edited in {result.via_file}.")
    for d in result.differences[:MAX_SHOWN]:
        lines.append(f"{d['call']}  before {d['before']}  after {d['after']}  [{d['source']}]")
    if result.only_special:
        lines.append("Every differing input contains NaN or infinity. On the ordinary values tried, old and new code agree.")
    if result.only_float_noise:
        lines.append("Every difference found is in the last digits of a float (the two results agree to nine digits). That can be plain float "
                     "arithmetic, or the visible edge of a rounding change: a value on a rounding boundary can still come out a whole step different.")
    if result.reason:
        lines.append(f"Also: {result.reason}.")
    lines.append(f"Whether the request intended this is unknown. If it did not, revising the code clears this and the check re-runs by itself. "
                 f"If it did, say so in your reply; the developer can accept it with /legacyguard:waive {finding_id} \"<reason>\".")
    return "\n".join(lines)
