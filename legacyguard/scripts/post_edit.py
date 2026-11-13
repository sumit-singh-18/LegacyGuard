#!/usr/bin/env python3
"""PostToolUse on Edit|Write, async with asyncRewake: behavioral diff; exit 2 wakes Claude (FR-15, FR-16).

Success stays in session.json and appears at Stop and in /legacyguard:status (D-013 note in OQ-B5 evidence).
"""
from __future__ import annotations

from pathlib import Path

from _common import Wake, hook_output, load_guard, run_hook


def handle(event: dict):
    file_path = (event.get("tool_input") or {}).get("file_path")
    root, cfg = load_guard(event, file_path)
    if cfg is None:
        return None
    from guardlib import baseline, engine, ledger, paths, symbols, virtual
    from runner import diff as differ
    rel = paths.relative_to_root(file_path, root) if file_path else None
    if rel is None:
        return None
    text = virtual.read_current(file_path)
    file_hash = symbols.sha(text) if text is not None else None
    with ledger.locked(cfg.state_dir) as data:
        for f in data["findings"]:  # a contract break whose edit went through was approved or over budget
            if f.get("pending_hash") and f.get("pending_hash") == file_hash and f["state"] == "open" and f["kind"] == "contract_break":
                f["state"] = "applied"
        if not data.get("baseline_ok") or not engine.is_guarded_source(cfg, rel):
            return None
        targets = engine.targets_for(cfg, rel, data)
    if not targets:
        return None
    results = differ.run(cfg, str(baseline.baseline_dir(cfg)), root, targets)
    if virtual.read_current(file_path) != text:
        return None  # a newer edit landed while this ran; its own check covers the current code
    with ledger.locked(cfg.state_dir) as data:
        wake = engine.record(cfg, data, results)
        if wake:
            data["wakeups"] += 1
    texts = [differ.format_wake(r, f["id"]) for f, r in wake[:2]]
    # The project's own tests that reach the changed code, baseline against now (roadmap C4).
    try:
        texts += engine.affected_tests(cfg, rel, targets, file_hash)[:2]
    except Exception as exc:  # noqa: BLE001 - a test run that cannot start must not hide the diff's result
        from _common import log_error
        log_error(root, f"affected tests: {type(exc).__name__}: {exc}")
    if texts:
        raise Wake("\n\n".join(texts))
    # No difference: Claude is told what was run and what that does and does not show, as context only (D-045).
    # Without it Claude cannot know the edit was checked, and repeats the work or assumes nothing looked.
    note = differ.format_note(results)
    return hook_output("PostToolUse", context=note) if note else None


if __name__ == "__main__":
    run_hook(handle)
