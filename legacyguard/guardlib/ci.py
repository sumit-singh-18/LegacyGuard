"""The guard on a pull request or any branch, outside a Claude Code session (roadmap E1).

OLD is a git worktree at the base (the merge base with the default branch, or --base). NEW is the checked-out tree.
Every changed Python file gets the same checks a session gives an edit: contract and test-integrity checks, the
behavioral diff of changed functions, their callers and the routes they serve. The result is a Markdown report
(also written to the GitHub step summary), GitHub annotations on the changed lines, and an exit code.

This guards changes from people and from any coding agent, not only Claude Code sessions. There is nobody to wake,
so differences are reported; waivers committed in .legacyguard/waivers.json are honored.
"""
from __future__ import annotations

import copy
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from guardlib import baseline, checks, config as cfgmod, contracts_parse as cp, engine, symbols, waivers
from runner import diff


@dataclass
class Outcome:
    base: str
    files: list[str] = field(default_factory=list)
    results: list = field(default_factory=list)       # diff.Result
    breaks: list = field(default_factory=list)        # (file, checks.Break)
    lines: dict = field(default_factory=dict)         # "file::qualname" -> line in the new file
    skipped: list = field(default_factory=list)       # callers left out of the budget, with reasons (engine.skipped_note)
    notes: list = field(default_factory=list)         # contract-tool notes (Sprint 7.6): oasdiff fallbacks, model drift

    def failing(self, cfg) -> list:
        """Differences that fail the check. One found only on a placeholder instance (Task 6.8c) is not among them: it is
        listed apart (placeholder_only) and annotated as a warning."""
        out = [r for r in self.results if r.status in (diff.DIFFERENT, diff.BROKEN) and not diff.placeholder_only(r)
               and not waivers.is_waived(cfg, "behavior_difference", r.file, r.qualname, r.code_hash)]
        return out

    def placeholder_only(self, cfg) -> list:
        return [r for r in self.results if diff.placeholder_only(r)
                and not waivers.is_waived(cfg, "behavior_difference", r.file, r.qualname, r.code_hash)]


def _git(root: str, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=120)
    if check and res.returncode:
        raise RuntimeError(res.stderr.strip() or f"git {args[0]} failed")
    return res.stdout.strip()


def default_base(root: str) -> str:
    """The merge base with origin's default branch, else with main or master."""
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if subprocess.run(["git", "-C", root, "rev-parse", "--verify", "-q", ref], capture_output=True).returncode == 0:
            try:
                return _git(root, "merge-base", ref, "HEAD")
            except RuntimeError:
                continue
    return _git(root, "rev-parse", "HEAD~1")


def load_cfg(root: str, python: str | None = None):
    """The project's config if it has one, else what auto-setup would write, in memory only."""
    try:
        cfg = cfgmod.load_config(root)
    except cfgmod.ConfigMissing:
        from guardlib import autosetup
        raw = copy.deepcopy(cfgmod.DEFAULTS)
        found = autosetup.detect(root) or {}
        for key in ("scope", "source_roots", "django_settings", "app", "contracts"):
            if found.get(key):
                raw[key] = found[key]
        cfg = cfgmod.Config(root=root, raw=raw)
    if python:
        cfg.raw["python"] = python
    cfg.raw["behavioral_diff"]["warm"] = False  # one run: nothing to keep warm
    return cfg


def _baseline_at(cfg, base: str) -> Path:
    """A worktree of the base where the session baseline lives, so every engine function finds it."""
    target = baseline.baseline_dir(cfg)
    baseline.ensure_state_dir(cfg)
    baseline.remove(cfg)
    _git(cfg.root, "worktree", "add", "--detach", "-f", str(target), base)
    return target


def apply_overrides(cfg, overrides: dict | None) -> None:
    """Merge overrides into cfg.raw; a nested mapping (behavioral_diff, ...) is merged key by key."""
    for key, value in (overrides or {}).items():
        if isinstance(value, dict) and isinstance(cfg.raw.get(key), dict):
            cfg.raw[key].update(value)
        else:
            cfg.raw[key] = value


