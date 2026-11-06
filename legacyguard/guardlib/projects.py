"""Which project a hook or command is about, when Claude was started one folder above the projects (FR-38, D-045).

A developer who keeps several repos in one folder and opens Claude there used to get no checks and no notice.
The guard still works per project: each repo below the start folder has its own settings, baseline and findings.
"""
from __future__ import annotations

import os
from pathlib import Path

from guardlib import config as cfgmod
from guardlib import ledger
from guardlib.paths import find_root, norm

MAX_CHILDREN = 5
SKIP = {"node_modules", "venv", "env", "site-packages", "__pycache__", "dist", "build"}


def children(folder: str | Path) -> list[str]:
    """Git repositories directly inside a folder, by name. Hidden folders and dependency folders are skipped."""
    out = []
    try:
        entries = sorted(Path(folder).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for entry in entries:
        if entry.name.startswith(".") or entry.name in SKIP or not entry.is_dir():
            continue
        if (entry / ".git").exists():
            out.append(norm(str(entry)))
    return out


def _existing_dir(path: str) -> str:
    p = Path(path)
    for candidate in (p.parent, *p.parent.parents):
        if candidate.is_dir():
            return str(candidate)
    return str(p.parent)


def for_file(cwd: str, file_path: str | None, session_id: str | None):
    """(root, Config or None) for an edit. The file's own repo decides, not the folder Claude was started in.

    A repo other than the start folder's is used only if this session started the guard there; an old baseline
    from another session would compare against the wrong code."""
    cwd_root = find_root(cwd)
    root = find_root(_existing_dir(file_path)) if file_path and os.path.isabs(str(file_path)) else cwd_root
    try:
        cfg = cfgmod.load_config(root)
    except (cfgmod.ConfigMissing, cfgmod.ConfigError):
        return root, None
    if root != cwd_root and ledger.load(cfg.state_dir).get("session_id") != session_id:
        return root, None
    return root, cfg


def active(cwd: str, session_id: str | None) -> list:
    """Configs this session is guarding: the start folder's own project, else the projects started inside it."""
    root = find_root(cwd)
    try:
        return [cfgmod.load_config(root)]
    except cfgmod.ConfigError:
        return []
    except cfgmod.ConfigMissing:
        pass
    out = []
    for child in children(root):
        try:
            cfg = cfgmod.load_config(child)
        except (cfgmod.ConfigMissing, cfgmod.ConfigError):
            continue
        if ledger.load(cfg.state_dir).get("session_id") == session_id:
            out.append(cfg)
    return out


def for_command(start: str):
    """(Config, note) for a slash command. From a folder of projects, the one this guard touched last."""
    root = find_root(start)
    try:
        return cfgmod.load_config(root), None
    except cfgmod.ConfigMissing:
        pass
    found = []
    for child in children(root):
        try:
            cfg = cfgmod.load_config(child)
        except (cfgmod.ConfigMissing, cfgmod.ConfigError):
            continue
        state = cfg.state_dir / "session.json"
        found.append((state.stat().st_mtime if state.exists() else 0.0, cfg))
    if not found:
        raise cfgmod.ConfigMissing(str(cfgmod.config_path(root)))
    found.sort(key=lambda mc: -mc[0])
    cfg = found[0][1]
    names = ", ".join(Path(c.root).name for _, c in found)
    note = None if len(found) == 1 else (f"Several guarded projects are in this folder ({names}). This is {Path(cfg.root).name}, the one used most recently. "
                                         "Run the command from inside a project to pick another.")
    return cfg, note
