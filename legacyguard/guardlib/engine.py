"""Run the behavioral diff for files and record what it found. Shared by PostToolUse and Stop."""
from __future__ import annotations

from pathlib import Path

from guardlib import baseline, checks, ledger, paths, symbols, virtual, waivers
from runner import diff as differ


def is_guarded_source(cfg, rel: str | None) -> bool:
    return bool(rel) and rel.endswith(".py") and paths.in_scope(rel, cfg.scope) and not rel.startswith(".legacyguard/") and not checks.is_test_path(rel) \
        and not paths.matches_any(rel, cfg.raw.get("frozen") or [])


MAX_DEPENDENTS = 8


def _sym_pair(cfg, rel: str, qualname: str):
    """(symbol in the baseline, symbol now) for a function, or None if it is not a function in both."""
    before, after = baseline.read_baseline(cfg, rel), virtual.read_current(str(Path(cfg.root) / rel))
    if before is None or after is None:
        return None
    then = next((s for s in symbols.extract(before)[0] if s.qualname == qualname), None)
    now = next((s for s in symbols.extract(after)[0] if s.qualname == qualname), None)
    if then is None or now is None or now.kind == "class":
        return None
    return then, now


def dependents(cfg, rel: str, changed: list[differ.Target], taken: set[str]) -> list[differ.Target]:
    """Functions that call an edited one, up to two steps away (FR-41, roadmap B7).

    An edit to a shared helper changes what its callers return, and the helper itself is often the one function that
    cannot be run. Callers are resolved through imports (guardlib/callgraph.py), not matched by name; nearest first,
    resolved edges before name-only ones, the edited file before others."""
    from guardlib import callgraph
    from indexer import db
    if not changed:
        return []
    conn = db.connect(cfg.root) if db.exists(cfg.root) else None
    try:
        edges = []
        for t in changed:
            edges += callgraph.callers(cfg.root, rel, t.qualname, conn, hops=2)
    except Exception:  # noqa: BLE001 - no callers is a smaller check, never a failed one
        edges = []
    finally:
        if conn is not None:
            conn.close()
    names = {t.qualname for t in changed}
    combined = symbols.sha("|".join(sorted(t.code_hash for t in changed)))
    out, seen = [], set()
    for e in sorted(edges, key=lambda e: (e.hops, e.how != "resolved", e.file != rel, e.file, e.qualname)):
        if len(out) >= MAX_DEPENDENTS or (e.file, e.qualname) in seen or e.qualname in taken or (e.file == rel and e.qualname in names):
            continue
        if not is_guarded_source(cfg, e.file):
            continue
        pair = _sym_pair(cfg, e.file, e.qualname)
        if pair is None:
            continue
        then, now = pair
        seen.add((e.file, e.qualname))
        callee = e.callee.rsplit(".", 1)[-1]
        out.append(differ.Target(e.file, e.qualname, now.kind, then.params, symbols.sha(now.node_text + "|calls|" + combined),
                                 source=then.node_text, via=f"calls {callee}", via_file=rel))
    return out


def targets_for(cfg, rel: str, data: dict) -> list[differ.Target]:
    """Changed functions, the functions that call them, and functions with an open difference.

    A revert must be proved by running the function again, and that includes a caller whose difference came from this file."""
    before, after = baseline.read_baseline(cfg, rel), virtual.read_current(str(Path(cfg.root) / rel))
    targets = {t.qualname: t for t in differ.changed_targets(rel, before, after)}
    for dep in dependents(cfg, rel, list(targets.values()), set(targets)):
        targets[dep.qualname] = dep
    if after is not None and before is not None:
        now = {s.qualname: s for s in symbols.extract(after)[0]}
        then = {s.qualname: s for s in symbols.extract(before)[0]}
        for f in data["findings"]:
            if f["kind"] != "behavior_difference" or f["state"] != "open" or f["item"] in targets:
                continue
            q = f["item"]
            if f["file"] == rel and not f.get("via"):
                if q in now and q in then and now[q].kind != "class":
                    targets[q] = differ.Target(rel, q, now[q].kind, then[q].params, symbols.sha(now[q].node_text))
            elif f.get("via_file") == rel:
                pair = _sym_pair(cfg, f["file"], q)
                if pair is not None:
                    targets[q] = differ.Target(f["file"], q, pair[1].kind, pair[0].params, symbols.sha(pair[1].node_text + "|recheck"),
                                               via=f["via"], via_file=rel)
    return sorted(targets.values(), key=lambda t: t.qualname)