def _baseline_routes(cfg, base_dir: str, index: bool) -> None:
    """Sprint 7.2b: refresh() builds the routes table on the head tree here, so the baseline's routes (for deleted and
    renamed handlers) come from asking the framework in the baseline tree. Without indexing, rows left by an earlier
    run describe another base, so they are cleared and the lookup falls back to routes, then to decorators."""
    from indexer import db, routes
    if not db.exists(cfg.root):
        return
    try:
        conn = db.connect(cfg.root)
    except Exception:  # noqa: BLE001 - without baseline routes, deleted and renamed handlers use the fallbacks
        return
    try:
        # The old rows go first, committed on their own, so a failure below never leaves another base's rows behind.
        conn.execute("DELETE FROM baseline_routes")
        conn.commit()
        if index:
            routes.rebuild_baseline(conn, cfg, base_dir)
            conn.commit()
    except Exception:  # noqa: BLE001 - without baseline routes, deleted and renamed handlers use the fallbacks
        try:
            conn.rollback()
            conn.execute("DELETE FROM baseline_routes")
            conn.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        conn.close()


def run(root: str, base: str | None = None, python: str | None = None, index: bool = True,
        overrides: dict | None = None) -> tuple:
    """(cfg, Outcome) for the changes between base and the working tree. overrides go on top of the loaded config."""
    root = str(Path(root).resolve())
    cfg = load_cfg(root, python)
    apply_overrides(cfg, overrides)
    base = base or default_base(root)
    base_dir = _baseline_at(cfg, base)
    from guardlib import contract_tools
    contract_tools.refresh(cfg, reset_counters=False)
    if index:
        try:
            from indexer import __main__ as indexer_main
            indexer_main.refresh(cfg)  # routes and contracts; tests are not run here
        except Exception:  # noqa: BLE001 - without an index, routes are skipped; everything else still runs
            pass
    _baseline_routes(cfg, str(base_dir), index)
    changed = [f for f in _git(root, "diff", "--name-only", base, "--", "*.py").splitlines() if f]
    out = Outcome(base=base, files=changed)
    http_rel = cfg.contract_path("http")
    http = {}
    if http_rel and (base_dir / http_rel).exists():
        try:
            http = cp.http_items((base_dir / http_rel).read_text())
        except Exception:  # noqa: BLE001
            http = {}
    contract_files = [f for f in _git(root, "diff", "--name-only", base).splitlines() if f and f not in changed]
    from runner import scratch
    # Task 7.4: one throwaway working folder per tree for every check of this run. No session cache (Part B): the
    # base worktree is rebuilt per run, and this one block already clones it once for the whole run.
    tool_notes: list = []
    tool_timeout = contract_tools.settings(cfg)["timeout_s"]
    with scratch.for_trees(cfg, str(base_dir), root):
        for rel in changed + contract_files:
            before = baseline.read_baseline(cfg, rel)
            after_path = Path(root) / rel
            after = after_path.read_text(errors="replace") if after_path.exists() else ""
            for b in checks.run_checks(cfg, rel, before, after, http, trees=(str(base_dir), root), notes=tool_notes,
                                       tool_timeout=tool_timeout):
                if not waivers.is_waived(cfg, "contract_break", rel, b.item, symbols.sha(after)):
                    out.breaks.append((rel, b))
            if rel not in changed or not engine.is_guarded_source(cfg, rel) or before is None or not after:
                continue
            targets = diff.changed_targets(rel, before, after)
            targets += engine.dependents(cfg, rel, targets, {t.qualname for t in targets}, skipped=out.skipped,
                                         limit=diff.MAX_FUNCTIONS - len(targets))
            targets += engine.route_targets(cfg, targets)
            targets += engine.removed_route_targets(cfg, rel, before, after, {t.qualname for t in targets})
            seen = {(r.file, r.qualname) for r in out.results}
            targets = [t for t in targets if (t.file, t.qualname) not in seen]
            if not targets:
                continue
            out.results += diff.run(cfg, str(base_dir), root, targets)
            for t in targets:
                text = (Path(root) / t.file).read_text(errors="replace") if (Path(root) / t.file).exists() else ""
                sym = next((s for s in symbols.extract(text)[0] if s.qualname == t.qualname), None)
                out.lines[f"{t.file}::{t.qualname}"] = sym.line_start if sym else 1
        _drift(cfg, out, str(base_dir), root, changed + contract_files)
    out.notes = list(dict.fromkeys([t for _, t in tool_notes] + out.notes))
    return cfg, out


