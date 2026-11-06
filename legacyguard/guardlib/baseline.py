"""Session baseline (D-025, D-035): a worktree at HEAD plus the session-start contents of dirty files."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from guardlib import paths

STATE_GITIGNORE = """# Written by LegacyGuard. config.yaml and waivers.json stay committable.
baseline/
index.db
index.db-*
session.json
.session.*
guard.log
coverage.db
coverage.db-*
harvest.json
reports/
.hypothesis/
"""


class BaselineError(Exception):
    pass


def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)
    if check and res.returncode:
        raise BaselineError(res.stderr.strip() or f"git {args[0]} failed")
    return res


def baseline_dir(cfg) -> Path:
    return cfg.state_dir / "baseline"


def ensure_state_dir(cfg) -> None:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    gi = cfg.state_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(STATE_GITIGNORE)


def remove(cfg) -> None:
    target = baseline_dir(cfg)
    _git(cfg.root, "worktree", "remove", "--force", str(target), check=False)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    _git(cfg.root, "worktree", "prune", check=False)


def _dirty_entries(root: str) -> list[tuple[str, str]]:
    """(status, path) for tracked changes and untracked files, from git status."""
    res = _git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    tokens = res.stdout.split("\0")
    out, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token:
            continue
        code, path = token[:2], token[3:]
        if "R" in code or "C" in code:
            old = tokens[i]
            i += 1
            if "R" in code:
                out.append(("D", old))
        out.append(("D" if "D" in code else "M", path))
    return out


def create(cfg) -> dict:
    """Recreate the baseline. Returns {commit, dirty_files}. Raises BaselineError for a non-git repo or no commits."""
    root = cfg.root
    ensure_state_dir(cfg)
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode:
        raise BaselineError("not a git repository")
    head = _git(root, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode:
        raise BaselineError("the repository has no commits yet")
    remove(cfg)
    target = baseline_dir(cfg)
    _git(root, "worktree", "add", "--detach", "--quiet", str(target), "HEAD")
    dirty = []
    for status, rel in _dirty_entries(root):
        if rel.startswith(".legacyguard/") or not paths.in_scope(rel, cfg.scope):
            continue
        src, dst = Path(root) / rel, target / rel
        if status == "D":
            dst.unlink(missing_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        dirty.append(rel)
    return {"commit": head.stdout.strip(), "dirty_files": sorted(set(dirty))}


def changed_files(cfg) -> list[str]:
    """In-scope files that differ between the session baseline and the working tree, found by content."""
    target = baseline_dir(cfg)
    if not target.exists():
        return []
    res = _git(cfg.root, "ls-files", "-co", "--exclude-standard")
    now = {f for f in res.stdout.splitlines() if not f.startswith(".legacyguard/") and paths.in_scope(f, cfg.scope)}
    res = _git(str(target), "ls-files", "-co", "--exclude-standard")
    then = {f for f in res.stdout.splitlines() if not f.startswith(".legacyguard/") and paths.in_scope(f, cfg.scope)}
    out = []
    for rel in sorted(now | then):
        a, b = target / rel, Path(cfg.root) / rel
        if a.is_file() != b.is_file():
            out.append(rel)
        elif a.is_file() and (os.path.getsize(a) != os.path.getsize(b) or a.read_bytes() != b.read_bytes()):
            out.append(rel)
    return out


def read_baseline(cfg, rel: str) -> str | None:
    p = baseline_dir(cfg) / rel
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return None
