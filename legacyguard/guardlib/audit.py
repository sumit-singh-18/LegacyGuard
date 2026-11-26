"""The whole-session audit behind the Stop hook (FR-18). Compares the working tree with the baseline, so shell edits count."""
from __future__ import annotations

from pathlib import Path

from guardlib import baseline, checks, contracts_parse as cp, engine, ledger, paths, symbol_diff, symbols, virtual, waivers
from indexer import db as idb
from runner import diff as differ
from runner import scratch

BLOCKING_STATUS = (ledger.FAILURE, ledger.DIFFERENCE)


def _baseline_http(cfg) -> dict:
    rel = cfg.contract_path("http")
    text = baseline.read_baseline(cfg, rel) if rel else None
    try:
        return cp.http_items(text) if text else {}
    except Exception:  # noqa: BLE001 - never block on our own parse
        return {}


def _migrations(cfg):
    folder = cfg.contract_path("db")

    def reader(rel, before, after):
        def scripts(base: Path):
            if not folder:  # a Django migration edited with no contracts.db configured (found in Sprint 7.6)
                return []
            d = base / folder
            return [p.read_text() for p in sorted(d.iterdir()) if p.suffix == ".sql"] if d.is_dir() else []
        return scripts(baseline.baseline_dir(cfg)), scripts(Path(cfg.root))
    return reader


def _contract_findings(cfg, data, changed, drift=None, notes=None, oas=None, tool_state=None):
    http = _baseline_http(cfg)
    reader = _migrations(cfg)
    seen_items = set()
    from guardlib import contract_tools
    trees = (str(baseline.baseline_dir(cfg)), cfg.root)
    timeout = contract_tools.settings(cfg)["timeout_s"]
    notes = notes if notes is not None else []
    static_oas: list = []  # (file, Break) from oasdiff on the static contract file, for the code-first dedupe (D5)
    tool_state = {} if tool_state is None else tool_state  # Sprint 7.6 Part C: one per Stop (squawk's time rule)
    for rel in changed:
        before = baseline.read_baseline(cfg, rel)
        after = virtual.read_current(str(Path(cfg.root) / rel)) or ""
        if paths.matches_any(rel, cfg.raw.get("frozen") or []):
            ledger.add_finding(data, kind="frozen", status=ledger.FAILURE, source="rule", subject=f"frozen file changed outside Edit and Write: {rel}", file=rel,
                               evidence="The working tree differs from the baseline for a file on the frozen list.", item=rel, code_hash=symbols.sha(after))
            continue
        code_hash = symbols.sha(after)
        for b in checks.run_checks(cfg, rel, before, after, http, reader, trees=trees, notes=notes, tool_timeout=timeout,
                                   tool_state=tool_state):
            seen_items.add((rel, b.item))
            if b.source == "oasdiff":
                static_oas.append((rel, b))
            known = next((f for f in data["findings"] if f["kind"] == "contract_break" and f["file"] == rel and f["item"] == b.item), None)
            if known is not None:
                if known["state"] == "not_applied":
                    known["state"] = "open"
                continue
            waived = waivers.is_waived(cfg, "contract_break", rel, b.item, code_hash)
            ledger.add_finding(data, kind="contract_break", status=ledger.FAILURE, source=b.source, subject=b.subject, file=rel,
                               evidence=f"{b.text()}; found by the session audit, so the edit bypassed the pre-write check", item=b.item, code_hash=code_hash,
                               state="waived" if waived else "open", extra={"contract_kind": b.kind})
    _squawk_kept(data, tool_state, seen_items)
    _drift_findings(cfg, data, drift, seen_items)
    _oas_findings(cfg, data, oas, seen_items, static_oas)
    for f in data["findings"]:
        if f["kind"] == "contract_break" and f["state"] in ("open", "applied") and (f["file"], f["item"]) not in seen_items:
            f["state"] = "not_applied"


def _squawk_kept(data, tool_state: dict, seen_items: set) -> None:
    """Sprint 7.6 Part C (D11): a .sql file squawk did not lint at this Stop (missing, timed out, failed, stopped by the
    time rule, or no longer parsing) keeps its open squawk findings out of the not_applied sweep; squawk said nothing
    about them. Switched off (kind `off`) keeps nothing, so the sweep closes them, as for drift."""
    kinds = tool_state.get("squawk_unchecked_kind") or {}
    for f in data["findings"]:
        if (f["kind"] == "contract_break" and f.get("source") == "squawk" and f["state"] in ("open", "applied")
                and kinds.get(f["file"]) not in (None, "off")):
            seen_items.add((f["file"], f["item"]))


