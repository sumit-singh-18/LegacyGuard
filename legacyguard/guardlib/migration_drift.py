"""Django model drift (Sprint 7.6 Part A): model changes that have no migration, found by running
`makemigrations --check --dry-run` (non-interactive, no database) in both trees and keeping only what is new.

The command runs in the target interpreter through `runner/lg_migrations.py`, inside the worker's set-up
(`lg_worker.prepare`: blocks, stubs, a scratch folder, the tree protected), under the OS sandbox, with a throwaway
working folder (`scratch.current`). It runs project code (settings, models, migration modules), so it never runs in
PreToolUse: PostToolUse gives a context note (no finding), the Stop audit and `ci.run` give `contract_break` findings
with `contract_kind: "db"` and `source: "makemigrations"`.

Decisions come from the recorded operations, questions and conflicts, never from the exit code. A run that is not
complete (set-up failed, something was blocked, a CommandError, a timeout, unreadable output) is "not checked": never
drift and never "no drift".

Known limits:
- The project's settings must import inside the worker (stubs on, network and database blocked) and its database
  driver must be importable in the target interpreter; otherwise the check is skipped with a note.
- Non-null and `auto_now_add` fields without a default are answered with a placeholder default so detection goes on;
  the item says the field needs a default. Rename questions answer "no", so a renamed field shows as remove plus add.
- Third-party apps' drift appears in both trees and is filtered by "new only"; drift caused by a dependency upgrade
  inside the session would be reported as new.
- A drift waiver is keyed on Django's one-line description of the operation (`sha(item)`, e.g. "Add field price to
  item"), not on the field definition: it survives unrelated edits, but also a later change to that field's type or
  options while the description stays the same. Another Django version describing it differently asks again.
- Switching the check off during a session (`contract_tools.makemigrations: false`, or `django_settings` removed)
  makes `applies` false, so the next Stop sweeps existing drift findings to `not_applied`, which reads as "fixed" in
  the report although nothing was checked.
- At Stop, "not checked" and hedge notes are recorded again on every Stop (they are not deduplicated per session as
  the PostToolUse notes are).
- In CI, drift in a third-party app (`file = "django-app:<app>"`) is not a real path: its annotation has no `file=`
  and the comment lists it under the app name.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from guardlib import config as cfgmod, paths, symbols
from guardlib.checks import Break

RUNNER = Path(__file__).resolve().parent.parent / "runner"
SCRIPT = RUNNER / "lg_migrations.py"
MARK = "LGMIG"
WHAT_RAN = "makemigrations --check --dry-run, no database"


@dataclass
class Item:
    app: str
    item: str
    subject: str
    leaves: tuple = ()


@dataclass
class DriftBreak(Break):
    leaves: tuple = ()


@dataclass
class DriftOutcome:
    state: str                     # checked | not_checked | hedged
    breaks: list = field(default_factory=list)   # (file, DriftBreak)
    note: str | None = None
    base_result: dict | None = None  # {"key", "result"} to cache in session.json, when newly run and complete
    note_kind: str | None = None   # tool_notes entry for once-per-session notes; None: shown every time
    runs: int = 0
    django: str | None = None
    seconds: float = 0.0


def enabled(cfg) -> bool:
    try:
        from guardlib import contract_tools
        value = contract_tools.settings(cfg).get("makemigrations", "auto")
        if value is False or str(value).lower() in ("false", "off", "no"):
            return False
        return bool(cfg.raw.get("django_settings"))
    except Exception:  # noqa: BLE001
        return False


def applies(cfg, rel: str | None, after_text: str | None) -> bool:
    """Whether an edit to rel may change Django model drift: a models module or package, a migration, or a file that
    defines models."""
    try:
        if not rel or not rel.endswith(".py") or not enabled(cfg) or not paths.in_scope(rel, cfg.scope):
            return False
        parts = rel.split("/")
        if parts[-1] == "models.py" or "models" in parts[:-1] or "migrations" in parts[:-1]:
            return True
        text = after_text or ""
        return "models.Model" in text or "from django.db import models" in text
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
                       "settings": cfg.raw.get("django_settings"), "python": real, "python_mtime": mtime,
                       "io": bd.get("io", "stub"), "source_roots": cfg.raw.get("source_roots") or [],
                       "env": bd.get("env") or {}}, sort_keys=True)


def complete(result: dict | None) -> bool:
    return bool(result) and result.get("error") is None and not result.get("blocks")


def run_tree(cfg, tree: str, timeout: float) -> dict | None:
    """The lg_migrations result for one tree. A result with `error` set when the run could not finish; None on an
    internal failure."""
    try:
        from indexer.coverage_map import clean_env
        from runner import os_sandbox, scratch
        bd = cfg.raw.get("behavioral_diff") or {}
        py = cfgmod.target_python(cfg)
        if not py:
            return {"error": "no Python found for the project", "kind": "nodjango"}
        tree = str(tree)
        roots = [str(Path(tree) / r) for r in (cfg.raw.get("source_roots") or [])] + [tree]
        with scratch.current(cfg, tree) as cwd, tempfile.TemporaryDirectory(prefix="lgmig") as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps({"root": tree, "sys_path": roots, "protect": [str(cfg.root), tree], "cwd": cwd.path,
                                       "cwd_root": cwd.folder or cwd.tree or cwd.path, "scratch": str(Path(tmp) / "scratch"),
                                       "django_settings": cfg.raw.get("django_settings"), "deterministic": False,
                                       "io": str(bd.get("io", "stub"))}))
            env = clean_env(cfg)
            env.pop("PYTHONPATH", None)
            writable = [tempfile.gettempdir(), tmp] + ([cwd.path] if cwd.strategy != "off" else [])
            cmd = os_sandbox.wrap([py, "-B", str(SCRIPT), str(req)], py, writable=writable,
                                  readonly=[str(cfg.root), tree], mode=str(bd.get("os_sandbox", "auto")))
            try:
                done = subprocess.run(cmd, cwd=cwd.path, env=env, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"error": f"timed out after {timeout} s", "kind": "timeout"}
            except OSError as exc:
                return {"error": f"could not start {py}: {exc}", "kind": "nodjango"}
        for line in (done.stdout or "").splitlines():
            if line.startswith(MARK):
                try:
                    out = json.loads(line[len(MARK):])
                except ValueError:
                    break
                return out if isinstance(out, dict) else {"error": "unreadable output", "kind": "output"}
        last = (done.stderr or "").strip().splitlines()[-1:] or [f"exit code {done.returncode}"]
        return {"error": f"no result: {last[0][:200]}", "kind": "output"}
    except Exception:  # noqa: BLE001
        return None


def items(result: dict) -> dict[str, Item]:
    out: dict[str, Item] = {}
    for app, ops in sorted((result.get("changes") or {}).items()):
        for op in ops:
            key = f"drift:{app}:{op}"
            out[key] = Item(app, key, f"model change without a migration: {app}: {op}")
    for a in result.get("asked") or []:
        app = a.get("app") or "?"
        key = f"drift:{app}:needs-default:{a.get('model')}.{a.get('field')}"
        kind = str(a.get("kind") or "").replace("_", " ")
        out[key] = Item(app, key, f"model field cannot be migrated without a default: {a.get('model')}.{a.get('field')} ({kind})")
    for app, leaves in sorted((result.get("conflicts") or {}).items()):
        key = f"drift:{app}:conflict"
        out[key] = Item(app, key, f"conflicting migrations in {app}: {', '.join(leaves)}", tuple(sorted(leaves)))
    return out


def new_items(base: dict, work: dict) -> list[Item]:
    """Items of the working tree not in the baseline. A conflict is new when the app had none in the baseline or
    has a leaf the baseline's conflict did not have."""
    b, w = items(base), items(work)
    out = []
    for key, it in w.items():
        if key.endswith(":conflict"):
            old = b.get(key)
            if old is None or not set(it.leaves) <= set(old.leaves):
                out.append(it)
        elif key not in b:
            out.append(it)
    return out


