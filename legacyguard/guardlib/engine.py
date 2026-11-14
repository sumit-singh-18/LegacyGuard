"""Run the behavioral diff for files and record what it found. Shared by PostToolUse and Stop."""
from __future__ import annotations

from pathlib import Path

from guardlib import baseline, checks, intent, ledger, paths, symbols, virtual, waivers
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


def _required(params: list[dict]) -> int:
    """Parameters a call must fill: no default, not self/cls, not *args/**kwargs."""
    return sum(1 for p in params if p.get("default") is None and p["name"] not in ("self", "cls") and not p["name"].startswith("*"))


def _covered(conn, file: str, qualname: str) -> bool:
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM test_cover WHERE file=? AND qualname=? LIMIT 1", (file, qualname)).fetchone() is not None
    except Exception:  # noqa: BLE001 - an index without coverage simply ranks nothing first
        return False


def dependents(cfg, rel: str, changed: list[differ.Target], taken: set[str]) -> list[differ.Target]:
    """Functions that call an edited one, up to two steps away (FR-41, roadmap B7).

    An edit to a shared helper changes what its callers return, and the helper itself is often the one function that
    cannot be run. Callers are resolved through imports (guardlib/callgraph.py), not matched by name.

    Only MAX_DEPENDENTS fit, so callers likely to be callable go first (static signals only; validation run 1 lost its
    budget to methods whose instances could not be built). The order:
      1. nearer first (1 hop before 2), resolved edges before name-only ones;
      2. callers the tests run (test_cover in the index), since their inputs and instances can be harvested;
      3. plain functions before methods, which need an instance;
      4. fewer required parameters;
      5. the edited file before others, then by file and name.

    A caller reached by several edges keeps its best-ranked one for `via` and the ranking. It is gated on the changed
    statements of every edited function it reaches, one or two hops away: it passes only if its inputs run all of them
    (runner/diff.py caller_gate, compare_one)."""
    from guardlib import callgraph
    from indexer import db
    if not changed:
        return []
    conn = db.connect(cfg.root) if db.exists(cfg.root) else None
    try:
        edges = []
        try:
            for t in changed:
                edges += [(e, t) for e in callgraph.callers(cfg.root, rel, t.qualname, conn, hops=2)]
        except Exception:  # noqa: BLE001 - no callers is a smaller check, never a failed one
            edges = []
        names = {t.qualname for t in changed}
        combined = symbols.sha("|".join(sorted(t.code_hash for t in changed)))
        callers: dict = {}   # (file, qualname) -> {"best": (rank, edge), "edited": [targets], "pair": (then, now)}
        for e, t in edges:
            key = (e.file, e.qualname)
            if e.qualname in taken or (e.file == rel and e.qualname in names):
                continue
            if key not in callers:
                if not is_guarded_source(cfg, e.file):
                    continue
                pair = _sym_pair(cfg, e.file, e.qualname)
                if pair is None:
                    continue
                callers[key] = {"best": None, "edited": [], "pair": pair, "covered": _covered(conn, e.file, e.qualname)}
            c = callers[key]
            then, now = c["pair"]
            rank = (e.hops, e.how != "resolved", not c["covered"], now.kind == "method", _required(then.params),
                    e.file != rel, e.file, e.qualname)
            if c["best"] is None or rank < c["best"][0]:
                c["best"] = (rank, e)
            if t not in c["edited"]:
                c["edited"].append(t)
    finally:
        if conn is not None:
            conn.close()
    out = []
    for c in sorted(callers.values(), key=lambda c: c["best"][0])[:MAX_DEPENDENTS]:
        e, (then, now) = c["best"][1], c["pair"]
        callee = e.callee.rsplit(".", 1)[-1]
        out.append(differ.Target(e.file, e.qualname, now.kind, then.params, symbols.sha(now.node_text + "|calls|" + combined),
                                 source=then.node_text, via=f"calls {callee}", via_file=rel, **differ.caller_gate(c["edited"])))
    return out


def route_targets(cfg, targets: list[differ.Target]) -> list[differ.Target]:
    """API routes whose handler is among the targets, with the requests to send (roadmap C3). Needs `app:` in the
    config and the routes table from the index."""
    from indexer import db
    from runner import routes_diff
    if not cfg.raw.get("app") or not targets or not db.exists(cfg.root):
        return []
    conn = db.connect(cfg.root)
    try:
        handlers = {(t.file, t.qualname) for t in targets if t.kind != "route"}
        hashes = {(t.file, t.qualname): t.code_hash for t in targets}
        out = []
        for method, path, file, qualname in routes_diff.routes_for(conn, handlers):
            requests = routes_diff.requests_for(cfg.root, method, path, routes_diff.handler_params(conn, file, qualname))
            out.append(differ.Target(file, f"{method} {path}", "route", [], symbols.sha(f"{method} {path}|{hashes[(file, qualname)]}"),
                                     requests=requests))
        return out
    except Exception:  # noqa: BLE001 - a route that cannot be planned is skipped, never a failed check
        return []
    finally:
        conn.close()