def _drift(cfg, out: Outcome, base_dir: str, root: str, files: list[str]) -> None:
    """Sprint 7.6: Django model drift new since the base, as contract breaks (no session data, no cache)."""
    from guardlib import migration_drift
    try:
        if not any(migration_drift.applies(cfg, rel, _read(Path(root) / rel)) for rel in files):
            return
        drift = migration_drift.compute(cfg, base_dir, root, None)
    except Exception as exc:  # noqa: BLE001 - fails open: a note, no break
        out.notes.append(f"LegacyGuard: the migration check failed ({type(exc).__name__}).")
        return
    if drift.note:
        out.notes.append(drift.note)
    for file, b in drift.breaks:
        if not waivers.is_waived(cfg, "contract_break", file, b.item, migration_drift.code_hash(b)):
            out.breaks.append((file, b))


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _esc(text: str) -> str:
    """GitHub workflow-command escaping for a message."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotations(cfg, out: Outcome) -> list[str]:
    lines = []
    for r in out.failing(cfg):
        d = r.differences[0] if r.differences else None
        msg = f"{d['call']}  before {d['before']}  after {d['after']}" if d else r.reason
        if r.via:
            msg = f"Not edited, but it {r.via} (edited in {r.via_file}). " + msg
        if r.placeholder_note:
            msg += f". {r.placeholder_note}"
        line = out.lines.get(f"{r.file}::{r.qualname}", 1)
        lines.append(f"::error file={r.file},line={line},title=LegacyGuard: {r.qualname} behaves differently::{_esc(msg)}")
    for r in out.placeholder_only(cfg):
        d = r.differences[0]
        msg = f"{d['call']}  before {d['before']}  after {d['after']}. {r.placeholder_note}"
        line = out.lines.get(f"{r.file}::{r.qualname}", 1)
        lines.append(f"::warning file={r.file},line={line},title=LegacyGuard: {r.qualname} differs on a placeholder instance::{_esc(msg)}")
    for rel, b in out.breaks:
        if rel.startswith("django-app:"):  # drift in a third-party app: not a path GitHub can attach to
            lines.append(f"::warning title=LegacyGuard contract::{_esc(rel + ': ' + b.text())}")
        else:
            lines.append(f"::warning file={rel},title=LegacyGuard contract::{_esc(b.text())}")
    return lines


def not_run(out: Outcome) -> list[dict]:
    """Skipped callers that no file's check ran after all, each once (a caller of two edited files is skipped twice,
    or skipped for one file and run for another)."""
    ran = {(r.file, r.qualname) for r in out.results}
    seen: set = set()
    keep = []
    for s in out.skipped:
        key = (s["file"], s["qualname"])
        if key not in ran and key not in seen:
            seen.add(key)
            keep.append(s)
    return keep


def report(cfg, out: Outcome) -> str:
    failing = out.failing(cfg)
    verified = [r for r in out.results if r.status == diff.DONE]
    untested = [r for r in out.results if r.status == diff.UNTESTED]
    on_placeholder = out.placeholder_only(cfg)
    waived = [r for r in out.results if r.status in (diff.DIFFERENT, diff.BROKEN) and r not in failing and r not in on_placeholder]
    slower = [r for r in out.results if r.slowdowns]  # Task 6.6: listed apart, never failing the check
    lines = ["## LegacyGuard", "",
             f"Compared the changes against `{out.base[:10]}` by running old and new code on the same inputs: "
             f"**{len(failing)} behave differently**, {len(verified)} agree, {len(untested)} could not be checked"
             + (f", {len(on_placeholder)} differ only on a placeholder instance (not failing)" if on_placeholder else "")
             + (f", {len(waived)} waived" if waived else "") + (f", {len(slower)} slower in the new code" if slower else "")
             + (f", {len(out.breaks)} contract or test changes" if out.breaks else "") + ".", ""]
    if failing:
        lines += ["### Behaves differently", ""]
        for r in failing:
            where = f" (not edited; it {r.via}, edited in `{r.via_file}`)" if r.via else ""
            lines.append(f"- **`{r.file}::{r.qualname}`**{where}")
            for d in r.differences[:3]:
                lines.append(f"  - `{d['call']}` before `{d['before']}` after `{d['after']}`")
            for d in r.declared_failures[:3]:
                lines.append(f"  - `{d['call']}` declared {d['expected']}, got {d['got']}")
            if not r.differences and r.reason:
                lines.append(f"  - {r.reason}")
            if r.placeholder_note:
                lines.append(f"  - {r.placeholder_note}.")
        lines += ["", "If a difference is intended, record it in `.legacyguard/waivers.json` with `/legacyguard:waive` and commit the file.", ""]
    if on_placeholder:
        lines += ["### Differs only on a placeholder instance (does not fail the check)", ""]
        for r in on_placeholder:
            where = f" (not edited; it {r.via}, edited in `{r.via_file}`)" if r.via else ""
            lines.append(f"- `{r.file}::{r.qualname}`{where}")
            for d in r.differences[:3]:
                lines.append(f"  - `{d['call']}` before `{d['before']}` after `{d['after']}`")
            lines.append(f"  - {r.placeholder_note}.")
        lines.append("")
    if out.breaks:
        lines += ["### Contract and test changes", ""] + [f"- `{rel}`: {b.text()}" for rel, b in out.breaks] + [""]
    if out.notes:
        lines += ["<details><summary>Contract tool notes</summary>", ""] + [f"- {n}" for n in out.notes] + ["", "</details>", ""]
    if verified:
        lines += ["<details><summary>Agree on the inputs tried</summary>", ""]
        for r in verified:
            conf = diff.confidence_text(r)
            ph = diff.placeholder_text(r)
            lines.append(f"- `{r.file}::{r.qualname}`: {r.inputs_compared} inputs" + (f"; {conf}" if conf else "") + (f"; {ph}" if ph else ""))
        lines += ["", "</details>", ""]
    if slower:
        lines += ["<details><summary>Slower in the new code (not counted as a difference)</summary>", ""]
        for r in slower:
            lines += [f"- `{r.file}::{r.qualname}`: {diff.slowdown_text(s)}" for s in r.slowdowns[:3]]
        lines += ["", "</details>", ""]
    if untested:
        lines += ["<details><summary>Could not be checked</summary>", ""]
        lines += [f"- `{r.file}::{r.qualname}`: {r.reason}" for r in untested] + ["", "</details>", ""]
    skipped_text = engine.skipped_note(not_run(out))
    if skipped_text:
        lines += ["<details><summary>Callers not run</summary>", ""] + [f"- {x}" for x in skipped_text.splitlines()] + ["", "</details>", ""]
    lines.append("Agreement is evidence on the inputs tried, not a proof.")
    return "\n".join(lines)


def exit_code(cfg, out: Outcome, fail_on: str) -> int:
    if fail_on == "none":
        return 0
    if out.failing(cfg):
        return 1
    if fail_on == "any" and out.breaks:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="legacyguard-ci", description="Run LegacyGuard on the changes since a base commit.")
    p.add_argument("--root", default=os.getcwd())
    p.add_argument("--base", default="", help="base commit or ref (default: merge base with the default branch)")
    p.add_argument("--python", default="", help="the project's Python, with its dependencies installed")
    p.add_argument("--fail-on", choices=("difference", "any", "none"), default="difference")
    p.add_argument("--no-index", action="store_true")
    a = p.parse_args(argv)
    cfg, out = run(a.root, a.base or None, a.python or None, index=not a.no_index)
    text = report(cfg, out)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text + "\n")
    if os.environ.get("GITHUB_ACTIONS"):
        for line in annotations(cfg, out):
            print(line)
    try:
        baseline.remove(cfg)
    except Exception:  # noqa: BLE001
        pass
    return exit_code(cfg, out, a.fail_on)