def file_for(result: dict, app: str, tree: str) -> str:
    """Repo-relative models file of the app when inside the tree, else its migrations folder, else django-app:<app>."""
    info = (result.get("apps") or {}).get(app) or {}
    root = os.path.realpath(tree).rstrip(os.sep) + os.sep
    models = info.get("models_file")
    if models:
        real = os.path.realpath(models)
        if real.startswith(root):
            return real[len(root):].replace(os.sep, "/")
    path = info.get("path")
    if path:
        real = os.path.realpath(path)
        if real.startswith(root):
            return real[len(root):].replace(os.sep, "/") + "/migrations/"
    return f"django-app:{app}"


def code_hash(b) -> str:
    leaves = getattr(b, "leaves", ()) or ()
    if b.item.endswith(":conflict"):
        return symbols.sha(b.item + "|" + "|".join(sorted(leaves)))
    return symbols.sha(b.item)


def _failure(result: dict | None, cfg) -> tuple[str, str]:
    """(reason kind, note) for a run that is not complete."""
    if result is None:
        return "internal", "the migration check failed inside LegacyGuard"
    err = str(result.get("error") or "")
    detail = str(result.get("detail") or "")
    if result.get("blocks"):
        return "blocked", f"the migration check was stopped: {', '.join(sorted(set(result['blocks'])))} was refused while it ran"
    if err == "setup":
        if "No module named 'django'" in detail:
            return "nodjango", f"Django did not import in {cfgmod.target_python(cfg)}; the migration check was skipped"
        return "setup", f"the Django settings did not load ({detail[:200]}); the migration check was skipped"
    kind = result.get("kind") or ("command" if "CommandError" in err else "error")
    return kind, f"the migration check could not finish ({err[:200]})"


