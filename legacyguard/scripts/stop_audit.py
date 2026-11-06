#!/usr/bin/env python3
"""Stop: session audit, block at most once, write the report (FR-18, FR-19)."""
from __future__ import annotations

from _common import clip, load_guard, log_error, run_hook


def handle(event: dict):
    root, cfg = load_guard(event)
    if cfg is None:
        return None
    from guardlib import audit, ledger, report
    with ledger.locked(cfg.state_dir) as data:
        unresolved = audit.run(cfg, data)
        if unresolved and not event.get("stop_hook_active"):
            data["stop_blocked_for"] += [f["id"] for f in unresolved]
            return {"decision": "block", "reason": clip(audit.block_reason(unresolved))}
        path = report.write(data, cfg)
        data["report"] = str(path)
        message = report.summary(data, cfg, path)
        try:  # the map is a second read; it must never cost the developer the report
            from guardlib import sessionmap
            message += f" Map: {sessionmap.write(cfg, data)}"
        except Exception as exc:  # noqa: BLE001
            log_error(root, f"stop_audit: session map failed: {exc!r}")
        return {"systemMessage": message}


if __name__ == "__main__":
    run_hook(handle)