def _drift_findings(cfg, data, drift, seen_items) -> None:
    """Sprint 7.6: Django model drift as contract_break findings, before the not_applied sweep. A run that was not
    complete (or only hedged) leaves existing drift findings as they are; drift None (no changed file applies, so
    nothing can be new) lets the sweep close them."""
    from guardlib import migration_drift
    if drift is None:
        return
    if drift.state != "checked":
        for f in data["findings"]:
            if f["kind"] == "contract_break" and f.get("source") == "makemigrations" and f["state"] in ("open", "applied"):
                seen_items.add((f["file"], f["item"]))
        return
    for file, b in drift.breaks:
        seen_items.add((file, b.item))
        known = next((f for f in data["findings"] if f["kind"] == "contract_break" and f["file"] == file and f["item"] == b.item), None)
        if known is not None:
            if known["state"] == "not_applied":
                known["state"] = "open"
            continue
        code_hash = migration_drift.code_hash(b)
        waived = waivers.is_waived(cfg, "contract_break", file, b.item, code_hash)
        ledger.add_finding(data, kind="contract_break", status=ledger.FAILURE, source="makemigrations", subject=b.subject, file=file,
                           evidence=f"{b.subject}; found by makemigrations --check --dry-run (no database), new since the baseline",
                           item=b.item, code_hash=code_hash, state="waived" if waived else "open", extra={"contract_kind": "db"})


def _static_key(b):
    """(oasdiff id, subject) of a static-file oasdiff break: item oasdiff:<id>:<op path>:<fingerprint>."""
    parts = b.item.split(":")
    return (parts[1] if len(parts) > 1 else "", b.subject)


def _oas_findings(cfg, data, oas, seen_items, static_oas) -> None:
    """Sprint 7.6 Part B: the app's generated spec as contract_break findings (source app-openapi), before the
    not_applied sweep. A standing break is FAILURE/open (blocks the Stop once); a hedged one RISK/recorded (never
    blocks); a known finding follows its hedge both ways, waived ones stay waived. Recorded findings no longer reported
    go to not_applied. A not-checked or paused run leaves every app-openapi finding as it is."""
    from guardlib import app_openapi
    mine = [f for f in data["findings"] if f["kind"] == "contract_break" and f.get("source") == app_openapi.SOURCE]
    if oas is not None and oas.state == "not_checked":
        for f in mine:
            if f["state"] in ("open", "applied", "recorded"):
                seen_items.add((f["file"], f["item"]))
        return
    reported = set()
    static = {}
    for rel, b in static_oas:
        static.setdefault(_static_key(b), (rel, b))
    rows = [(file, b, False) for file, b in (oas.breaks if oas is not None else [])]
    rows += [(file, b, True) for file, b in (oas.hedged if oas is not None else [])]
    how = "oasdiff" if oas is not None and oas.via == "oasdiff" else "the built-in parser"
    for file, b, hedged in rows:
        key = b.dedupe_key()
        if key is not None and key in static:  # D5: the same change in the static contract file is reported there
            rel, sb = static[key]
            sf = next((f for f in data["findings"] if f["kind"] == "contract_break" and f["file"] == rel and f["item"] == sb.item), None)
            if sf is not None and "also in the app's generated spec" not in sf["evidence"]:
                sf["evidence"] += "; also in the app's generated spec"
            continue
        seen_items.add((file, b.item))
        reported.add((file, b.item))
        evidence = f"{b.subject}; found by comparing the OpenAPI spec the app generates in the baseline and now ({how})"
        if hedged:
            evidence += f"; {b.hedge}"
        known = next((f for f in data["findings"] if f["kind"] == "contract_break" and f["file"] == file and f["item"] == b.item), None)
        if known is not None:
            if known["state"] == "waived":
                continue
            known["evidence"] = evidence  # rebuilt each run, so the hedge is said exactly when it holds
            if hedged:
                known["status"], known["hedged"] = ledger.RISK, True
                if known["state"] in ("open", "applied", "not_applied"):
                    known["state"] = "recorded"
            else:
                known["status"], known["hedged"] = ledger.FAILURE, False
                if known["state"] in ("recorded", "not_applied"):
                    known["state"] = "open"
            continue
        code_hash = app_openapi.code_hash(b)
        waived = waivers.is_waived(cfg, "contract_break", file, b.item, code_hash)
        ledger.add_finding(data, kind="contract_break", status=ledger.RISK if hedged else ledger.FAILURE, source=app_openapi.SOURCE,
                           subject=b.subject, file=file, evidence=evidence, item=b.item, code_hash=code_hash,
                           state="waived" if waived else ("recorded" if hedged else "open"),
                           extra={"contract_kind": "http", "hedged": hedged})
    for f in mine:
        if f["state"] == "recorded" and (f["file"], f["item"]) not in reported:
            f["state"] = "not_applied"


