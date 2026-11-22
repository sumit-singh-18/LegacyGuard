"""Sprint 7.1: impact on the resolved call graph (guardlib/impact.py mode "resolved", guardlib/callgraph.py)."""
from __future__ import annotations

import subprocess
import sys

import pytest

from conftest import PLUGIN, write_tree
from guardlib import callgraph, impact

FILES = {
    "pkg/__init__.py": "",
    "pkg/a.py": "def total(x):\n    return x + 1\n",
    "pkg/b.py": "def total(x):\n    return x * 2\n",
    "pkg/use_a.py": "from pkg.a import total\n\n\ndef bill():\n    return total(1)\n",
    "pkg/use_b.py": "from pkg.b import total\n\n\ndef report():\n    return total(2)\n",
    "pkg/alias.py": "from pkg.a import total as t\n\n\ndef aliased():\n    return t(3)\n",
    # two-hop chains through intermediates that share a name, in different files
    "pkg/x.py": "from pkg.a import total\n\n\ndef step():\n    return total(4)\n",
    "pkg/y.py": "from pkg.b import total\n\n\ndef step():\n    return total(5)\n",
    "pkg/top_x.py": "from pkg.x import step\n\n\ndef top_x():\n    return step()\n",
    "pkg/top_y.py": "from pkg.y import step\n\n\ndef top_y():\n    return step()\n",
    "tests/test_a.py": "from pkg.a import total\n\n\ndef test_total():\n    assert total(1) == 2\n",
}


def _git(root):
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path, make_cfg):
    from indexer import __main__ as indexer_main
    from indexer import db
    root = write_tree(tmp_path / "p", FILES)
    _git(root)
    cfg = make_cfg(root)
    indexer_main.refresh(cfg)
    conn = db.connect(cfg.root)
    yield cfg, conn
    conn.close()


def _callers(imp):
    return {(c["file"], c["qualname"]): c for c in imp.callers}


def test_resolved_lists_only_real_callers(project):
    cfg, conn = project
    imp = impact.compute(conn, cfg, "pkg/a.py::total", mode="resolved")
    found = _callers(imp)
    assert imp.mode == "resolved" and not imp.note and not imp.truncated
    assert set(found) == {("pkg/use_a.py", "bill"), ("pkg/alias.py", "aliased"), ("pkg/x.py", "step"),
                          ("pkg/top_x.py", "top_x")}
    assert ("pkg/use_b.py", "report") not in found and ("pkg/top_y.py", "top_y") not in found
    assert not any(f.startswith("tests/") for f, _ in found)  # test files are still not callers
    bill = found[("pkg/use_a.py", "bill")]
    assert bill["sources"] == ["resolved"] and bill["via"] == "total" and bill["via_file"] == "pkg/a.py" and bill["hop"] == 1
    top = found[("pkg/top_x.py", "top_x")]
    assert top["hop"] == 2 and top["via"] == "step" and top["via_file"] == "pkg/x.py"
    text = impact.render(imp)
    assert "bill() [resolved]" in text and "report()" not in text and "[names]" not in text


def test_aliased_import_caller_found_only_when_resolved(project):
    cfg, conn = project
    assert ("pkg/alias.py", "aliased") in _callers(impact.compute(conn, cfg, "pkg/a.py::total", mode="resolved"))
    # the name index records the call as t(), so a name match misses it
    assert ("pkg/alias.py", "aliased") not in _callers(impact.compute(conn, cfg, "pkg/a.py::total", mode="names"))


def test_names_mode_keeps_the_old_behaviour(project):
    cfg, conn = project
    imp = impact.compute(conn, cfg, "pkg/a.py::total", mode="names")
    found = _callers(imp)
    assert imp.mode == "names"
    assert ("pkg/use_a.py", "bill") in found and ("pkg/use_b.py", "report") in found  # same-named total in b.py lends its caller
    assert all(c["sources"][0] == "names" and c["via_file"] is None for c in imp.callers)
    assert "report() [names]" in impact.render(imp)
    assert impact.compute(conn, cfg, "pkg/a.py::total").mode == "names"  # the default stays the fast one


def test_default_mode_and_bad_mode(project):
    cfg, conn = project
    with pytest.raises(ValueError):
        impact.compute(conn, cfg, "pkg/a.py::total", mode="graph")