def record(cfg, data: dict, results: list[differ.Result]) -> list[tuple[dict, differ.Result]]:
    """Add findings to the ledger. Returns new, unwaived differences: the ones that may wake Claude."""
    wake: list[tuple[dict, differ.Result]] = []
    # A caller two steps away is quiet when the caller between it and the edit already shows the difference.
    edited = {r.qualname.rsplit(".", 1)[-1]: r.status for r in results if r.via}
    edited.update({r.qualname.rsplit(".", 1)[-1]: r.status for r in results if not r.via})
    for r in results:
        via = {"via": r.via, "via_file": r.via_file} if r.via else {}
        if r.via:
            # What happened to each caller is kept for the session map, whether or not it becomes a finding.
            outcome = {differ.DONE: "agree", differ.DIFFERENT: "differ", differ.BROKEN: "differ"}.get(r.status, "not_run")
            data.setdefault("callers_run", {})[f"{r.file}::{r.qualname}"] = {
                "status": outcome, "inputs": r.inputs_compared, "via": r.via, "reason": (r.reason or "")[:160],
                "example": (f"{r.differences[0]['call']} gave {r.differences[0]['before']} before and {r.differences[0]['after']} after"
                            if r.differences else "")[:300]}
            # A caller is in the report only when it shows something the edited function's own result does not.
            # Its agreement on a few inputs says little while the function it calls is known to differ or went
            # unchecked, and a second finding for a break already reported at its source is noise.
            was_open = any(f["kind"] == "behavior_difference" and f["file"] == r.file and f["item"] == r.qualname and f["state"] == "open"
                           for f in data["findings"])
            callee = r.via.split()[-1]
            source_status = edited.get(callee)
            if source_status is None and any(f["kind"] == "behavior_difference" and f["state"] == "open" and not f.get("via") and f["file"] == r.via_file
                                             and f["item"].rsplit(".", 1)[-1] == callee for f in data["findings"]):
                # At Stop only the callers may be re-run; the edited function's own open difference is already in the ledger.
                source_status = differ.DIFFERENT
            if r.status == differ.UNTESTED:
                continue
            if r.status == differ.DONE and not was_open:
                continue
            if r.status in (differ.DIFFERENT, differ.BROKEN) and source_status in (differ.DIFFERENT, differ.BROKEN):
                continue
        if r.status in (differ.DIFFERENT, differ.BROKEN):
            status = ledger.DIFFERENCE if r.status == differ.DIFFERENT else ledger.FAILURE
            shown = [{k: d[k] for k in ("call", "before", "after", "source")} for d in r.differences[:differ.MAX_SHOWN]]
            evidence = "; ".join(f"{d['call']} before {d['before']} after {d['after']}" for d in shown) or r.reason
            waived = waivers.is_waived(cfg, "behavior_difference", r.file, r.qualname, r.code_hash)
            f, new = ledger.add_finding(data, kind="behavior_difference", status=status, source="diff", subject=r.subject, file=r.file, item=r.qualname, evidence=evidence,
                                        code_hash=r.code_hash, state="waived" if waived else "open",
                                        extra={"counterexamples": r.differences[:differ.MAX_SHOWN], "inputs_compared": r.inputs_compared, **via})
            for old in data["findings"]:
                if old is not f and old["kind"] == "behavior_difference" and old["file"] == r.file and old["item"] == r.qualname and old["state"] == "open":
                    old["state"] = "superseded"
            for old in data["findings"]:
                if old["kind"] in ("verified", "untested") and old["file"] == r.file and old["item"] == r.qualname:
                    old["state"] = "superseded"
            if new and not waived:
                wake.append((f, r))
        elif r.status == differ.DONE:
            for old in data["findings"]:
                if old["file"] == r.file and old["item"] == r.qualname and old["kind"] in ("behavior_difference", "untested", "verified") and old["state"] in ("open", "recorded"):
                    old["state"] = "resolved" if old["kind"] == "behavior_difference" else "superseded"
            f, new = ledger.add_finding(data, kind="verified", status=ledger.VERIFIED, source="diff", subject=r.subject, file=r.file, item=r.qualname,
                                        evidence=f"{r.inputs_compared} inputs compared, {r.inputs_completed} returned a value" +
                                                 (f", {r.dropped_nondeterministic} dropped as non-deterministic" if r.dropped_nondeterministic else "") +
                                                 (f"; {differ.confidence_text(r)}" if r.mutants_total else ""),
                                        code_hash=r.code_hash, state="recorded",
                                        extra={"inputs_compared": r.inputs_compared, "mutants": [r.mutants_killed, r.mutants_total], **via})
            if not new:
                f["state"] = "recorded"
        else:
            for old in data["findings"]:
                if old["file"] == r.file and old["item"] == r.qualname and old["kind"] in ("verified", "untested") and old["state"] == "recorded":
                    old["state"] = "superseded"
            f, new = ledger.add_finding(data, kind="untested", status=ledger.UNTESTED, source="diff", subject=r.subject, file=r.file, item=r.qualname,
                                        evidence=f"untested because it {r.reason}" if r.reason.startswith(("needs", "writes", "starts")) else f"untested because: {r.reason}",
                                        code_hash=r.code_hash, state="recorded")
            if not new:
                f["state"] = "recorded"
    return wake


