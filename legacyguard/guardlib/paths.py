"""Path helpers. Pure string operations so Windows paths can be tested on any OS."""
from __future__ import annotations

import fnmatch
import os
import posixpath
import subprocess
from pathlib import Path


def norm(path: str) -> str:
    """Forward slashes, no redundant parts, lower-case drive letter."""
    p = posixpath.normpath(str(path).replace("\\", "/"))
    if len(p) > 1 and p[1] == ":":
        p = p[0].lower() + p[1:]
    return p


def resolved(path: str) -> str:
    """Normalize, resolving symlinks only for native paths (macOS /tmp is a symlink)."""
    if "\\" not in str(path) and os.path.isabs(str(path)):
        return norm(os.path.realpath(path))
    return norm(path)


def relative_to_root(path: str, root: str) -> str | None:
    """Repo-relative path, or None if the path is outside the root."""
    p, r = resolved(path), resolved(root)
    if p == r:
        return "."
    prefix = r.rstrip("/") + "/"
    return p[len(prefix):] if p.startswith(prefix) else None


def in_scope(rel: str | None, scope: str) -> bool:
    if rel is None:
        return False
    s = norm(scope).rstrip("/")
    return s in ("", ".") or rel == s or rel.startswith(s + "/")


def matches_any(rel: str | None, patterns: list[str]) -> bool:
    if rel is None:
        return False
    return any(rel == norm(p) or fnmatch.fnmatch(rel, norm(p)) for p in patterns)


def git_root(start: str) -> str | None:
    try:
        res = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return norm(res.stdout.strip()) if res.returncode == 0 and res.stdout.strip() else None


def find_root(cwd: str) -> str:
    """Nearest ancestor holding .legacyguard/config.yaml, without leaving the git repo cwd is in; else that repo's top, else cwd.

    The search stops at the first folder with a .git entry: a config above it belongs to a different repo, and a repo
    cloned inside a guarded (or switched-off) one must be judged on its own.
    """
    cur = Path(os.path.realpath(cwd))
    for candidate in (cur, *cur.parents):
        if (candidate / ".legacyguard" / "config.yaml").exists() or (candidate / ".git").exists():
            return norm(str(candidate))
    return git_root(str(cur)) or norm(str(cur))
