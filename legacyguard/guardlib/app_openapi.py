"""Code-first OpenAPI (Sprint 7.6 Part B): the spec the app generates, baseline against the edited code.

When the config names an `app:` whose object has a callable `openapi()` (FastAPI), the spec is built in the baseline
and in the working tree, each in its own target-interpreter process (`runner/lg_openapi.py`) under the worker's set-up
(blocks, stubs, determinism, `setup_phase()`, `load_app`), the OS sandbox and a throwaway working folder, with
`PYTHONHASHSEED=0`. It runs project code, so never in PreToolUse: PostToolUse wakes Claude on a new ERR change, the
Stop audit writes `contract_break` findings (source `app-openapi`, `contract_kind: "http"`), and `ci.run` reports
breaks.

Comparison: canonical JSON text per tree; byte-equal texts end the check with no tool run. Otherwise
`contract_tools.oasdiff_breaking` on the two texts, internal refs only (`--allow-external-refs=false`); ERR changes are
breaks, WARN changes a note. Without a usable oasdiff the built-in parser compares the two dicts, fields only for schema
names present on both sides, and says which schemas it did not compare.

Noise: the baseline is built twice (once per session, cached in `.legacyguard/app_openapi_base.json`; session.json
`app_openapi` holds only a summary and the key and shas that prove the file current). When the two baseline texts
differ, oasdiff also runs old-A vs old-B once and its change list is cached. An old-vs-new break that old-vs-old also
reports is dropped; one that only shares (operation, path) with an old-vs-old WARN/ERR change is hedged; every other
break stands. A hedged break never wakes and is a non-blocking RISK finding at Stop.

A baseline that cannot be fully built is not cached; the failed attempt is remembered under the cache key, and after
MAX_BASE_ATTEMPTS checks the check pauses until the key changes. When both attempts built one identical text from one
of the two builds, that text is cached with `stable: None`, and then every later break of the session is hedged ("built
once"): a non-blocking RISK finding at Stop, never a wake.

Every public function returns an outcome or a safe value and never raises. "Not checked" is reported, never hidden.

Known limits:
- FastAPI-shaped `app.openapi()` only; Flask extensions, django-ninja, DRF and Starlette's docstring schemas are not read.
- The trigger set is the files the app imported when its spec was built (baseline, last working-tree run) plus the app
  module; a module imported only inside a function is not in it. The first guarded edit of a session always runs.
- The working tree is built once per check, so a nondeterministic working tree with a stable baseline is not detected.
- The parser fallback compares no fields of a model pydantic renamed (a second model of the same name, `-Input` and
  `-Output`); it names those schemas in a note. Waiver items differ between oasdiff and the parser.
- When the noise list came from the parser but old-vs-new from oasdiff (or the reverse), only the (operation, path)
  hedge applies across them; parser field items have no operation, so they match only oasdiff changes without one.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from guardlib import config as cfgmod, symbols
from guardlib.checks import Break

RUNNER = Path(__file__).resolve().parent.parent / "runner"
SCRIPT = RUNNER / "lg_openapi.py"
MARK = "LGOAS"
SOURCE = "app-openapi"
REL = "openapi-from-app.json"
LABEL = "the app's generated spec"
CACHE_FILE = "app_openapi_base.json"
SPEC_BUDGET_S = 45
MIN_STAGE_S = 1.0
MAX_BASE_ATTEMPTS = 2
MAX_LISTED = 6
BUDGET = "the time budget for this check ran out"
HEDGE_ONCE = "the baseline spec was built only once, so it could not be checked for changes between builds"
HEDGE_NOISE = "the baseline's generated spec also changes between two builds at {where}, so this may not come from the edit"
PAUSED_NOTE = ("LegacyGuard: the app's OpenAPI spec could not be built in the baseline in {n} checks ({detail}), so the "
               "code-first schema check is paused for this session. It restarts when the baseline, the `app` setting, the "
               "Python interpreter or the worker settings change.")


@dataclass
class SpecBreak(Break):
    change: tuple = ()   # (id, operation, path, text) of an oasdiff change; () for a parser break
    raw: str = ""        # the parser's item, without the app-oas-parser: prefix
    hedge: str = ""

    def dedupe_key(self):
        """(oasdiff id, subject): equal for the same change found in a static spec file (checks._oasdiff_breaks)."""
        if not self.change:
            return None
        return (self.change[0] or "change", self.subject)


@dataclass
class SpecOutcome:
    state: str                                     # checked | not_checked | hedged | none
    breaks: list = field(default_factory=list)     # (file, SpecBreak) standing
    hedged: list = field(default_factory=list)     # (file, SpecBreak) with .hedge
    warns: list = field(default_factory=list)      # WARN change texts
    notes: list = field(default_factory=list)      # (tool_notes kind, text): once per session
    via: str | None = None                         # oasdiff | parser
    parser_why: str = ""                           # refusal kind when the parser stood in
    key: str | None = None
    cache_write: dict | None = None                # contents of app_openapi_base.json to write
    summary: dict | None = None                    # session.json app_openapi
    failed: dict | None = None                     # {detail, one_sha}: a baseline that was not fully built
    paused: bool = False
    runs: int = 0
    versions: dict = field(default_factory=dict)
    kind: str | None = None
    app: str = ""
    seconds: float = 0.0

    @property
    def note(self) -> str | None:
        return self.notes[0][1] if self.notes else None


# ---- config and trigger ----

def enabled(cfg) -> bool:
    try:
        from guardlib import contract_tools
        value = contract_tools.settings(cfg).get("openapi_from_app", "auto")
        if value is False or str(value).lower() in ("false", "off", "no"):
            return False
        return bool(cfg.raw.get("app"))
    except Exception:  # noqa: BLE001
        return False


def cache_key(cfg, data: dict | None) -> str:
    from runner import warm
    py = cfgmod.target_python(cfg) or ""
    real = os.path.realpath(py) if py else ""
    try:
        mtime = os.stat(real).st_mtime_ns if real else None
    except OSError:
        mtime = None
    bd = cfg.raw.get("behavioral_diff") or {}
    return json.dumps({"commit": (data or {}).get("baseline_commit"), "generation": warm._generation(cfg),
                       "app": cfg.raw.get("app"), "python": real, "python_mtime": mtime, "io": bd.get("io", "stub"),
                       "source_roots": cfg.raw.get("source_roots") or [], "env": bd.get("env") or {}}, sort_keys=True)


def is_paused(cfg, state: dict | None, data: dict | None) -> bool:
    try:
        return bool(state) and state.get("key") == cache_key(cfg, data) and bool((state.get("failed") or {}).get("paused"))
    except Exception:  # noqa: BLE001
        return False


def applies(cfg, rel: str | None, state: dict | None, data: dict | None) -> bool:
    """Whether an edit to rel runs the check (D6). state: session.json app_openapi; data: {"baseline_commit": ...}."""
    try:
        from guardlib import engine
        if not enabled(cfg) or not engine.is_guarded_source(cfg, rel):
            return False
        if not state or state.get("key") != cache_key(cfg, data):
            return True  # nothing cached under this key: this run builds the cache
        failed = state.get("failed")
        if failed:
            return not failed.get("paused")  # D10: paused until the key changes; one failed attempt: try again
        if state.get("kind") == "none":
            return False
        files = set(state.get("base_files") or []) | set(state.get("work_files") or [])
        return rel in files or rel == state.get("app_file")
    except Exception:  # noqa: BLE001
        return False


# ---- one tree ----

def run_tree(cfg, tree: str, timeout: float) -> dict | None:
    """The lg_openapi result for one tree; `error` set when the run could not finish; None on an internal failure."""
    try:
        from indexer.coverage_map import clean_env
        from runner import os_sandbox, scratch
        bd = cfg.raw.get("behavioral_diff") or {}
        py = cfgmod.target_python(cfg)
        if not py:
            return {"error": "python", "detail": "no Python found for the project"}
        tree = str(tree)
        timeout = max(0.1, float(timeout))
        roots = [str(Path(tree) / r) for r in (cfg.raw.get("source_roots") or [])] + [tree]
        with scratch.current(cfg, tree) as cwd, tempfile.TemporaryDirectory(prefix="lgoas") as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps({"root": tree, "sys_path": roots, "protect": [str(cfg.root), tree], "cwd": cwd.path,
                                       "cwd_root": cwd.folder or cwd.tree or cwd.path, "scratch": str(Path(tmp) / "scratch"),
                                       "app": cfg.raw.get("app"), "io": str(bd.get("io", "stub")), "deterministic": True}))
            env = clean_env(cfg)
            env.pop("PYTHONPATH", None)
            writable = [tempfile.gettempdir(), tmp] + ([cwd.path] if cwd.strategy != "off" else [])
            cmd = os_sandbox.wrap([py, "-B", str(SCRIPT), str(req)], py, writable=writable,
                                  readonly=[str(cfg.root), tree], mode=str(bd.get("os_sandbox", "auto")))
            try:
                done = os_sandbox.run(cmd, timeout, cwd=cwd.path, env=env, capture_output=True, text=True)
            except subprocess.TimeoutExpired:
                return {"error": "timeout", "detail": f"timed out after {timeout:g} s"}
            except OSError as exc:
                return {"error": "start", "detail": f"could not start {py}: {exc}"}
        for line in (done.stdout or "").splitlines():
            if line.startswith(MARK):
                try:
                    out = json.loads(line[len(MARK):])
                except ValueError:
                    break
                return out if isinstance(out, dict) else {"error": "output", "detail": "unreadable output"}
        last = (done.stderr or "").strip().splitlines()[-1:] or [f"exit code {done.returncode}"]
        return {"error": "output", "detail": f"no result: {last[0][:200]}"}
    except Exception:  # noqa: BLE001
        return None


def built(r: dict | None) -> bool:
    """The run finished: no error, nothing blocked (kind may be "none")."""
    return bool(r) and r.get("error") is None and not r.get("blocks")


def complete(r: dict | None) -> bool:
    return built(r) and r.get("kind") not in (None, "none") and isinstance(r.get("text"), str)


def _failure(r: dict | None) -> tuple[str, str]:
    """(error kind, detail) for a run that is not complete."""
    if r is None:
        return "internal", "internal error in LegacyGuard"
    if r.get("blocks"):
        return "blocked", f"{', '.join(sorted(set(r['blocks'])))[:200]} was refused while it ran"
    err = str(r.get("error") or "")
    detail = str(r.get("detail") or "")
    if err == "import":
        return "import", f"the app did not import: {detail[:200]}"
    if err == "encode":
        return "encode", f"the spec could not be written as JSON (encode: {detail[:200]})"
    if err == "openapi":
        return "openapi", f"app.openapi() raised {detail[:200]}"
    if err == "budget":
        return "budget", BUDGET
    if r.get("kind") == "none" and not err:
        return "none", "the app has no openapi() method"
    return (err or "error"), (detail or err or "unknown error")[:200]


def _rel_files(paths: list | None, tree: str) -> list[str]:
    root = os.path.realpath(tree).rstrip(os.sep) + os.sep
    out = []
    for p in paths or []:
        real = os.path.realpath(str(p))
        if real.startswith(root):
            out.append(real[len(root):].replace(os.sep, "/"))
    return sorted(set(out))


def _entry(r: dict, tree: str) -> dict:
    return {"text": r.get("text"), "sha": r.get("sha"), "files": _rel_files(r.get("files"), tree),
            "app_file": (_rel_files([r["app_file"]], tree) or [None])[0] if r.get("app_file") else None,
            "versions": r.get("versions") or {}, "kind": r.get("kind")}


# ---- the baseline cache (M3) ----

def cache_path(cfg) -> Path:
    return Path(cfg.state_dir) / CACHE_FILE


def load_cache(cfg, state: dict | None, key: str) -> dict | None:
    """The cached baseline when session.json's summary proves the file current; None means "not cached"."""
    try:
        if not state or state.get("key") != key or state.get("failed"):
            return None
        data = json.loads(cache_path(cfg).read_text())
        if not isinstance(data, dict) or data.get("key") != key or not isinstance(data.get("a"), dict):
            return None
        b = data.get("b")
        if data["a"].get("sha") != state.get("sha_a") or ((b or {}).get("sha") if b else None) != state.get("sha_b"):
            return None
        if data.get("stable") != state.get("stable") or data["a"].get("kind") != state.get("kind"):
            return None
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_cache(cfg, outcome: SpecOutcome | None) -> None:
    """Atomic write of the baseline cache, outside the ledger lock. Best effort."""
    try:
        if outcome is None or not outcome.cache_write:
            return
        path = cache_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{CACHE_FILE}.{os.getpid()}.{id(outcome)}.tmp")
        tmp.write_text(json.dumps(outcome.cache_write))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def drop_cache(cfg) -> None:
    try:
        cache_path(cfg).unlink()
    except OSError:
        pass