def targets_for(cfg, rel: str, data: dict) -> list[differ.Target]:
    """Changed functions, the functions that call them, and functions with an open difference.

    A revert must be proved by running the function again, and that includes a caller whose difference came from this file."""
    before, after = baseline.read_baseline(cfg, rel), virtual.read_current(str(Path(cfg.root) / rel))
    targets = {t.qualname: t for t in differ.changed_targets(rel, before, after)}
    for dep in dependents(cfg, rel, list(targets.values()), set(targets)):
        targets[dep.qualname] = dep
    for route in route_targets(cfg, list(targets.values())):
        targets[route.qualname] = route
    for t in targets.values():
        decl = intent.declaration_for(data, t.file, t.qualname)
        if decl and t.kind != "route":
            t.declared = decl["examples"]
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
                    # Still judged by the changed statements of every edited function it reaches, while they have any.
                    wanted = set(f.get("callees") or [f["via"].split()[-1]])
                    short = {w.rsplit(".", 1)[-1] for w in wanted}
                    edited = [t for t in targets.values() if t.file == rel and not t.via
                              and (t.qualname in wanted or (not f.get("callees") and t.qualname.rsplit(".", 1)[-1] in short))]
                    gate = differ.caller_gate(edited)
                    targets[q] = differ.Target(f["file"], q, pair[1].kind, pair[0].params, symbols.sha(pair[1].node_text + "|recheck"),
                                               via=f["via"], via_file=rel, **gate)
    return sorted(targets.values(), key=lambda t: t.qualname)


def record(cfg, data: dict, results: list[differ.Result]) -> list[tuple[dict, differ.Result]]:
    """Add findings to the ledger. Returns new, unwaived differences: the ones that may wake Claude."""
    wake: list[tuple[dict, differ.Result]] = []
    # A caller two steps away is quiet when the caller between it and the edit already shows the difference.
    edited = {r.qualname.rsplit(".", 1)[-1]: r.status for r in results if r.via}
    edited.update({r.qualname.rsplit(".", 1)[-1]: r.status for r in results if not r.via})
    for r in results:
        via = {"via": r.via, "via_file": r.via_file, **({"callees": r.callees} if r.callees else {})} if r.via else {}
        if r.via:
            # What happened to each caller is kept for the session map, whether or not it becomes a finding.
            outcome = {differ.DONE: "agree", differ.DIFFERENT: "differ", differ.BROKEN: "differ"}.get(r.status, "not_run")
            data.setdefault("callers_run", {})[f"{r.file}::{r.qualname}"] = {
                "status": outcome, "inputs": r.inputs_compared, "via": r.via, "reason": (r.reason or "")[:160],
                "statements": [r.statements_reached, r.statements_total],
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
            # A declared change (declare_change) whose examples all hold is recorded as intended, and so are callers
            # whose difference comes from a declared function. A declared example that fails always wakes Claude.
            decl = intent.declaration_for(data, r.file, r.qualname)
            callee_decl = None
            if r.via:
                callee = r.via.split()[-1]
                callee_decl = next((d for k, d in (data.get("declarations") or {}).items()
                                    if k.startswith(f"{r.via_file}::") and k.rsplit("::", 1)[1].rsplit(".", 1)[-1] == callee), None)
            intended = not r.declared_failures and (decl is not None or callee_decl is not None)
            if r.declared_failures:
                evidence = "; ".join(f"{d['call']} declared {d['expected']} got {d['got']}" for d in r.declared_failures[:differ.MAX_SHOWN])
            if r.unreached_note:
                evidence += f". {r.unreached_note}"
            state = "waived" if waived else "intended" if intended else "open"
            extra = {"counterexamples": r.differences[:differ.MAX_SHOWN], "inputs_compared": r.inputs_compared,
                     "statements": [r.statements_reached, r.statements_total], **via}
            if intended:
                extra["intent"] = (decl or callee_decl)["description"]
            if r.declared_failures:
                extra["declared_failures"] = r.declared_failures[:differ.MAX_SHOWN]
            f, new = ledger.add_finding(data, kind="behavior_difference", status=status, source="diff", subject=r.subject, file=r.file, item=r.qualname, evidence=evidence,
                                        code_hash=r.code_hash, state=state, extra=extra)
            for old in data["findings"]:
                if old is not f and old["kind"] == "behavior_difference" and old["file"] == r.file and old["item"] == r.qualname and old["state"] == "open":
                    old["state"] = "superseded"
            for old in data["findings"]:
                if old["kind"] in ("verified", "untested") and old["file"] == r.file and old["item"] == r.qualname:
                    old["state"] = "superseded"
            if new and state == "open":
                wake.append((f, r))
        elif r.status == differ.DONE:
            for old in data["findings"]:
                if old["file"] == r.file and old["item"] == r.qualname and old["kind"] in ("behavior_difference", "untested", "verified") and old["state"] in ("open", "recorded"):
                    old["state"] = "resolved" if old["kind"] == "behavior_difference" else "superseded"
            f, new = ledger.add_finding(data, kind="verified", status=ledger.VERIFIED, source="diff", subject=r.subject, file=r.file, item=r.qualname,
                                        evidence=f"{r.inputs_compared} inputs compared, {r.inputs_completed} returned a value" +
                                                 (f", {r.dropped_nondeterministic} dropped as non-deterministic" if r.dropped_nondeterministic else "") +
                                                 (f"; {differ.reached_text(r)}" + (f" of {differ.callee_names(r.callees)}" if r.via and r.callees else "")
                                                  if r.statements_total else "") +
                                                 (f"; {differ.confidence_text(r)}" if r.mutants_total else ""),
                                        code_hash=r.code_hash, state="recorded",
                                        extra={"inputs_compared": r.inputs_compared, "mutants": [r.mutants_killed, r.mutants_total],
                                               "statements": [r.statements_reached, r.statements_total], **via})
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
