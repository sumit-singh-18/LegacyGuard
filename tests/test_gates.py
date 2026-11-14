"""Honest "verified" (validation run 1): callers are gated on the edited function's changed statements, every changed
statement must be reached, edits that cannot change behaviour are skipped, and callable callers are run first."""
from guardlib import engine, ledger
from runner import diff
from runner import lg_codec

from conftest import call, write_tree


def ran(*outcomes, lines=None, gate_lines=None):
    out = {"status": "ran", "results": list(outcomes), "lines": lines}
    if gate_lines is not None:
        out["gate_lines"] = gate_lines
    return out


def ret(v):
    return {"outcome": "return", "value": lg_codec.canon(v)}


# ---- per-statement gate ----

def test_two_statement_edit_with_one_reached_is_untested():
    t = diff.Target("m.py", "f", "function", [{"name": "a0"}], "h", new_gate=[[5, 5], [7, 8]])
    r = diff.compare_one(t, [call(1)], ran(ret(1)), ran(ret(1)), ran(ret(1), lines=[4, 5, 6]), True)
    assert r.status == diff.UNTESTED
    assert "inputs reached 1 of 2 changed statements; lines 7-8 never ran" in r.reason
    assert (r.statements_reached, r.statements_total) == (1, 2)
    full = diff.compare_one(t, [call(1)], ran(ret(1)), ran(ret(1)), ran(ret(1), lines=[5, 8]), True)
    assert full.status == diff.DONE and (full.statements_reached, full.statements_total) == (2, 2)
    assert "reached 2 of 2 changed statements" in diff.format_note([full])


def test_deletion_is_gated_on_the_old_tree():
    t = diff.Target("m.py", "f", "function", [{"name": "a0"}], "h", old_gate=[[3, 3]])
    miss = diff.compare_one(t, [call(1)], ran(ret(1), lines=[2]), ran(ret(1)), ran(ret(1)), True)
    assert miss.status == diff.UNTESTED and "reached 0 of 1" in miss.reason
    hit = diff.compare_one(t, [call(1)], ran(ret(1), lines=[3]), ran(ret(1)), ran(ret(1)), True)
    assert hit.status == diff.DONE
    caller = diff.Target("c.py", "c", "function", [], "h", via="calls f", via_file="m.py", callees=["f"], callee_file="m.py",
                         callee_old_gate=[[3, 3], [6, 6]])
    part = diff.compare_one(caller, [call()], ran(ret(1), gate_lines=[3]), ran(ret(1)), ran(ret(1)), True)
    assert part.status == diff.UNTESTED
    assert part.reason == "inputs reached 1 of 2 changed statements of f; line 6 (in the code before the edit) never ran"


GATED_OLD = """
def g(x: int) -> int:
    y = x
    if len(str(x)) > 400:
        return y * 2
    return y
"""
GATED_NEW = GATED_OLD.replace("    y = x\n", "    y = x + 0\n").replace("return y * 2", "return y + y")


def test_partly_reached_edit_end_to_end(tmp_path, make_cfg):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": GATED_NEW})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/m.py": GATED_OLD})
    cfg = make_cfg(root)
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    assert [t.new_gate for t in targets] == [[[2, 2], [4, 4]]]
    r = diff.run(cfg, str(base), str(root), targets, harvest={})[0]
    assert r.status == diff.UNTESTED, r
    assert "inputs reached 1 of 2 changed statements; line 4 never ran" in r.reason


# ---- edits that cannot change behaviour ----

PLAIN = '''
def f(a, b):
    """Add two numbers."""
    total = a + b  # sum
    return total
'''


def test_comment_whitespace_and_docstring_edits_produce_no_target():
    comment = PLAIN.replace("# sum", "# the sum, pragma: no cover")
    spacing = PLAIN.replace("total = a + b", "total = a  +  b").replace("def f(a, b):", "def f(a,b):")
    doc = PLAIN.replace("Add two numbers.", "Return a plus b.")
    added_comment = PLAIN.replace("    return total", "    # done\n\n    return total")
    for after in (comment, spacing, doc, added_comment):
        assert diff.changed_targets("m.py", PLAIN, after) == [], after
    real = doc.replace("a + b", "a - b")
    assert [t.qualname for t in diff.changed_targets("m.py", PLAIN, real)] == ["f"]


def test_method_with_comment_only_edit_is_skipped():
    old = "class C:\n    def m(self, x):\n        return x  # a\n\n    def n(self):\n        return 1\n"
    new = old.replace("# a", "# b").replace("return 1", "return 2")
    assert [t.qualname for t in diff.changed_targets("m.py", old, new)] == ["C.n"]


# ---- callers carry the edited function's gate ----

CORE = """
def fee(x):
    if x > 100:
        return x * 2
    return x
"""
CALLERS = {
    "pkg/__init__.py": "",
    "pkg/api.py": "from pkg.core import fee\n\n\ndef small():\n    return fee(1)\n\n\ndef big():\n    return fee(500)\n",
    "pkg/top.py": "from pkg.api import small\n\n\ndef top():\n    return small()\n",
}