def affected_tests(cfg, rel: str, targets: list[differ.Target], code_hash: str | None) -> list[str]:
    """Run the project's tests that reach the changed functions on the baseline and on the edited code, record
    regressions, and return the wake texts for new ones (roadmap C4). Returns [] when there is no coverage map."""
    from indexer import db
    from runner import affected_tests as at
    s = at.settings(cfg)
    if not s["enabled"] or not targets or not db.exists(cfg.root):
        return []
    conn = db.connect(cfg.root)
    try:
        nodeids = at.select(conn, cfg.root, [(t.file, t.qualname) for t in targets], int(s["max_tests"]))
    finally:
        conn.close()
    if not nodeids:
        return []
    with ledger.locked(cfg.state_dir) as data:
        cache = dict(data.get("old_test_results") or {})
    regressions, cache, new = at.compare(cfg, str(baseline.baseline_dir(cfg)), cfg.root, nodeids, cache)
    reaches = sorted({t.qualname for t in targets})
    wakes: list[str] = []
    with ledger.locked(cfg.state_dir) as data:
        data["old_test_results"] = cache
        failing = {r["nodeid"] for r in regressions}
        for f in data["findings"]:
            if f["kind"] == "test_regression" and f["state"] == "open" and f["item"] in nodeids and f["item"] not in failing \
                    and (new.get(f["item"]) or {}).get("outcome") == "passed":
                f["state"] = "resolved"
        for r in regressions:
            test_file = r["nodeid"].split("::", 1)[0]
            waived = waivers.is_waived(cfg, "test_regression", test_file, r["nodeid"], code_hash)
            f, is_new = ledger.add_finding(data, kind="test_regression", status=ledger.FAILURE, source="tests", subject=r["nodeid"], file=test_file,
                                           item=r["nodeid"], evidence=(r["message"].splitlines() or ["the test fails"])[0][:300], code_hash=code_hash,
                                           state="waived" if waived else "open", extra={"reaches": reaches, "edited_file": rel})
            for old in data["findings"]:
                if old is not f and old["kind"] == "test_regression" and old["item"] == r["nodeid"] and old["state"] == "open":
                    old["state"] = "superseded"
            if is_new and not waived:
                wakes.append(at.format_wake(r, f["id"], reaches))
        if wakes:
            data["wakeups"] += 1
    return wakes