# ---- comparing ----

def _parallel(cfg, jobs: dict, timeout_s: float, deadline: float) -> dict:
    """Run run_tree for each {name: tree} at once, never waiting past the deadline."""
    ex = cf.ThreadPoolExecutor(max_workers=len(jobs))
    try:
        each = max(0.1, min(timeout_s, deadline - time.time()))
        futs = {name: ex.submit(run_tree, cfg, tree, each) for name, tree in jobs.items()}
        done, _ = cf.wait(list(futs.values()), timeout=max(0.0, deadline - time.time()) + 0.5)
    finally:
        ex.shutdown(wait=False)
    out = {}
    for name, f in futs.items():
        if f not in done:
            out[name] = {"error": "budget", "detail": BUDGET}
            continue
        try:
            out[name] = f.result()
        except Exception:  # noqa: BLE001
            out[name] = None
    return out


def _where_of_item(raw: str) -> tuple[str, str]:
    """(operation, path) of a parser item: route:METHOD /p, param:METHOD /p:name; fields have none."""
    label, _, rest = raw.partition(":")
    if label in ("route", "param"):
        method, _, tail = rest.partition(" ")
        path = tail.rsplit(":", 1)[0] if label == "param" else tail
        return method, path.split("|", 1)[0]
    return "", ""