def test_none_found_text_is_honest_per_mode(project):
    cfg, conn = project
    resolved = impact.compute(conn, cfg, "pkg/top_y.py::top_y", mode="resolved")
    assert "callers  none found through imports" in impact.render(resolved)
    names = impact.compute(conn, cfg, "pkg/top_y.py::top_y", mode="names")
    assert "callers  none found by name" in impact.render(names)


def test_tree_nests_under_the_callee_file(project):
    cfg, conn = project
    imp = impact.compute(conn, cfg, "total", mode="resolved")  # both definitions match the bare name
    assert sorted(map(tuple, imp.targets)) == [("pkg/a.py", "total"), ("pkg/b.py", "total")]
    tree = impact.render_tree(imp).splitlines()
    a_root = tree.index("total  pkg/a.py")
    b_root = next(i for i, line in enumerate(tree) if line.startswith("total  pkg/b.py"))
    x_step = next(i for i, line in enumerate(tree) if "step  pkg/x.py" in line)
    y_step = next(i for i, line in enumerate(tree) if "step  pkg/y.py" in line)
    top_x = next(i for i, line in enumerate(tree) if "top_x  pkg/top_x.py" in line)
    top_y = next(i for i, line in enumerate(tree) if "top_y  pkg/top_y.py" in line)
    assert a_root < x_step < top_x < b_root < y_step < top_y
    # each top sits one level under its own step
    assert tree[top_x].index("top_x") > tree[x_step].index("step")
    indent = lambda line: len(line) - len(line.lstrip(" |`-"))  # noqa: E731
    assert top_x == x_step + 1 and indent(tree[top_x]) > indent(tree[x_step])  # directly under its own step, one level in
    assert top_y == y_step + 1 and indent(tree[top_y]) > indent(tree[y_step])
    assert "top_y" not in "\n".join(tree[a_root:b_root])
    assert "[resolved]" in tree[top_x] and "[resolved]" in tree[top_y]


def test_names_tree_mixes_same_named_intermediates(project):
    """The old (hop, name) nesting: both tops land under the first step. Kept for names mode only."""
    cfg, conn = project
    tree = impact.render_tree(impact.compute(conn, cfg, "pkg/a.py::total", mode="names"))
    assert "top_y" in tree and "top_x" in tree


def test_truncation_note_when_candidate_files_capped(project, monkeypatch):
    cfg, conn = project
    monkeypatch.setattr(callgraph, "MAX_CANDIDATE_FILES", 1)
    imp = impact.compute(conn, cfg, "pkg/a.py::total", mode="resolved")
    assert imp.truncated and "more than 1 files mention total" in imp.truncated_reason
    assert "note     callers may be incomplete: more than 1 files mention total" in impact.render(imp)


def test_truncation_note_when_limit_hit(project):
    cfg, conn = project
    imp = impact.compute(conn, cfg, "pkg/a.py::total", mode="resolved", limit=2)
    assert imp.truncated and len(imp.callers) == 2
    assert "the search stopped at 2 callers" in impact.render(imp)
    assert "callers may be incomplete: the search stopped at 2 callers" in impact.render_tree(imp)


def test_fallback_to_names_when_callgraph_raises(project, monkeypatch):
    cfg, conn = project

    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(callgraph, "callers", boom)
    imp = impact.compute(conn, cfg, "pkg/a.py::total", mode="resolved")
    assert imp.mode == "names" and not imp.truncated and imp.fallback == "error"
    assert "RuntimeError" in imp.note and ("pkg/use_b.py", "report") in _callers(imp)
    text = impact.render(imp)
    assert "report() [names]" in text and "note     resolving callers through imports failed (RuntimeError)" in text
    assert "resolving callers through imports failed" in impact.render_tree(imp)


def test_callgraph_edge_callee_file_and_engine_shape(tmp_path):
    root = write_tree(tmp_path / "p", FILES)
    edges = callgraph.callers(str(root), "pkg/a.py", "total", None, hops=2)
    by = {(e.file, e.qualname): e for e in edges}
    assert by[("pkg/use_a.py", "bill")].callee_file == "pkg/a.py"
    assert by[("pkg/top_x.py", "top_x")].callee_file == "pkg/x.py" and by[("pkg/top_x.py", "top_x")].callee == "step"
    assert callgraph.Edge("f.py", "g", "h", "resolved").callee_file == ""  # positional construction still works
    stats: dict = {}
    callgraph.callers(str(root), "pkg/a.py", "total", None, hops=2, limit=1, stats=stats)
    assert stats.get("limit_hit") is True


