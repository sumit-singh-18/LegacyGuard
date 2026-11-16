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
SLOWDOWN = "Slowdown — not a proof of a wrong result"  # Task 6.6: an input timed out only in the new code
STATUSES = (VERIFIED, FAILURE, DIFFERENCE, RISK, UNTESTED, SLOWDOWN)
SOURCES = ("names", "tests", "routes", "schema", "history", "diff", "rule")


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / "session.json"


def fresh(session_id: str | None) -> dict:
    return {"session_id": session_id, "started": dt.datetime.now().isoformat(timespec="seconds"), "baseline_commit": None, "dirty_files": [],
            "baseline_ok": False, "prompt_text": [], "prompts_used": 0, "wakeups": 0, "next_id": 1, "findings": [], "stop_blocked_for": [],
            "outside_notified": [], "context_sent": [], "edits": 0, "report": None, "callers_run": {},
            "callers_unrunnable": {}, "callers_skipped": {}}


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


def note_caller_outcome(data: dict, key: str, own_hash: str | None, unrunnable: bool, reason: str) -> None:
    """Session memory of callers that could not be run, keyed by file::qualname and the caller's own code hash
    (roadmap 6.5). `unrunnable` must mean the cause is on the baseline side (diff.Result.baseline_unrunnable: every
    input raised or failed setup on the code before the session), never something the edit did. Any other outcome
    for the caller forgets it."""
    memory = data.setdefault("callers_unrunnable", {})
    if unrunnable and own_hash:
        memory[key] = {"hash": own_hash, "reason": (reason or "it could not be run")[:160]}
    else:
        memory.pop(key, None)


def unrunnable_caller(memory: dict | None, key: str, own_hash: str) -> str:
    """The remembered reason a caller could not be run, while its code is unchanged; "" otherwise."""
    entry = (memory or {}).get(key)
    if not entry or entry.get("hash") != own_hash:
        return ""
    return entry.get("reason") or "it could not be run"


def find(data: dict, finding_id: str) -> dict | None:
    wanted = finding_id.strip().upper()
    return next((f for f in data["findings"] if f["id"].upper() == wanted), None)