def _where(b: SpecBreak) -> tuple[str, str]:
    return (b.change[1], b.change[2]) if b.change else _where_of_item(b.raw)


def _parser(a_spec: dict, b_spec: dict) -> tuple[list, list, list]:
    """(breaks as (raw item, subject), old-only schema names, new-only schema names) from the built-in parser,
    comparing fields only for schema names present in both specs (D7)."""
    from guardlib import checks, contracts_parse as cp
    a, b = cp.http_items_from(a_spec), cp.http_items_from(b_spec)
    names_a = set(((a_spec.get("components") or {}).get("schemas") or {}))
    names_b = set(((b_spec.get("components") or {}).get("schemas") or {}))
    both = names_a & names_b

    def keep(items):
        return {k: v for k, v in items.items() if not k.startswith("field:") or k[len("field:"):].split(".", 1)[0] in both}
    found = [(br.item, br.subject) for br in checks._http_breaks(REL, keep(a), keep(b))]
    return found, sorted(names_a - names_b), sorted(names_b - names_a)


def _changes(cfg, ta: str, tb: str, timeout: float):
    """("oasdiff", errs, warns) | ("parser", parser breaks, renamed (old, new), refusal) | ("timeout", reason)."""
    from guardlib import contract_tools
    a_spec, b_spec = json.loads(ta), json.loads(tb)
    tmp = tempfile.gettempdir()
    res = contract_tools.oasdiff_breaking(cfg, REL, ta, tb, (tmp, tmp), timeout, specs=(a_spec, b_spec),
                                          internal_only=True, label=LABEL)
    if isinstance(res, contract_tools.OasResult):
        return ("oasdiff", res.errs, res.warns)
    found, old_only, new_only = _parser(a_spec, b_spec)
    return ("parser", found, (old_only, new_only), res)


