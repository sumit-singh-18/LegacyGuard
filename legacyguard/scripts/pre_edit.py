#!/usr/bin/env python3
"""PreToolUse on Edit|Write: scope, frozen, virtual apply, contract and test checks, decision (FR-12)."""
from __future__ import annotations

from pathlib import Path

from _common import hook_output, load_guard, run_hook

EVENT = "PreToolUse"
MANY_CALLERS = 5
STRONG_COUPLING = 0.5
GUARD_STATE = ("Files under .legacyguard/ are controlled by the developer through /legacyguard commands, "
               "so this edit was denied. The developer can change them directly.")


def _read_migrations(cfg, rel, before, after):
    from guardlib.contracts_parse import dangerous_statements  # noqa: F401 - keeps imports lazy for speed
    folder = Path(cfg.root) / cfg.contract_path("db")
    files = sorted(p for p in folder.iterdir() if p.suffix == ".sql") if folder.is_dir() else []
    target = Path(cfg.root) / rel
    scripts_before = [before if p == target else p.read_text() for p in files]
    if not rel.endswith(".sql"):
        return scripts_before, scripts_before
    names = sorted({p.name for p in files} | {target.name})
    by = {p.name: p.read_text() for p in files}
    by[target.name] = after
    return scripts_before, [by[n] for n in names]


def _http_map(cfg):
    from guardlib import contracts_parse as cp
    rel = cfg.contract_path("http")
    try:
        return cp.http_items((Path(cfg.root) / rel).read_text()) if rel else {}
    except Exception:  # noqa: BLE001 - never block on our own parse
        return {}


def _risk_context(cfg, rel, before, after, data):
    """Risk signals go to Claude as context, never as a prompt (D-013). Once per symbol per session."""
    from guardlib import impact, symbol_diff
    from indexer import db
    if not db.exists(cfg.root) or not rel.endswith(".py"):
        return None
    changes = [c for c in symbol_diff.diff(before, after) if c.kind != "added" and c.before is not None and c.before.kind != "class"]
    if not changes:
        return None
    conn = db.connect(cfg.root)
    try:
        lines = []
        for change in changes[:3]:
            key = f"{rel}::{change.qualname}"
            if key in data["context_sent"]:
                continue
            imp = impact.compute(conn, cfg, key)
            if imp is None:
                continue
            signals = []
            if len(imp.callers) >= MANY_CALLERS:
                signals.append("many_callers")
            if imp.coverage_known and not imp.tests:
                signals.append("untested")
            if any(c["confidence"] >= STRONG_COUPLING for c in imp.cochange):
                signals.append("history_coupling")
            if change.kind in ("signature_changed", "deleted", "renamed", "visibility_changed") and imp.callers:
                signals.append(change.kind)
            if signals:
                data["context_sent"].append(key)
                lines.append(f"Risk signals for {change.qualname}: {', '.join(signals)}.\n{impact.render(imp, 700)}")
        return "\n".join(lines) or None
    finally:
        conn.close()


def handle(event: dict):
    root, cfg = load_guard(event)
    if cfg is None:
        return None
    from guardlib import baseline, checks, ledger, paths, policy, symbols, virtual, waivers

    tool_input = event.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path:
        return None
    rel = paths.relative_to_root(file_path, root)

    if rel is not None and (rel == ".legacyguard" or rel.startswith(".legacyguard/")):
        return hook_output(EVENT, decision="deny", reason=GUARD_STATE)

    if not paths.in_scope(rel, cfg.scope):
        with ledger.locked(cfg.state_dir) as data:
            if file_path in data["outside_notified"]:
                return None
            data["outside_notified"].append(file_path)
        return hook_output(EVENT, system_message=f"LegacyGuard: {paths.norm(file_path)} is outside the guarded scope ({cfg.scope}), so it was not checked.")

    if paths.matches_any(rel, cfg.raw.get("frozen") or []):
        with ledger.locked(cfg.state_dir) as data:
            f, _ = ledger.add_finding(data, kind="frozen", status=ledger.FAILURE, source="rule", subject=f"frozen file edit attempted: {rel}", file=rel,
                                      evidence="Denied by the frozen list in .legacyguard/config.yaml; the edit did not happen.", item=rel, state="blocked")
        return hook_output(EVENT, decision="deny",
                           reason=f"[{f['id']}] {rel} is on this repo's frozen list in .legacyguard/config.yaml, so it cannot be edited. "
                                  "Other files that use it can change, as long as they keep calling it as it is.")

    current = virtual.read_current(file_path)
    after = virtual.apply(event.get("tool_name", ""), tool_input, current)
    if after is None:
        return None  # the tool will fail by itself: old_string missing or not unique

    notes = []
    if rel.endswith(".py") and symbols.has_errors(after) and not symbols.has_errors(current or ""):
        notes.append(f"LegacyGuard: the new content of {rel} does not parse as Python.")

    breaks = checks.run_checks(cfg, rel, current, after, _http_map(cfg), lambda r, b, a: _read_migrations(cfg, r, b, a))
    code_hash = symbols.sha(after)
    with ledger.locked(cfg.state_dir) as data:
        data["edits"] += 1
        if not breaks:
            ctx = _risk_context(cfg, rel, current, after, data)
            text = "\n".join(notes + ([ctx] if ctx else []))
            return hook_output(EVENT, context=text) if text else None

        waived = all(waivers.is_waived(cfg, "contract_break", rel, b.item, code_hash) for b in breaks)
        decision = policy.contract_decision(cfg.mode, data["prompts_used"], cfg.budget, waived)
        ids = []
        for b in breaks:
            f, _ = ledger.add_finding(data, kind="contract_break", status=ledger.FAILURE, source=b.source, subject=b.subject, file=rel,
                                      evidence=b.text(), item=b.item, code_hash=code_hash, extra={"contract_kind": b.kind, "pending_hash": code_hash})
            f["last_decision"] = decision.action if not decision.over_budget else "over_budget"
            ids.append(f["id"])
        if decision.prompt_used:
            data["prompts_used"] += 1
        used, budget = data["prompts_used"], cfg.budget

    listing = "; ".join(f"[{i}] {b.subject}" for i, b in zip(ids, breaks))
    label = "Test integrity" if all(b.kind == "test" for b in breaks) else "Contract break"
    context = f"{label} in {rel}: {listing}. Contract file: {breaks[0].contract}."
    if decision.action == "ask":
        return hook_output(EVENT, decision="ask", context=f"{context} The developer is asked to approve or reject this edit. If it is rejected, keep the existing behavior.",
                           reason=f"{label}: {listing}. Prompt {used} of {budget}. To accept it for good: /legacyguard:waive {ids[0]} \"<reason>\"")
    if decision.action == "deny":
        return hook_output(EVENT, decision="deny", context=context,
                           reason=f"{label}: {listing}. Strict mode denies this until the developer runs /legacyguard:waive {ids[0]} \"<reason>\".")
    if decision.over_budget:
        return hook_output(EVENT, context=f"{context} This session's prompt budget is used up, so the edit was not stopped and the finding goes to the session report.")
    return hook_output(EVENT, context=context) if decision.include_context else None


if __name__ == "__main__":
    run_hook(handle)
