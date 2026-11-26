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
        skipped: list = []  # callers left out of the budget, named in the note (roadmap 6.5)
        targets = engine.targets_for(cfg, rel, data, skipped=skipped)
        base_cached = data.get("migration_drift_base")  # Sprint 7.6: read here, used outside the lock
        oas_state = data.get("app_openapi")  # Sprint 7.6 Part B: the code-first spec's summary, read here too
        data_snapshot = {"baseline_commit": data.get("baseline_commit")}
    from guardlib import app_openapi, migration_drift
    drift_wanted = migration_drift.applies(cfg, rel, text)
    oas_wanted = app_openapi.applies(cfg, rel, oas_state, data_snapshot)
    if not targets and not drift_wanted and not oas_wanted:
        return None
    import time
    from runner import scratch
    drift = oas = None
    oas_wake: list = []
    oas_text = None
    results, texts = [], []
    # Task 7.4: one throwaway working folder per tree for the drift check, the diff and the affected tests together;
    # the baseline's folder is the session's cached one (Part B), so only the working tree is cloned per check.
    with scratch.for_trees(cfg, str(baseline.baseline_dir(cfg)), root, session_cache=True):
        if drift_wanted:
            # Sprint 7.6: Django model drift runs project code, so here and not in PreToolUse; never under the ledger
            # lock. It gives a context note only (D2): the Stop audit turns drift still there into a finding.
            try:
                drift = migration_drift.compute(cfg, str(baseline.baseline_dir(cfg)), root, base_cached or {}, data=data_snapshot)
            except Exception as exc:  # noqa: BLE001 - hooks fail open
                from _common import log_error
                log_error(root, f"migration drift: {type(exc).__name__}: {exc}")
                drift = None
        if oas_wanted:
            # Sprint 7.6 Part B: the spec the app generates, baseline against now. It runs project code, so here and not
            # in PreToolUse, never under the ledger lock, within one deadline (SPEC_BUDGET_S). The baseline cache file is
            # written right away, outside the lock; session.json gets its summary in the store block below.
            try:
                oas = app_openapi.compute(cfg, str(baseline.baseline_dir(cfg)), root, oas_state, data_snapshot,
                                          deadline=time.time() + app_openapi.SPEC_BUDGET_S)
                app_openapi.write_cache(cfg, oas)
            except Exception as exc:  # noqa: BLE001 - hooks fail open
                from _common import log_error
                log_error(root, f"app openapi: {type(exc).__name__}: {exc}")
                oas = None
        if targets:
            results = differ.run(cfg, str(baseline.baseline_dir(cfg)), root, targets)
        if virtual.read_current(file_path) != text:
            # A newer edit landed while this ran; its own check covers the current code. The spec cache file may already
            # be written but its session.json summary is not: the next check sees no summary and rebuilds the baseline
            # (wasteful, never wrong).
            return None
        drift_text, drift_kind = migration_drift.context_text(drift)
        wake = []
        with ledger.locked(cfg.state_dir) as data:
            from guardlib import contract_tools
            if drift is not None:
                if drift.base_result:
                    data["migration_drift_base"] = drift.base_result
                migration_drift.record_run(data, drift)
                if drift_text and drift_kind:
                    drift_text = (contract_tools.take_notes(data, [(drift_kind, drift_text)]) or [None])[0]
            if oas is not None:
                # Whatever the targets: the summary and failure memory, the once-per-session notes, and the wake decision,
                # made from the session.json read under this lock, so of two overlapping hooks only the first wakes.
                shown = contract_tools.take_notes(data, app_openapi.record_run(data, oas))
                oas_wake = app_openapi.wake_items(oas, data.get("app_openapi_seen") or [])
                data.setdefault("app_openapi_seen", []).extend(oas_wake)
                oas_text = "\n".join(x for x in [app_openapi.context_text(oas, oas_wake)] + shown if x) or None
            if targets:
                wake = engine.record(cfg, data, results)
                engine.record_isolation(data)
            if wake or oas_wake:
                data["wakeups"] += 1
        if targets:
            texts = [differ.format_wake(r, f["id"]) for f, r in wake[:2]]
            # The project's own tests that reach the changed code, baseline against now (roadmap C4).
            try:
                texts += engine.affected_tests(cfg, rel, targets, file_hash)[:2]
            except Exception as exc:  # noqa: BLE001 - a test run that cannot start must not hide the diff's result
                from _common import log_error
                log_error(root, f"affected tests: {type(exc).__name__}: {exc}")
    skipped_text = engine.skipped_note(skipped)
    if oas_wake:  # Sprint 7.6 Part B (D3): a new breaking change in the generated spec wakes, with or without targets
        raise Wake("\n\n".join(texts + [app_openapi.wake_text(oas, oas_wake)]
                                + [x for x in (oas_text, drift_text, skipped_text) if x]))
    if not targets:
        context = "\n".join(x for x in (oas_text, drift_text) if x)
        return hook_output("PostToolUse", context=context) if context else None
    if texts:  # the drift text never wakes by itself (D2); it rides along with a wake
        raise Wake("\n\n".join(texts + [x for x in (oas_text, drift_text, skipped_text) if x]))
    # No difference: Claude is told what was run and what that does and does not show, as context only (D-045).
    # Without it Claude cannot know the edit was checked, and repeats the work or assumes nothing looked.
    note = "\n".join(x for x in (differ.format_note(results), oas_text, drift_text, skipped_text) if x)
    return hook_output("PostToolUse", context=note) if note else None

if __name__ == "__main__":
    run_hook(handle)