def _noise(cfg, ta: str, tb: str, timeout: float) -> dict | None:
    """The old-vs-old change list, or None when it could not be computed in time."""
    got = _changes(cfg, ta, tb, timeout)
    if got[0] == "oasdiff":
        changes = [{"id": c.get("id", ""), "operation": c.get("operation", ""), "path": c.get("path", ""),
                    "text": c.get("text", ""), "level": lvl, "item": ""}
                   for lvl, group in ((3, got[1]), (2, got[2])) for c in group]
        return {"changes": changes, "via": "oasdiff"}
    refusal = got[3]
    if getattr(refusal, "kind", "") == "timeout":
        return None
    changes = []
    for raw, subject in got[1]:
        op, path = _where_of_item(raw)
        changes.append({"id": "", "operation": op, "path": path, "text": subject, "level": 3, "item": raw})
    return {"changes": changes, "via": "parser"}


def _filter(breaks: list, noise: dict | None, stable) -> tuple[list, list]:
    """(standing, hedged) after the old-vs-old filter (D4, round-1 correction 1)."""
    standing, hedged = [], []
    entries = (noise or {}).get("changes") or []
    for file, b in breaks:
        if stable is None:
            b.hedge = HEDGE_ONCE
            hedged.append((file, b))
            continue
        if entries:
            if b.change and any((e.get("id"), e.get("operation"), e.get("path"), e.get("text")) == b.change for e in entries):
                continue
            if b.raw and any(e.get("item") == b.raw for e in entries):
                continue
            op, path = _where(b)
            if any(int(e.get("level") or 0) >= 2 and (e.get("operation") or "", e.get("path") or "") == (op, path)
                   for e in entries):
                where = f"{op} {path}".strip() or "the spec's components"
                b.hedge = HEDGE_NOISE.format(where=where)
                hedged.append((file, b))
                continue
        standing.append((file, b))
    return standing, hedged