def _caller_setup(tmp_path, make_cfg, new_core):
    root = write_tree(tmp_path / "proj", {**CALLERS, "pkg/core.py": new_core})
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", {**CALLERS, "pkg/core.py": CORE})
    changed = diff.changed_targets("pkg/core.py", (base / "pkg/core.py").read_text(), (root / "pkg/core.py").read_text())
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    return cfg, base, root, changed, deps


def test_caller_that_never_reaches_the_change_is_untested(tmp_path, make_cfg):
    cfg, base, root, changed, deps = _caller_setup(tmp_path, make_cfg, CORE.replace("return x * 2", "return x + x"))
    by_name = {d.qualname: d for d in deps}
    assert by_name["top"].callees == ["fee"] and by_name["top"].callee_new_gate == changed[0].new_gate  # hop 2 keeps fee's gate
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), deps, harvest={})}
    assert results["big"].status == diff.DONE, results["big"].reason
    assert (results["big"].statements_reached, results["big"].statements_total) == (1, 1)
    for name in ("small", "top"):
        assert results[name].status == diff.UNTESTED, results[name]
        assert "reached the changed lines of fee" in results[name].reason
    note = diff.format_note(list(results.values()))
    assert "big (reached 1 of 1 changed statements of fee)" in note and "small" not in note


def test_ledger_evidence_mentions_reached_statements(tmp_path, make_cfg):
    cfg, base, root, changed, deps = _caller_setup(tmp_path, make_cfg, CORE.replace("return x * 2", "return x + x"))
    results = diff.run(cfg, str(base), str(root), changed + deps, harvest={})
    data = ledger.fresh("s")
    engine.record(cfg, data, results)
    verified = [f for f in data["findings"] if f["kind"] == "verified"]
    assert verified and all("reached" in f["evidence"] and "changed statements" in f["evidence"] for f in verified)
    assert data["callers_run"]["pkg/api.py::small"]["statements"] == [0, 1]


def test_worker_traces_callee_gate(tmp_path, run_worker):
    root = write_tree(tmp_path / "p", {**CALLERS, "pkg/core.py": CORE})
    spec = {"module": "pkg.api", "qualname": "big", "inputs": [call()], "gate": {"file": "pkg/core.py", "ranges": [[3, 3], [4, 4]]}}
    out = run_worker(root, [spec])["pkg.api:big"]
    assert out["gate_lines"] == [3]  # fee's lines 3-4 only, not its `if` on line 2
    spec["qualname"] = "small"
    assert run_worker(root, [spec])["pkg.api:small"]["gate_lines"] == [4]
    assert "gate_lines" not in run_worker(root, [{**spec, "gate": None}])["pkg.api:small"]


# ---- caller ranking ----

RANKED = {
    "pkg/__init__.py": "",
    "pkg/core.py": CORE,
    "pkg/use.py": (
        "from pkg.core import fee\n\n\n"
        "class Extractor:\n    def __init__(self, downloader, session):\n        self.d = downloader\n\n"
        "    def _real_extract(self, url):\n        return fee(len(url))\n\n\n"
        "def helper(a):\n    return fee(a)\n\n\n"
        "def needs_more(a, b, c):\n    return fee(a + b + c)\n"
    ),
}


def test_callable_plain_function_ranks_before_method(tmp_path, make_cfg, monkeypatch):
    root = write_tree(tmp_path / "proj", {**RANKED, "pkg/core.py": CORE.replace("x * 2", "x * 3")})
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", RANKED)
    changed = diff.changed_targets("pkg/core.py", (base / "pkg/core.py").read_text(), (root / "pkg/core.py").read_text())
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    assert [d.qualname for d in deps] == ["helper", "needs_more", "Extractor._real_extract"]
    monkeypatch.setattr(engine, "MAX_DEPENDENTS", 1)
    assert [d.qualname for d in engine.dependents(cfg, "pkg/core.py", changed, set())] == ["helper"]


def test_docstring_rule_covers_only_the_functions_own_docstring():
    old = 'def f(x):\n    """Doc."""\n    def inner():\n        """Inner doc."""\n        return 1\n    return inner.__name__\n'
    own = old.replace('"""Doc."""', '"""Other doc."""')
    nested = old.replace("Inner doc.", "Changed inner doc.")
    assert diff.changed_targets("m.py", old, own) == []
    assert [t.qualname for t in diff.changed_targets("m.py", old, nested)] == ["f"]  # a nested docstring is kept


def test_docstring_edit_counts_where_docstrings_are_read():
    for header in ("import ply.yacc as yacc\n\n\n", "from click import command\n\n\n", "import docopt\n\n\n", ""):
        old = header + 'def p_expr(p):\n    """expr : expr PLUS term"""\n    p[0] = p[1]\n'
        new = old.replace("PLUS", "MINUS")
        if header:
            assert [t.qualname for t in diff.changed_targets("m.py", old, new)] == ["p_expr"], header
        else:
            assert diff.changed_targets("m.py", old, new) == []
    reads = 'def usage():\n    """Usage: x"""\n    return 1\n\nHELP = usage.__doc__\n'
    assert [t.qualname for t in diff.changed_targets("m.py", reads, reads.replace("Usage: x", "Usage: y"))] == ["usage"]