def _counting_callgraph(monkeypatch):
    calls = []
    real = callgraph.callers

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(callgraph, "callers", spy)
    return calls


def _scripts_path(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))


def test_pre_edit_stays_on_names(project, monkeypatch):
    cfg, conn = project
    _scripts_path(monkeypatch)
    import pre_edit
    calls = _counting_callgraph(monkeypatch)
    modes = []
    real = impact.compute
    monkeypatch.setattr(impact, "compute", lambda *a, **k: modes.append(k.get("mode")) or real(*a, **k))
    before = FILES["pkg/a.py"]
    after = "def total(x, y=0):\n    return x + 1\n"
    text = pre_edit._risk_context(cfg, "pkg/a.py", before, after, {"context_sent": []})
    assert modes == ["names"] and calls == []
    assert text and "[names]" in text and "[resolved]" not in text


def test_prompt_context_stays_on_names(project, monkeypatch):
    cfg, conn = project
    _scripts_path(monkeypatch)
    import prompt_context
    calls = _counting_callgraph(monkeypatch)
    monkeypatch.setattr(prompt_context, "active_guards", lambda event: [cfg])
    out = prompt_context.handle({"prompt": "please change top_x so it rounds", "session_id": "s"})
    assert calls == []
    text = str(out)
    assert "top_x" in text and "[resolved]" not in text


def test_mcp_tool_uses_resolved(project, monkeypatch):
    cfg, conn = project
    pytest.importorskip("mcp.server.mcpserver")
    sys.path.insert(0, str(PLUGIN))
    from mcp_server import server
    monkeypatch.setattr(server, "_load", lambda: cfg)
    fn = getattr(server.impact, "fn", server.impact)
    text = fn("pkg/a.py::total")
    assert "bill() [resolved]" in text and "aliased() [resolved]" in text and "report()" not in text


def test_lg_impact_computes_once_and_resolved(project, monkeypatch, capsys):
    cfg, conn = project
    _scripts_path(monkeypatch)
    import lg
    monkeypatch.setattr(lg, "_cfg", lambda: cfg)
    modes = []
    real = impact.compute
    monkeypatch.setattr(impact, "compute", lambda *a, **k: modes.append(k.get("mode")) or real(*a, **k))
    args = type("A", (), {"symbol": "pkg/a.py::total", "depth": 3, "tree": True})()
    assert lg.cmd_impact(args) == 0
    out = capsys.readouterr().out
    assert modes == ["resolved"]
    assert "bill() [resolved]" in out and "`- top_x  pkg/top_x.py  [resolved]" in out and "report" not in out


def test_lg_impact_not_found(project, monkeypatch, capsys):
    cfg, conn = project
    _scripts_path(monkeypatch)
    import lg
    monkeypatch.setattr(lg, "_cfg", lambda: cfg)
    args = type("A", (), {"symbol": "nothing_here", "depth": 3, "tree": True})()
    assert lg.cmd_impact(args) == 0
    assert "nothing_here: not found in the index" in capsys.readouterr().out


def test_session_map_uses_resolved_edges(project):
    from guardlib import ledger, sessionmap
    cfg, conn = project
    data = ledger.fresh("s")
    ledger.add_finding(data, kind="verified", status=ledger.VERIFIED, source="diff", subject="pkg/a.py::total", file="pkg/a.py",
                       item="total", evidence="3 inputs compared", code_hash="h", state="recorded")
    model = sessionmap.build(cfg, data)
    ids = {n["id"] for n in model["nodes"]}
    assert {"pkg/use_a.py::bill", "pkg/alias.py::aliased", "pkg/x.py::step", "pkg/top_x.py::top_x"} <= ids
    assert "pkg/use_b.py::report" not in ids and "pkg/top_y.py::top_y" not in ids
    edges = {(e["from"], e["to"]) for e in model["edges"]}
    assert ("pkg/top_x.py::top_x", "pkg/x.py::step") in edges and ("pkg/x.py::step", "pkg/a.py::total") in edges
    assert next(n for n in model["nodes"] if n["id"] == "pkg/use_a.py::bill")["sources"] == ["resolved"]


