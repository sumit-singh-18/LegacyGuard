"""The whole-session audit behind the Stop hook (FR-18). Compares the working tree with the baseline, so shell edits count."""
from __future__ import annotations

from pathlib import Path

from guardlib import baseline, checks, contracts_parse as cp, engine, ledger, paths, symbol_diff, symbols, virtual, waivers
from indexer import db as idb
from runner import diff as differ

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
            d = base / folder
            return [p.read_text() for p in sorted(d.iterdir()) if p.suffix == ".sql"] if d.is_dir() else []
        return scripts(baseline.baseline_dir(cfg)), scripts(Path(cfg.root))
    return reader


def _contract_findings(cfg, data, changed):
    http = _baseline_http(cfg)
    reader = _migrations(cfg)
    seen_items = set()
    for rel in changed:
        before = baseline.read_baseline(cfg, rel)
        after = virtual.read_current(str(Path(cfg.root) / rel)) or ""
        if paths.matches_any(rel, cfg.raw.get("frozen") or []):
            ledger.add_finding(data, kind="frozen", status=ledger.FAILURE, source="rule", subject=f"frozen file changed outside Edit and Write: {rel}", file=rel,
                               evidence="The working tree differs from the baseline for a file on the frozen list.", item=rel, code_hash=symbols.sha(after))
            continue
        code_hash = symbols.sha(after)
        for b in checks.run_checks(cfg, rel, before, after, http, reader):
            seen_items.add((rel, b.item))
            known = next((f for f in data["findings"] if f["kind"] == "contract_break" and f["file"] == rel and f["item"] == b.item), None)
            if known is not None:
                if known["state"] == "not_applied":
                    known["state"] = "open"
                continue
            waived = waivers.is_waived(cfg, "contract_break", rel, b.item, code_hash)
            ledger.add_finding(data, kind="contract_break", status=ledger.FAILURE, source=b.source, subject=b.subject, file=rel,
                               evidence=f"{b.text()}; found by the session audit, so the edit bypassed the pre-write check", item=b.item, code_hash=code_hash,
                               state="waived" if waived else "open", extra={"contract_kind": b.kind})
    for f in data["findings"]:
        if f["kind"] == "contract_break" and f["state"] in ("open", "applied") and (f["file"], f["item"]) not in seen_items:
            f["state"] = "not_applied"


def _behavior_findings(cfg, data, changed):
    root = cfg.root
    for rel in changed:
        if not engine.is_guarded_source(cfg, rel):
            continue
        targets = engine.targets_for(cfg, rel, data)
        checked = {(f["item"], f["code_hash"]) for f in data["findings"] if f["file"] == rel and f["kind"] in ("verified", "untested", "behavior_difference")
                   and f["state"] in ("open", "recorded", "waived")}
        todo = [t for t in targets if (t.qualname, t.code_hash) not in checked]
        if todo:
            engine.record(cfg, data, differ.run(cfg, str(baseline.baseline_dir(cfg)), root, todo))


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


def run(cfg, data: dict) -> list[dict]:
    """Update the ledger from the whole diff. Returns unresolved high-risk findings not yet raised at a Stop."""
    if not data.get("baseline_ok"):
        return []
    changed = baseline.changed_files(cfg)
    _contract_findings(cfg, data, changed)
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
