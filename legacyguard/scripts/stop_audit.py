#!/usr/bin/env python3
"""Stop: session audit, block at most once, write the report (FR-18, FR-19). One pass per guarded project (FR-38)."""
from __future__ import annotations

from _common import active_guards, clip, log_error, run_hook


def handle(event: dict):
    guards = active_guards(event)
    if not guards:
        return None
    from guardlib import audit, ledger, report
    blocks, messages = [], []
    for cfg in guards:
        with ledger.locked(cfg.state_dir) as data:
            unresolved = audit.run(cfg, data)
            if unresolved and not event.get("stop_hook_active"):
                data["stop_blocked_for"] += [f["id"] for f in unresolved]
                blocks.append(audit.block_reason(unresolved))
                continue
            if len(guards) > 1 and not data["findings"] and not data["edits"]:
                continue  # a project in this folder that the session never touched needs no report
            path = report.write(data, cfg)
            data["report"] = str(path)
            message = report.summary(data, cfg, path)
            try:  # the map is a second read; it must never cost the developer the report
                from guardlib import sessionmap
                message += f" Map: {sessionmap.write(cfg, data)}"
            except Exception as exc:  # noqa: BLE001
                log_error(cfg.root, f"stop_audit: session map failed: {exc!r}")
            messages.append(message)
    if blocks:
        return {"decision": "block", "reason": clip("\n\n".join(blocks))}
    return {"systemMessage": " ".join(messages)} if messages else None


if __name__ == "__main__":
    run_hook(handle)