def _note(where: str, kind: str, detail: str) -> tuple[str, str]:
    tree = "the baseline" if where == "base" else "the edited code"
    return (f"app-openapi-{where}-{kind}",
            f"LegacyGuard: the app's OpenAPI spec could not be built in {tree} ({detail}); schema changes were not checked.")


def _budget(out: SpecOutcome) -> SpecOutcome:
    out.state = "not_checked"
    out.notes.append(("app-openapi-budget", f"LegacyGuard: {BUDGET} before the app's OpenAPI specs were compared; "
                                             "schema changes were not checked."))
    return out


def compute(cfg, base_root: str, work_root: str, state: dict | None = None, data: dict | None = None,
            deadline: float | None = None, use_cache: bool = True) -> SpecOutcome:
    """Breaks in the app's generated spec, baseline against the edited code. state: session.json app_openapi (read
    under the ledger lock by the caller); data: {"baseline_commit": ...}; deadline: absolute time for the whole stage.
    use_cache False (ci.run): build both baseline runs every time, write nothing."""
    started = time.time()
    out = SpecOutcome("not_checked", app=str(cfg.raw.get("app") or ""))
    try:
        from guardlib import contract_tools
        deadline = deadline if deadline is not None else started + SPEC_BUDGET_S
        timeout_s = float(contract_tools.settings(cfg)["timeout_s"])

        def left() -> float:
            return deadline - time.time()

        key = cache_key(cfg, data)
        out.key = key
        base = load_cache(cfg, state, key) if use_cache else None
        work = None
        if base is not None:
            if left() < MIN_STAGE_S:
                return _budget(out)
            work = run_tree(cfg, work_root, min(timeout_s, left()))
            out.runs += 1
            if built(work) and (work.get("versions") or {}) != (base["a"].get("versions") or {}):
                base = None  # another FastAPI or pydantic than the cached baseline was built with: rebuild it
        if base is None:
            if left() < MIN_STAGE_S:
                out.failed = {"detail": BUDGET, "one_sha": None}
                return _budget(out)
            jobs = {"a": base_root, "b": base_root}
            if work is None:
                jobs["w"] = work_root
            res = _parallel(cfg, jobs, timeout_s, deadline)
            out.runs += len(jobs)
            a, b = res["a"], res["b"]
            work = res.get("w", work)
            if complete(a) != complete(b) and left() >= MIN_STAGE_S:  # a half-built baseline is retried once
                again = run_tree(cfg, base_root, min(timeout_s, left()))
                out.runs += 1
                if complete(a):
                    b = again
                else:
                    a = again
            base = _baseline(out, a, b, state, key, base_root)
            if base is None:
                return out
        out.kind = base["a"].get("kind")
        out.versions = (work or {}).get("versions") or base["a"].get("versions") or {}
        summary = {"key": key, "sha_a": base["a"].get("sha"), "sha_b": (base.get("b") or {}).get("sha") if base.get("b") else None,
                   "stable": base.get("stable"), "kind": out.kind,
                   "base_files": sorted(set(base["a"].get("files") or []) | set((base.get("b") or {}).get("files") or [])),
                   "work_files": list((state or {}).get("work_files") or []) if (state or {}).get("key") == key else [],
                   "app_file": base["a"].get("app_file"), "failed": None}
        out.summary = summary
        if out.kind == "none":
            out.state = "none"
            return out
        if base.get("stable") is False and (base.get("noise") is None or base.get("noise_pending")):
            noise = _noise(cfg, base["a"]["text"], base["b"]["text"], min(timeout_s, left())) if left() >= MIN_STAGE_S else None
            if noise is None:
                base["noise"], base["noise_pending"] = None, True
                out.cache_write = base if use_cache else None
                return _budget(out)
            base["noise"], base["noise_pending"] = noise, False
            out.cache_write = base if use_cache else None
        if not complete(work):
            if built(work) and (work or {}).get("kind") == "none":
                kind, detail = "none", "the app in the edited code has no openapi() method"
            else:
                kind, detail = _failure(work)
            out.notes.append(_note("work", kind, detail))
            return out
        w = _entry(work, work_root)
        summary["work_files"] = w["files"]
        summary["app_file"] = w["app_file"] or summary["app_file"]
        if w["text"] == base["a"]["text"] or (base.get("b") and w["text"] == base["b"].get("text")):
            out.state = "checked"
            return out
        if left() < MIN_STAGE_S:
            return _budget(out)
        file = w["app_file"] or f"app:{out.app}"
        breaks = _compare(cfg, out, base["a"]["text"], w["text"], min(timeout_s, left()), file)
        standing, hedged = _filter(breaks, base.get("noise"), base.get("stable"))
        out.breaks, out.hedged = standing, hedged
        out.state = "hedged" if hedged and not standing else "checked"
        return out
    except Exception as exc:  # noqa: BLE001 - fails open
        out.state = "not_checked"
        out.breaks, out.hedged = [], []
        out.notes.append(("app-openapi-internal", f"LegacyGuard: the app's OpenAPI check failed inside LegacyGuard "
                                                  f"({type(exc).__name__}); schema changes were not checked."))
        return out
    finally:
        out.seconds = round(time.time() - started, 3)