def _cached_base(base_cached, key: str, work: dict):
    if not isinstance(base_cached, dict) or base_cached.get("key") != key:
        return None
    res = base_cached.get("result")
    if not complete(res) or res.get("django") != work.get("django"):
        return None
    return res


def compute(cfg, base_root: str, work_root: str, base_cached=None, data: dict | None = None, timeout: float | None = None) -> DriftOutcome:
    """New drift in the working tree against the baseline. base_cached: session.json migration_drift_base."""
    started = time.time()
    try:
        from guardlib import contract_tools
        timeout = float(timeout if timeout is not None else contract_tools.settings(cfg)["timeout_s"])
        work = run_tree(cfg, work_root, timeout)
        runs = 1
        if not complete(work):
            kind, text = _failure(work, cfg)
            return DriftOutcome("not_checked", note=f"LegacyGuard: {text}.", note_kind=f"makemigrations-{kind}", runs=runs,
                                django=(work or {}).get("django"), seconds=round(time.time() - started, 3))
        key = cache_key(cfg, data) if base_cached is not None else None
        base = _cached_base(base_cached, key, work) if key else None
        fresh = None
        if base is None:
            base = run_tree(cfg, base_root, timeout)
            runs += 1
            if complete(base) and data is not None:
                fresh = {"key": key or cache_key(cfg, data), "result": base}
        seconds = round(time.time() - started, 3)
        if not complete(base):
            found = items(work)
            if not found:
                return DriftOutcome("checked", note=None, runs=runs, django=work.get("django"), seconds=seconds)
            _, why = _failure(base, cfg)
            listing = "; ".join(i.subject for i in list(found.values())[:6])
            return DriftOutcome("hedged", note=(f"LegacyGuard: model changes without a migration ({WHAT_RAN}): {listing}. "
                                                f"The baseline could not be checked ({why}), so some of this may predate the session."),
                                runs=runs, django=work.get("django"), seconds=seconds)
        breaks = []
        for it in new_items(base, work):
            f = file_for(work, it.app, work_root)
            breaks.append((f, DriftBreak("db", it.item, it.subject, "makemigrations", f, leaves=it.leaves)))
        note = f"Model drift check: ran {WHAT_RAN} in both trees ({seconds:.2f} s); {len(breaks)} new item{'s' if len(breaks) != 1 else ''}."
        return DriftOutcome("checked", breaks, note, fresh, None, runs, work.get("django"), seconds)
    except Exception as exc:  # noqa: BLE001
        return DriftOutcome("not_checked", note=f"LegacyGuard: the migration check failed inside LegacyGuard ({type(exc).__name__}).",
                            note_kind="makemigrations-internal", seconds=round(time.time() - started, 3))


def context_text(outcome: DriftOutcome | None) -> tuple[str | None, str | None]:
    """(text, tool_notes kind) for PostToolUse: the new drift, the hedge, or the not-checked note."""
    if outcome is None:
        return None, None
    if outcome.state == "checked":
        if not outcome.breaks:
            return None, None
        listing = "; ".join(b.subject for _, b in outcome.breaks[:6])
        more = f" (+{len(outcome.breaks) - 6} more)" if len(outcome.breaks) > 6 else ""
        return (f"LegacyGuard: model changes without a migration ({WHAT_RAN}): {listing}{more}. "
                "Run makemigrations if these were intended."), None
    return outcome.note, outcome.note_kind


def record_run(data: dict, outcome: DriftOutcome | None) -> None:
    """Count runs for the report's "Contract tools" line."""
    if outcome is None or not outcome.runs:
        return
    ct = data.setdefault("contract_tools", {})
    mig = ct.setdefault("makemigrations", {"runs": 0, "django": None})
    mig["runs"] = int(mig.get("runs") or 0) + outcome.runs
    if outcome.django:
        mig["django"] = outcome.django
