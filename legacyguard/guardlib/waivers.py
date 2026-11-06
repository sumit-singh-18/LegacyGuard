"""Waivers in .legacyguard/waivers.json, a committable audit trail (FR-24)."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path


def _path(cfg) -> Path:
    return cfg.state_dir / "waivers.json"


def load(cfg) -> list[dict]:
    try:
        return json.loads(_path(cfg).read_text())
    except (OSError, ValueError):
        return []


def is_waived(cfg, kind: str, file: str | None, item: str, code_hash: str | None) -> bool:
    """A waiver holds only while the code hash matches; changed code asks again."""
    return any(w.get("kind") == kind and w.get("file") == file and w.get("item") == item and w.get("code_hash") == code_hash for w in load(cfg))


def add(cfg, finding: dict, reason: str) -> dict:
    entry = {"id": finding["id"], "kind": finding["kind"], "reason": reason, "file": finding.get("file"), "symbol": finding.get("subject"),
             "item": finding.get("item"), "code_hash": finding.get("code_hash"), "date": dt.date.today().isoformat()}
    data = load(cfg)
    data.append(entry)
    tmp = _path(cfg).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, _path(cfg))
    return entry