def _baseline(out: SpecOutcome, a, b, state, key: str, base_root: str) -> dict | None:
    """The baseline to compare against (cache_write set when it may be cached), or None with out filled in."""
    if built(a) and built(b) and (a or {}).get("kind") == "none" and (b or {}).get("kind") == "none":
        base = {"key": key, "stable": True, "a": _entry(a, base_root), "b": None, "noise": None, "noise_pending": False}
        out.cache_write = base
        return base
    if complete(a) and complete(b):
        stable = a["text"] == b["text"]
        base = {"key": key, "stable": stable, "a": _entry(a, base_root), "b": None if stable else _entry(b, base_root),
                "noise": None, "noise_pending": not stable}
        out.cache_write = base
        return base
    one = a if complete(a) else b if complete(b) else None
    bad = b if one is a else a
    kind, detail = _failure(bad)
    if one is None:
        out.failed = {"detail": detail, "one_sha": None}
        out.notes.append(_note("base", kind, detail))
        return None
    prev = ((state or {}).get("failed") or {}) if (state or {}).get("key") == key else {}
    base = {"key": key, "stable": None, "a": _entry(one, base_root), "b": None, "noise": None, "noise_pending": False}
    if int(prev.get("attempts") or 0) >= 1 and prev.get("one_sha") and prev.get("one_sha") == one.get("sha"):
        out.cache_write = base  # the same single build twice: go on with it, every break hedged (D10)
        return base
    out.failed = {"detail": detail, "one_sha": one.get("sha")}
    return base