def _app_openapi(cfg, data, changed, log=None):
    """(outcome, notes) for this Stop. outcome None: no changed file can affect the generated spec. A paused check
    with some guarded source changed gives a not_checked outcome, so findings are left as they are."""
    from guardlib import app_openapi
    try:
        if not app_openapi.enabled(cfg):
            return None, []
        guarded = [rel for rel in changed if engine.is_guarded_source(cfg, rel)]
        if not guarded:
            return None, []
        state = data.get("app_openapi")
        snapshot = {"baseline_commit": data.get("baseline_commit")}
        if app_openapi.is_paused(cfg, state, snapshot):
            oas = app_openapi.paused_outcome(state)
            return oas, list(oas.notes)
        if not any(app_openapi.applies(cfg, rel, state, snapshot) for rel in guarded):
            return None, []
        oas = app_openapi.compute(cfg, str(baseline.baseline_dir(cfg)), cfg.root, state, snapshot)
        app_openapi.write_cache(cfg, oas)
    except Exception as exc:  # noqa: BLE001 - hooks fail open: a failed run leaves the findings unchanged
        if log:
            log(f"app openapi: {type(exc).__name__}: {exc}")
        oas = app_openapi.SpecOutcome("not_checked", notes=[("app-openapi-internal", "LegacyGuard: the app's OpenAPI check "
                                                                                    "failed inside LegacyGuard.")])
    notes = app_openapi.record_run(data, oas)
    context = app_openapi.context_text(oas, [b.item for _, b in oas.breaks])  # hedged and WARN lines for the report
    return oas, notes + ([(None, context)] if context else [])


def _drift(cfg, data, changed, log=None):
    """The drift outcome for this Stop, or None when no changed file can affect it."""
    from guardlib import migration_drift
    root = cfg.root
    if not any(migration_drift.applies(cfg, rel, virtual.read_current(str(Path(root) / rel))) for rel in changed):
        return None
    try:
        drift = migration_drift.compute(cfg, str(baseline.baseline_dir(cfg)), root, data.get("migration_drift_base") or {}, data=data)
    except Exception as exc:  # noqa: BLE001 - hooks fail open: a failed run leaves drift findings unchanged
        if log:
            log(f"migration drift: {type(exc).__name__}: {exc}")
        return migration_drift.DriftOutcome("not_checked", note="LegacyGuard: the migration check failed inside LegacyGuard.",
                                            note_kind="makemigrations-internal")
    if drift.base_result:
        data["migration_drift_base"] = drift.base_result
    migration_drift.record_run(data, drift)
    return drift


def _behavior_findings(cfg, data, changed):
    root = cfg.root
    engine.prune_skipped(data, changed)  # callers skipped for an edit that is no longer in the tree (roadmap 6.5)
    # Task 7.4: one throwaway working folder per tree for the whole audit, prepared only if some check runs; the
    # baseline's folder is the session's cached one (Part B). Sprint 7.6: run() opens the block around the drift
    # check and this; for_trees is re-entrant, so the working tree is cloned at most once per Stop.
    with scratch.for_trees(cfg, str(baseline.baseline_dir(cfg)), root, session_cache=True):
        for rel in changed:
            if not engine.is_guarded_source(cfg, rel):
                continue
            targets = engine.targets_for(cfg, rel, data)
            checked = {(f["item"], f["code_hash"]) for f in data["findings"] if f["file"] == rel and f["kind"] in ("verified", "untested", "behavior_difference", "slowdown")
                       and f["state"] in ("open", "recorded", "waived")}
            todo = [t for t in targets if (t.qualname, t.code_hash) not in checked]
            if todo:
                engine.record(cfg, data, differ.run(cfg, str(baseline.baseline_dir(cfg)), root, todo))
        engine.record_isolation(data)


