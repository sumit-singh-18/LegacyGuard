"""Findings ledger in .legacyguard/session.json (FR-20). Hooks are separate processes, so writes take a file lock."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: accept the small race; the ledger is advisory state
    fcntl = None

VERIFIED = "Verified pass"
FAILURE = "Confirmed failure"
DIFFERENCE = "Behavior difference — intent unknown"
RISK = "Potential risk (static only)"
UNTESTED = "Untested"
STATUSES = (VERIFIED, FAILURE, DIFFERENCE, RISK, UNTESTED)
SOURCES = ("names", "tests", "routes", "schema", "history", "diff", "rule")


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / "session.json"


def fresh(session_id: str | None) -> dict:
    return {"session_id": session_id, "started": dt.datetime.now().isoformat(timespec="seconds"), "baseline_commit": None, "dirty_files": [],
            "baseline_ok": False, "prompt_text": [], "prompts_used": 0, "wakeups": 0, "next_id": 1, "findings": [], "stop_blocked_for": [],
            "outside_notified": [], "context_sent": [], "edits": 0, "report": None}


def load(state_dir: Path) -> dict:
    try:
        return json.loads(_path(state_dir).read_text())
    except (OSError, ValueError):
        return fresh(None)


@contextlib.contextmanager
def locked(state_dir: Path):
    """Read, let the caller change, write atomically, all under one lock."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / ".session.lock", "w") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(state_dir)
        yield data
        tmp = state_dir / f".session.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        os.replace(tmp, _path(state_dir))


def add_finding(data: dict, *, kind: str, status: str, source: str, subject: str, file: str | None, evidence: str, item: str,
                code_hash: str | None = None, state: str = "open", extra: dict | None = None) -> tuple[dict, bool]:
    """Record a finding once. Identity is (kind, file, item, code hash), so changed code is a new evaluation."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status}")
    if source not in SOURCES:
        raise ValueError(f"unknown source {source}")
    key = f"{kind}|{file}|{item}|{code_hash}"
    for f in data["findings"]:
        if f["key"] == key:
            return f, False
    finding = {"id": f"LG-{data['next_id']}", "key": key, "kind": kind, "status": status, "source": source, "subject": subject, "file": file,
               "item": item, "evidence": evidence, "code_hash": code_hash, "state": state,
               "time": dt.datetime.now().isoformat(timespec="seconds"), **(extra or {})}
    data["next_id"] += 1
    data["findings"].append(finding)
    return finding, True


def find(data: dict, finding_id: str) -> dict | None:
    wanted = finding_id.strip().upper()
    return next((f for f in data["findings"] if f["id"].upper() == wanted), None)
