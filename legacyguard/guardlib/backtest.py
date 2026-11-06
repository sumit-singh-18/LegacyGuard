"""Replay real commits and planted bugs through the guard's own checks, with no Claude involved (FR-28)."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

from guardlib import checks, contracts_parse as cp, ledger, paths
from runner import diff as differ


def _git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def _show(root, commit, rel) -> str | None:
    res = subprocess.run(["git", "-C", str(root), "show", f"{commit}:{rel}"], capture_output=True)
    if res.returncode:
        return None
    try:
        return res.stdout.decode()
    except UnicodeDecodeError:
        return None


def candidate_commits(root, limit: int, prefixes: tuple[str, ...]) -> list[tuple[str, list[str]]]:
    """Newest first: non-merge commits that have a parent and change Python files under the prefixes."""
    out = []
    log = _git(root, "log", "--no-merges", "--format=%x01%H", "--name-only", "--", "*.py")
    for chunk in log.split("\x01"):
        lines = [l.strip() for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        files = [f for f in lines[1:] if f.endswith(".py") and f.startswith(prefixes)]
        if not files:
            continue
        if subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{lines[0]}^"], capture_output=True).returncode:
            continue
        out.append((lines[0], files))
        if len(out) >= limit:
            break
    return out


def evaluate_edit(cfg, old_root: Path, new_root: Path, rel: str, before: str | None, after: str | None, harvest: dict) -> dict:
    """What the guard would do for one file change: decisions from pre-write checks, then the diff."""
    rec: dict = {"file": rel, "deny": False, "ask": False, "breaks": [], "wake": False, "functions": []}
    if paths.matches_any(rel, cfg.raw.get("frozen") or []):
        rec["deny"] = True
    text_after = after if after is not None else ""
    if before is not None or after is not None:
        def reader(r, b, a):
            def scripts(base):
                d = Path(base) / cfg.contract_path("db")
                return [p.read_text() for p in sorted(d.iterdir()) if p.suffix == ".sql"] if d.is_dir() else []
            return scripts(old_root), scripts(new_root)
        breaks = checks.run_checks(cfg, rel, before, text_after, {}, reader)
        rec["breaks"] = [b.subject for b in breaks]
        rec["ask"] = bool(breaks)
    if rel.endswith(".py") and not checks.is_test_path(rel) and before is not None and after is not None:
        targets = differ.changed_targets(rel, before, after)
        if targets:
            for r in differ.run(cfg, str(old_root), str(new_root), targets, harvest=harvest):
                status = {differ.DONE: ledger.VERIFIED, differ.DIFFERENT: ledger.DIFFERENCE, differ.BROKEN: ledger.FAILURE, differ.UNTESTED: ledger.UNTESTED}[r.status]
                rec["functions"].append({"qualname": r.qualname, "status": status, "reason": r.reason, "inputs_compared": r.inputs_compared,
                                         "dropped": r.dropped_nondeterministic, "example": {k: r.differences[0][k] for k in ("call", "before", "after")} if r.differences else None})
            rec["wake"] = any(f["status"] in (ledger.DIFFERENCE, ledger.FAILURE) for f in rec["functions"])
    return rec


def summarize(records: list[dict]) -> dict:
    edits = len(records)
    prompts = sum(r["ask"] for r in records)
    wakes = sum(r["wake"] for r in records)
    denies = sum(r["deny"] for r in records)
    statuses = Counter(f["status"] for r in records for f in r["functions"])
    reasons = Counter(f["reason"].split(":")[0][:70] for r in records for f in r["functions"] if f["status"] == ledger.UNTESTED)
    return {"edits": edits, "prompts": prompts, "wake_ups": wakes, "denies": denies,
            "prompt_rate_per_10_edits": round(prompts / edits * 10, 3) if edits else None,
            "wake_up_rate_per_20_edits": round(wakes / edits * 20, 3) if edits else None,
            "function_statuses": dict(statuses), "top_untested_reasons": reasons.most_common(8)}


class Worktrees:
    """Two scratch worktrees outside the repo, removed afterward."""

    def __init__(self, root):
        self.root = Path(root)
        self.tmp = Path(tempfile.mkdtemp(prefix="lgbacktest"))
        self.old, self.new = self.tmp / "old", self.tmp / "new"

    def __enter__(self):
        head = _git(self.root, "rev-parse", "HEAD").strip()
        _git(self.root, "worktree", "add", "--detach", "-q", str(self.old), head)
        _git(self.root, "worktree", "add", "--detach", "-q", str(self.new), head)
        return self

    def __exit__(self, *exc):
        for w in (self.old, self.new):
            subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", str(w)], capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "worktree", "prune"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def goto(self, old_commit: str, new_commit: str) -> None:
        _git(self.old, "checkout", "-q", "--force", "--detach", old_commit)
        _git(self.new, "checkout", "-q", "--force", "--detach", new_commit)


def replay(cfg, limit: int, prefixes: tuple[str, ...], harvest: dict, out_path: Path, log=print) -> dict:
    root = Path(cfg.root)
    commits = candidate_commits(root, limit, prefixes)
    records, per_commit = [], []
    done = {}
    if out_path.exists():
        done = {c["commit"]: c for c in json.loads(out_path.read_text()).get("commits", [])}
    with Worktrees(root) as wt:
        for i, (commit, files) in enumerate(commits, 1):
            if commit in done:
                per_commit.append(done[commit])
                records += done[commit]["edits"]
                continue
            t = time.time()
            subject = _git(root, "log", "-1", "--format=%s", commit).strip()[:80]
            wt.goto(f"{commit}^", commit)
            recs = [evaluate_edit(cfg, wt.old, wt.new, rel, _show(root, f"{commit}^", rel), _show(root, commit, rel), harvest) for rel in files]
            records += recs
            per_commit.append({"commit": commit[:10], "subject": subject, "edits": recs, "seconds": round(time.time() - t, 1)})
            per_commit[-1]["commit"] = commit
            log(f"[{i}/{len(commits)}] {commit[:8]} {len(files)} files, wakes {sum(r['wake'] for r in recs)}, asks {sum(r['ask'] for r in recs)}, {per_commit[-1]['seconds']}s: {subject}")
            out_path.write_text(json.dumps({"summary": summarize(records), "commits": per_commit}, indent=1))
    result = {"summary": summarize(records), "commits": per_commit}
    out_path.write_text(json.dumps(result, indent=1))
    return result


def planted(cfg, bugs_file: Path, harvest: dict, out_path: Path, log=print) -> dict:
    root = Path(cfg.root)
    spec = json.loads(bugs_file.read_text())
    rows = []
    with Worktrees(root) as wt:
        base = _git(root, "rev-parse", spec["base_commit"]).strip()
        for bug in spec["bugs"]:
            wt.goto(base, base)
            target = wt.new / bug["file"]
            before = target.read_text()
            if bug["old"] not in before:
                rows.append({**bug, "applied": False, "flagged": False, "proved": False, "statuses": []})
                continue
            after = before.replace(bug["old"], bug["new"], 1)
            target.write_text(after)
            rec = evaluate_edit(cfg, wt.old, wt.new, bug["file"], before, after, harvest)
            statuses = [f["status"] for f in rec["functions"]]
            proved = any(s in (ledger.DIFFERENCE, ledger.FAILURE) for s in statuses)
            flagged = bool(rec["ask"] or (statuses and any(s != ledger.VERIFIED for s in statuses)))
            rows.append({"id": bug["id"], "what": bug["what"], "file": bug["file"], "applied": True, "flagged": flagged, "proved": proved, "functions": rec["functions"], "breaks": rec["breaks"]})
            log(f"{bug['id']} flagged={flagged} proved={proved} {[(f['qualname'], f['status'], f['reason'][:50]) for f in rec['functions']]}")
            target.write_text(before)
    applied = [r for r in rows if r["applied"]]
    result = {"bugs": rows, "applied": len(applied), "catch_rate_any_non_pass": round(sum(r["flagged"] for r in applied) / len(applied), 3) if applied else None,
              "catch_rate_proved_by_execution": round(sum(r["proved"] for r in applied) / len(applied), 3) if applied else None}
    out_path.write_text(json.dumps(result, indent=1))
    return result