def _compare(cfg, out: SpecOutcome, ta: str, tb: str, timeout: float, file: str) -> list:
    """Old-vs-new breaks as (file, SpecBreak), with notes for a fallback."""
    from guardlib import checks
    got = _changes(cfg, ta, tb, timeout)
    breaks = []
    if got[0] == "oasdiff":
        from guardlib import contract_tools
        out.via = "oasdiff"
        for c in got[1]:
            item, subject = contract_tools.break_fields(REL, c)
            change = (c.get("id", ""), c.get("operation", ""), c.get("path", ""), c.get("text", ""))
            breaks.append((file, SpecBreak("http", "app-oas:" + item, subject, SOURCE, file, change=change)))
        out.warns = [w.get("text") or w.get("id") or "?" for w in got[2]]
        return breaks
    _, found, (old_only, new_only), refusal = got
    out.via = "parser"
    out.parser_why = getattr(refusal, "kind", "") or ""
    reason = getattr(refusal, "reason", "")
    if refusal is not None and refusal.kind in checks.TOOL_NOTE_KIND and reason:
        out.notes.append((checks.TOOL_NOTE_KIND[refusal.kind], "LegacyGuard: " + reason))
    for raw, subject in found:
        breaks.append((file, SpecBreak("http", "app-oas-parser:" + raw, subject, SOURCE, file, raw=raw)))
    if old_only:
        out.notes.append(("app-openapi-renamed",
                          f"LegacyGuard: the built-in parser did not compare the fields of {len(old_only)} schemas whose names "
                          f"changed in the generated spec ({', '.join(old_only[:3])} -> {', '.join(new_only[:3]) or 'none'}), "
                          "for example when a second model of the same name appears."))
    return breaks


# ---- messages ----

def _how(outcome: SpecOutcome) -> str:
    if outcome.via == "oasdiff":
        return "oasdiff reports as breaking"
    if outcome.parser_why in ("missing", "off", ""):
        return "the built-in parser reports as breaking (oasdiff not available)"
    return "the built-in parser reports as breaking (oasdiff not used for this comparison)"


def _listing(subjects: list[str]) -> str:
    more = f" (+{len(subjects) - MAX_LISTED} more)" if len(subjects) > MAX_LISTED else ""
    return "; ".join(subjects[:MAX_LISTED]) + more


def wake_items(outcome: SpecOutcome | None, seen) -> list[str]:
    """Standing break items not yet woken this session (session.json app_openapi_seen)."""
    if outcome is None:
        return []
    seen = set(seen or [])
    return list(dict.fromkeys(b.item for _, b in outcome.breaks if b.item not in seen))