def test_fallback_when_callgraph_raises_partway(project, monkeypatch):
    """The first matched definition resolves (and truncates), the second raises: nothing partial leaks into the result."""
    cfg, conn = project
    real, calls = callgraph.callers, []

    def second_raises(*a, **k):
        calls.append(a[1])
        if len(calls) == 2:
            raise RuntimeError("partway")
        return real(*a, **k)
    monkeypatch.setattr(callgraph, "callers", second_raises)
    imp = impact.compute(conn, cfg, "total", mode="resolved", limit=1)
    assert len(calls) == 2 and imp.mode == "names" and imp.fallback == "error"
    assert not imp.truncated and not imp.truncated_reason
    assert all(c["sources"][0] == "names" and "reaches" not in c for c in imp.callers)
    assert ("pkg/use_b.py", "report") in _callers(imp)


def test_budget_spent_falls_back_to_names(project):
    import time
    cfg, conn = project
    imp = impact.compute(conn, cfg, "pkg/a.py::total", mode="resolved", deadline=time.monotonic())
    assert imp.mode == "names" and imp.fallback == "budget" and ("pkg/use_b.py", "report") in _callers(imp)
    assert "note     the time budget for resolving callers through imports ran out, so callers are matched by name" in impact.render(imp)


def test_deadline_mid_search_stops_callgraph(tmp_path):
    import time
    root = write_tree(tmp_path / "p", FILES)
    stats: dict = {}
    out = callgraph.callers(str(root), "pkg/a.py", "total", None, hops=2, stats=stats, deadline=time.monotonic() - 1)
    assert out == [] and stats["deadline_hit"] is True
    stats = {}
    assert callgraph.callers(str(root), "pkg/a.py", "total", None, hops=2, stats=stats, deadline=time.monotonic() + 60)
    assert "deadline_hit" not in stats


def test_parse_cache_gives_the_same_edges(tmp_path, monkeypatch):
    root = write_tree(tmp_path / "p", FILES)
    plain = callgraph.callers(str(root), "pkg/a.py", "total", None, hops=3)
    parses = []
    real = callgraph.ast.parse
    monkeypatch.setattr(callgraph.ast, "parse", lambda *a, **k: parses.append(1) or real(*a, **k))
    cache: dict = {}
    first = callgraph.callers(str(root), "pkg/a.py", "total", None, hops=3, cache=cache)
    n = len(parses)
    again = callgraph.callers(str(root), "pkg/b.py", "total", None, hops=3, cache=cache)
    assert first == plain and len(parses) == n  # the second search reads no file again
    assert {e.qualname for e in again} == {"report", "step", "top_y"}


def test_session_map_budget_spent_uses_names_with_a_note(project, monkeypatch):
    from guardlib import ledger, sessionmap
    cfg, conn = project
    monkeypatch.setattr(sessionmap, "RESOLVE_BUDGET_S", 0)
    data = ledger.fresh("s")
    ledger.add_finding(data, kind="verified", status=ledger.VERIFIED, source="diff", subject="pkg/a.py::total", file="pkg/a.py",
                       item="total", evidence="3 inputs compared", code_hash="h", state="recorded")
    model = sessionmap.build(cfg, data)
    ids = {n["id"] for n in model["nodes"]}
    assert "pkg/use_b.py::report" in ids  # name-matched, as before 7.1
    assert model["impact_notes"] == ["callers of 1 function are matched by name: the 0 s time budget for resolving callers "
                                     "through imports ran out"]
    assert "M.impact_notes" in sessionmap.render_html(model)


def test_bare_name_text_marks_callers_of_other_matches(project):
    cfg, conn = project
    text = impact.render(impact.compute(conn, cfg, "total", mode="resolved"))
    assert "also     matched: pkg/b.py::total; callers of each are listed" in text
    assert "bill() [resolved]  " in text or "bill() [resolved]\n" in text  # a caller of the first match has no marker
    assert "report() [resolved] -> pkg/b.py::total" in text and "top_y() [resolved] -> pkg/b.py::total" in text
    assert "bill() [resolved] ->" not in text
    names = impact.render(impact.compute(conn, cfg, "total", mode="names"))
    assert "also     matched: pkg/b.py::total; callers matched by name may belong to any of them" in names


def test_routes_of_every_matched_definition(project):
    cfg, conn = project
    conn.execute("INSERT INTO routes(method, path, handler_qualname, handler_file, source) VALUES ('GET', '/b', 'total', 'pkg/b.py', 'test')")
    conn.commit()
    imp = impact.compute(conn, cfg, "total", mode="resolved")
    assert [r["route"] for r in imp.routes] == ["GET /b"]