# ---- callers of several edited functions ----

MULTI_CORE = "def a(x):\n    if x > 100:\n        return x * 2\n    return x\n\n\ndef b(x):\n    if x > 100:\n        return x * 2\n    return x\n"
MULTI = {
    "pkg/__init__.py": "",
    "pkg/mid.py": "from pkg.core import a\n\n\ndef mid():\n    return a(1)\n",
    "pkg/top.py": ("from pkg.core import a, b\nfrom pkg.mid import mid\n\n\n"
                   "def c():\n    return mid() + b(500)\n\n\n"
                   "def both():\n    return a(500) + b(500)\n\n\n"
                   "def only_a():\n    return a(500) + b(1)\n"),
}


def _multi(tmp_path, make_cfg):
    new = MULTI_CORE.replace("return x * 2", "return x + x")
    root = write_tree(tmp_path / "proj", {**MULTI, "pkg/core.py": new})
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", {**MULTI, "pkg/core.py": MULTI_CORE})
    changed = diff.changed_targets("pkg/core.py", MULTI_CORE, new)
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    return cfg, base, root, deps


def test_caller_keeps_best_edge_and_is_gated_on_every_edited_function(tmp_path, make_cfg):
    cfg, base, root, deps = _multi(tmp_path, make_cfg)
    by_name = {d.qualname: d for d in deps}
    c = by_name["c"]
    # c calls b directly (hop 1) and a through mid (hop 2): the direct edge names it, both gates count.
    assert c.via == "calls b" and sorted(c.callees) == ["a", "b"] and sorted(c.callee_new_gate) == [[3, 3], [9, 9]]
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), deps, harvest={})}
    rc = results["c"]
    assert rc.status == diff.UNTESTED and (rc.statements_reached, rc.statements_total) == (1, 2)
    assert "inputs reached 1 of 2 changed statements of " in rc.reason and "line 3 never ran" in rc.reason
    assert results["both"].status == diff.DONE, results["both"].reason
    assert (results["both"].statements_reached, results["both"].statements_total) == (2, 2)
    oa = results["only_a"]
    assert oa.status == diff.UNTESTED and "line 9 never ran" in oa.reason  # b's edit never ran
    assert "of a, b" in oa.reason or "of b, a" in oa.reason
    note = diff.format_note(list(results.values()))
    assert "both (reached 2 of 2 changed statements of a, b)" in note or "both (reached 2 of 2 changed statements of b, a)" in note


def test_recheck_keeps_every_callee_gate(tmp_path, make_cfg, monkeypatch):
    cfg, base, root, deps = _multi(tmp_path, make_cfg)
    oa = next(d for d in deps if d.qualname == "only_a")
    data = ledger.fresh("s")
    data["findings"].append({"kind": "behavior_difference", "state": "open", "item": "only_a", "file": "pkg/top.py",
                             "via": oa.via, "via_file": "pkg/core.py", "callees": oa.callees})
    from guardlib import baseline
    monkeypatch.setattr(engine, "dependents", lambda *a, **k: [])  # only the re-check path adds only_a
    monkeypatch.setattr(baseline, "read_baseline", lambda cfg_, rel: (base / rel).read_text())
    targets = {t.qualname: t for t in engine.targets_for(cfg, "pkg/core.py", data)}
    assert sorted(targets["only_a"].callees) == ["a", "b"]
    assert sorted(targets["only_a"].callee_new_gate) == [[3, 3], [9, 9]]


def test_caller_difference_without_reaching_the_change_says_so(tmp_path, make_cfg):
    t = diff.Target("c.py", "c", "function", [], "h", via="calls f", via_file="m.py", callees=["f"], callee_file="m.py",
                    callee_new_gate=[[3, 3]])
    r = diff.compare_one(t, [call()], ran(ret(1)), ran(ret(1)), ran(ret(2), gate_lines=[]), True)
    assert r.status == diff.DIFFERENT and (r.statements_reached, r.statements_total) == (0, 1)
    wake = diff.format_wake(r, "LG-1")
    assert "never ran the changed statements of f (reached 0 of 1)" in wake and "may come from elsewhere" in wake
    data = ledger.fresh("s")
    root = write_tree(tmp_path / "p", {"c.py": "def c():\n    return 1\n"})
    engine.record(make_cfg(root), data, [r])
    f = next(f for f in data["findings"] if f["kind"] == "behavior_difference")
    assert f["state"] == "open" and "may come from elsewhere" in f["evidence"]  # still reported, not suppressed
    reached = diff.compare_one(t, [call()], ran(ret(1)), ran(ret(1)), ran(ret(2), gate_lines=[3]), True)
    assert reached.status == diff.DIFFERENT and not reached.unreached_note