def wake_text(outcome: SpecOutcome, items: list[str]) -> str:
    subjects = [b.subject for _, b in outcome.breaks if b.item in set(items)]
    return (f"LegacyGuard: the OpenAPI spec this app generates (app: {outcome.app}, built in the baseline and in the edited "
            f"code) changed in ways {_how(outcome)}: {_listing(subjects)}. Clients written against the old spec may fail. "
            "If the change was intended, say so; the developer can waive it with /legacyguard:waive.")


def context_text(outcome: SpecOutcome | None, woke: list[str] | None = None) -> str | None:
    """Context lines that are not a wake: standing breaks already woken earlier, hedged breaks with their hedge, and
    WARN changes. Not-checked and fallback notes go through contract_tools.take_notes."""
    if outcome is None:
        return None
    lines = []
    woke = set(woke or [])
    earlier = [b.subject for _, b in outcome.breaks if b.item not in woke]
    if earlier:
        lines.append(f"LegacyGuard: the OpenAPI spec this app generates (app: {outcome.app}) still differs from the "
                     f"baseline's in ways {_how(outcome)}, as reported earlier: {_listing(earlier)}.")
    by_hedge: dict[str, list[str]] = {}
    for _, b in outcome.hedged:
        by_hedge.setdefault(b.hedge, []).append(b.subject)
    for hedge, subjects in by_hedge.items():
        lines.append(f"LegacyGuard: the OpenAPI spec this app generates (app: {outcome.app}) changed in ways {_how(outcome)}, "
                     f"but {hedge}: {_listing(subjects)}.")
    if outcome.warns:
        n = len(outcome.warns)
        lines.append(f"LegacyGuard: oasdiff also reports {n} possible breaking change{'s' if n != 1 else ''} it cannot "
                     f"confirm in {LABEL}: {'; '.join(outcome.warns[:3])}.")
    return "\n".join(lines) or None


def paused_outcome(state: dict | None) -> SpecOutcome:
    failed = (state or {}).get("failed") or {}
    n = int(failed.get("attempts") or MAX_BASE_ATTEMPTS)
    return SpecOutcome("not_checked", paused=True, key=(state or {}).get("key"),
                       notes=[("app-openapi-paused", PAUSED_NOTE.format(n=n, detail=failed.get("detail") or "unknown"))])


def record_run(data: dict, outcome: SpecOutcome | None) -> list:
    """Store what this check learned in session.json (call under the ledger lock): run counts for the report, the
    summary, and the failure memory (attempts counted from the session.json read under this lock). Returns the
    (kind, text) notes to pass to contract_tools.take_notes."""
    if outcome is None:
        return []
    notes = list(outcome.notes)
    try:
        ct = data.setdefault("contract_tools", {})
        entry = ct.setdefault("app_openapi", {"runs": 0, "versions": {}, "kind": None})
        entry["runs"] = int(entry.get("runs") or 0) + int(outcome.runs or 0)
        if outcome.versions:
            entry["versions"] = outcome.versions
        if outcome.kind:
            entry["kind"] = outcome.kind
        if outcome.paused:
            return notes
        if outcome.failed is not None:
            prev = data.get("app_openapi") or {}
            pf = (prev.get("failed") or {}) if prev.get("key") == outcome.key else {}
            n = int(pf.get("attempts") or 0) + 1
            paused = n >= MAX_BASE_ATTEMPTS
            data["app_openapi"] = {"key": outcome.key, "failed": {"attempts": n, "detail": outcome.failed.get("detail"),
                                                                  "paused": paused, "one_sha": outcome.failed.get("one_sha")}}
            if paused:
                notes.append(("app-openapi-paused", PAUSED_NOTE.format(n=n, detail=outcome.failed.get("detail") or "unknown")))
        elif outcome.summary is not None:
            data["app_openapi"] = outcome.summary
    except Exception:  # noqa: BLE001
        pass
    return notes


def code_hash(b) -> str:
    return symbols.sha(b.item)
