#!/usr/bin/env python3
"""UserPromptSubmit: blast-radius context for names in the prompt (FR-11). Never modifies or blocks the prompt."""
from __future__ import annotations

import re

from _common import hook_output, load_guard, run_hook

MAX_SYMBOLS = 3
MAX_PROMPTS_STORED = 20


def named_symbols(conn, prompt: str) -> list[str]:
    """Indexed files and functions the prompt names. Two or more consecutive words of a name count, so 'late fee' finds compute_late_fee."""
    lowered = prompt.lower()
    words = re.sub(r"[^a-z0-9_]+", " ", lowered)
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", prompt))
    picked: list[str] = []
    rows = conn.execute("SELECT DISTINCT file, qualname, name, kind FROM symbols WHERE kind != 'class'").fetchall()
    files = {r["file"] for r in rows}
    named_files = [f for f in files if f in prompt or (f.rsplit("/", 1)[-1] in prompt)]
    for r in rows:
        if r["name"] in tokens and len(r["name"]) >= 4 and not r["name"].startswith("test"):
            picked.append(f"{r['file']}::{r['qualname']}")
    if not picked:
        for r in rows:
            parts = [p for p in r["name"].lower().split("_") if p]
            for size in range(len(parts), 1, -1):
                if any(f" {' '.join(parts[i:i + size])} " in f" {words} " for i in range(len(parts) - size + 1)):
                    picked.append(f"{r['file']}::{r['qualname']}")
                    break
    for f in sorted(named_files):
        for r in rows:
            if r["file"] == f and "." not in r["qualname"] and not r["name"].startswith("_"):
                picked.append(f"{f}::{r['qualname']}")
    return list(dict.fromkeys(picked))


def handle(event: dict):
    root, cfg = load_guard(event)
    if cfg is None:
        return None
    from guardlib import impact, ledger
    from indexer import db
    prompt = event.get("prompt") or ""
    if prompt.lstrip().startswith("/"):
        return None  # a slash command is the developer using a tool, not a change request; its words are not code names
    with ledger.locked(cfg.state_dir) as data:
        data["prompt_text"] = (data["prompt_text"] + [prompt])[-MAX_PROMPTS_STORED:]
    if not db.exists(cfg.root):
        return hook_output("UserPromptSubmit", context="LegacyGuard has no index yet, so no blast-radius data is available. The developer can run /legacyguard:index.")
    conn = db.connect(cfg.root)
    try:
        symbols = named_symbols(conn, prompt)
        if not symbols:
            return None
        blocks = [impact.impact_text(conn, cfg, s) for s in symbols[:MAX_SYMBOLS]]
        more = len(symbols) - MAX_SYMBOLS
        text = "LegacyGuard impact data for code this prompt names. Sources are in brackets; name matches can over-match.\n" + "\n".join(blocks)
        if more > 0:
            text += f"\n+{more} more functions. The impact tool answers for any of them."
        return hook_output("UserPromptSubmit", context=text)
    finally:
        conn.close()


if __name__ == "__main__":
    run_hook(handle)