def _test_with_code(cfg, data, changed):
    """A test edited in the same session as the code it tests (FR-17)."""
    tests = [r for r in changed if checks.is_test_path(r)]
    if not tests or not idb.exists(cfg.root):
        return
    sources = set(changed) - set(tests)
    conn = idb.connect(cfg.root)
    try:
        for t in tests:
            module = t[:-3].replace("/", ".")
            covered = {r["file"] for r in conn.execute("SELECT DISTINCT file FROM test_cover WHERE test_id LIKE ?", (module + ".%",))}
            both = sorted(covered & sources)
            if both:
                ledger.add_finding(data, kind="test_edited_with_code", status=ledger.RISK, source="tests", subject=f"test edited with the code it tests: {t}", file=t,
                                   evidence=f"This session also changed {', '.join(both[:3])}.", item=t, code_hash=symbols.sha(virtual.read_current(str(Path(cfg.root) / t)) or ""), state="recorded")
    finally:
        conn.close()


def _stale_callers(cfg, data, changed):
    """Callers in files nobody touched, when a function's signature or name changed (static, so report only)."""
    if not idb.exists(cfg.root):
        return
    conn = idb.connect(cfg.root)
    try:
        for rel in changed:
            if not rel.endswith(".py") or checks.is_test_path(rel):
                continue
            before, after = baseline.read_baseline(cfg, rel), virtual.read_current(str(Path(cfg.root) / rel))
            if before is None or after is None:
                continue
            for c in symbol_diff.diff(before, after):
                if c.kind not in ("signature_changed", "deleted", "renamed", "visibility_changed") or c.before.kind == "class":
                    continue
                rows = conn.execute("""SELECT DISTINCT s.qualname, s.file FROM calls k JOIN symbols s ON k.caller_symbol_id = s.id WHERE k.called_name = ?""", (c.before.name,)).fetchall()
                stale = [r for r in rows if r["file"] not in changed and not checks.is_test_path(r["file"])]
                if stale:
                    names = ", ".join(f"{r['qualname']} ({r['file']})" for r in stale[:5])
                    ledger.add_finding(data, kind="stale_callers", status=ledger.RISK, source="names", subject=f"{c.kind.replace('_', ' ')}: {c.qualname}; callers not updated", file=rel,
                                       evidence=f"Callers by name in unchanged files: {names}", item=c.qualname, code_hash=symbols.sha(after), state="recorded")
    finally:
        conn.close()


def _report_notes(data, notes: list, drift, oas_notes=None) -> None:
    """Contract-tool notes for the session report (Sprint 7.6), each text once."""
    ct = data.setdefault("contract_tools", {})
    texts = [t for _, t in notes] + ([drift.note] if drift is not None and drift.note else []) + [t for _, t in oas_notes or []]
    ct["notes"] = list(dict.fromkeys(texts))


def run(cfg, data: dict, log=None) -> list[dict]:
    """Update the ledger from the whole diff. Returns unresolved high-risk findings not yet raised at a Stop.
    log: a callable taking one string, for errors that must not stop the audit."""
    if not data.get("baseline_ok"):
        return []
    changed = baseline.changed_files(cfg)
    from guardlib import contract_tools
    contract_tools.refresh(cfg, reset_counters=False)  # keeps the pre-edit timeout counter (Sprint 7.6)
    notes: list = []
    with scratch.for_trees(cfg, str(baseline.baseline_dir(cfg)), cfg.root, session_cache=True):
        drift = _drift(cfg, data, changed, log)
        oas, oas_notes = _app_openapi(cfg, data, changed, log)  # Sprint 7.6 Part B: the app's generated spec
        _contract_findings(cfg, data, changed, drift=drift, notes=notes, oas=oas)
        _report_notes(data, notes, drift, oas_notes)
        _behavior_findings(cfg, data, changed)
    _test_with_code(cfg, data, changed)
    _stale_callers(cfg, data, changed)
    return [f for f in data["findings"] if f["status"] in BLOCKING_STATUS and f["state"] == "open" and f["id"] not in data["stop_blocked_for"]
            and f.get("last_decision") != "over_budget"]


def block_reason(findings: list[dict]) -> str:
    lines = ["LegacyGuard session audit found unresolved evidence:"]
    lines += [f"[{f['id']}] {f['status']}: {f['subject']}. {f['evidence'][:300]}" for f in findings[:6]]
    if len(findings) > 6:
        lines.append(f"+{len(findings) - 6} more in the session report.")
    lines.append("Revise the code if these were not requested. If they were, explain that in your reply; the developer decides with /legacyguard:waive. This audit will not stop you a second time.")
    return "\n".join(lines)
