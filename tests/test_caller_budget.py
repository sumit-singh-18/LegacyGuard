"""Callers that are unlikely to run are ranked last, and the ones left out are named (roadmap 6.5)."""
from guardlib import engine, ledger
from runner import diff

from conftest import write_tree

CORE = "def helper(x):\n    return x * 2\n"

# Ten extractor-like classes: __init__ needs arguments the guard has no values or types for, and no test builds them.
EXT = "from pkg.core import helper\n\n" + "".join(
    f"class E{i}:\n    def __init__(self, downloader, opts):\n        self.d = downloader\n\n"
    f"    def run(self, url):\n        return helper(len(url))\n\n" for i in range(10))
FUNCS = "from pkg.core import helper\n\ndef plain(x):\n    return helper(x) + 1\n\ndef other(x, y):\n    return helper(x) + y\n"


def _project(tmp_path, make_cfg, files):
    new = {**files, "pkg/core.py": CORE.replace("x * 2", "x * 3")}
    root = write_tree(tmp_path / "proj", new)
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", files)
    changed = diff.changed_targets("pkg/core.py", (base / "pkg/core.py").read_text(), (root / "pkg/core.py").read_text())
    return cfg, base, root, changed


def test_callable_functions_chosen_over_uncallable_methods(tmp_path, make_cfg, monkeypatch):
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/a_ext.py": EXT, "pkg/z_funcs.py": FUNCS}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    monkeypatch.setattr(engine, "MAX_DEPENDENTS", 3)
    skipped: list = []
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed}, skipped=skipped)
    names = [d.qualname for d in deps]
    assert len(names) == 3 and names[:2] == ["plain", "other"]
    assert len(skipped) == 9 and all(s["qualname"].startswith("E") for s in skipped)
    note = engine.skipped_note(skipped)
    assert note.startswith("9 callers not run: ") and "E" in note and "Nothing is known about them from execution" in note


def test_static_signal_ranks_unbuildable_classes_last(tmp_path, make_cfg, monkeypatch):
    # Every caller is a method one hop away; only the static signal tells them apart. The callable ones sort last by file.
    good = ("from pkg.core import helper\n\nclass Good:\n    def run(self, x):\n        return helper(x)\n\n"
            "class Typed:\n    def __init__(self, n: int):\n        self.n = n\n\n    def run(self, x):\n        return helper(x)\n\n"
            "class Built:\n    def __init__(self, a, b):\n        self.a = a\n\n    def run(self, x):\n        return helper(x)\n")
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/a_ext.py": EXT, "pkg/z_good.py": good,
             "tests/test_built.py": "from pkg.z_good import Built\n\ndef test_b():\n    assert Built(1, 2).run(3) == 6\n"}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    monkeypatch.setattr(engine, "MAX_DEPENDENTS", 3)
    skipped: list = []
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed}, skipped=skipped)
    assert sorted(d.qualname for d in deps) == ["Built.run", "Good.run", "Typed.run"]
    assert {s["qualname"] for s in skipped} == {f"E{i}.run" for i in range(10)}
    assert all("unlikely to build their class" in s["reason"] for s in skipped)
    note = engine.skipped_note(skipped)
    assert note.startswith("10 callers not run: ") and "+5 more" in note


def test_no_skipped_callers_means_no_note(tmp_path, make_cfg):
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/z_funcs.py": FUNCS}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    skipped: list = []
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed}, skipped=skipped)
    assert {d.qualname for d in deps} == {"plain", "other"} and skipped == []
    assert engine.skipped_note(skipped) == ""


def test_caller_with_no_completed_input_ranked_last_on_the_next_edit(tmp_path, make_cfg, monkeypatch):
    # a_bad sorts first by every static key (same hop, plain function, one parameter, first file) but never completes.
    bad = "from pkg.core import helper\n\ndef a_bad(x):\n    return helper(int('not a number'))\n"
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/a_bad.py": bad, "pkg/z_funcs.py": FUNCS}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    data = ledger.fresh("s")
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    assert [d.qualname for d in deps][0] == "a_bad"
    results = diff.run(cfg, str(base), str(root), changed + deps, harvest={})
    r = next(r for r in results if r.qualname == "a_bad")
    assert r.status == diff.UNTESTED and "no input ran to completion" in r.reason
    engine.record(cfg, data, results)
    assert "pkg/a_bad.py::a_bad" in data["callers_unrunnable"]

    # Second edit in the same session: a_bad is now last, and with a budget of two it is left out and named.
    monkeypatch.setattr(engine, "MAX_DEPENDENTS", 2)
    skipped: list = []
    deps2 = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed},
                              memory=data["callers_unrunnable"], skipped=skipped)
    assert [d.qualname for d in deps2] == ["plain", "other"]
    assert [s["qualname"] for s in skipped] == ["a_bad"] and "no input ran to completion" in skipped[0]["reason"]
    assert engine.skipped_note(skipped).startswith("1 caller not run: earlier in this session no input ran to completion")

    # The memory is tied to the caller's code: once a_bad is edited it is ranked on static signals again.
    (root / "pkg/a_bad.py").write_text(bad.replace("int('not a number')", "x"))
    monkeypatch.setattr(engine, "MAX_DEPENDENTS", 8)
    deps3 = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed}, memory=data["callers_unrunnable"])
    assert [d.qualname for d in deps3][0] == "a_bad"


def test_memory_forgotten_when_the_caller_runs(tmp_path, make_cfg):
    root = write_tree(tmp_path / "p", {"c.py": "def c(x):\n    return x\n"})
    cfg = make_cfg(root)
    data = ledger.fresh("s")
    r = diff.Result("c.py", "c", diff.UNTESTED, "h", reason="inputs could not be set up (could not build a Foo to call the method on)",
                    baseline_unrunnable=True)
    r.via, r.via_file = "calls f", "m.py"
    engine.record(cfg, data, [r])
    assert "could not build a Foo" in data["callers_unrunnable"]["c.py::c"]["reason"]
    ok = diff.Result("c.py", "c", diff.DONE, "h2")
    ok.via, ok.via_file = "calls f", "m.py"
    engine.record(cfg, data, [ok])
    assert "c.py::c" not in data["callers_unrunnable"]
    # Only the baseline-side flag counts, never the reason text: the same words can come from the edit itself.
    slow = diff.Result("c.py", "c", diff.UNTESTED, "h3", reason="no input ran to completion")
    slow.via, slow.via_file = "calls f", "m.py"
    engine.record(cfg, data, [slow])
    assert "c.py::c" not in data["callers_unrunnable"]


def test_targets_for_reports_skipped_and_fits_the_function_budget(tmp_path, make_cfg, monkeypatch):
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/a_ext.py": EXT, "pkg/z_funcs.py": FUNCS}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    from guardlib import baseline
    monkeypatch.setattr(baseline, "read_baseline", lambda cfg_, rel: (base / rel).read_text())
    data = ledger.fresh("s")
    skipped: list = []
    targets = engine.targets_for(cfg, "pkg/core.py", data, skipped=skipped)
    assert len(targets) <= diff.MAX_FUNCTIONS
    names = [t.qualname for t in targets]
    assert names[0] == "helper" and {"plain", "other"} <= set(names)
    assert len(skipped) == 4  # 12 callers, 8 fit
    assert engine.skipped_note(skipped).startswith("4 callers not run: ")
    assert len(data["callers_skipped"]) == 4


def test_new_side_hang_is_not_remembered(tmp_path, make_cfg):
    # The edit makes helper hang. The caller then has no input that completes, but the cause is the edit, so the fix
    # that follows must not find it ranked last with an untrue reason.
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE,
             "pkg/use.py": "from pkg.core import helper\n\ndef caller():\n    return helper(1)\n"}
    root = write_tree(tmp_path / "proj", {**files, "pkg/core.py": "def helper(x):\n    while True:\n        pass\n"})
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", files)
    changed = diff.changed_targets("pkg/core.py", (base / "pkg/core.py").read_text(), (root / "pkg/core.py").read_text())
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    assert [d.qualname for d in deps] == ["caller"]
    results = diff.run(cfg, str(base), str(root), deps, harvest={})
    r = results[0]
    assert r.status == diff.UNTESTED and "no input ran to completion" in r.reason and not r.baseline_unrunnable
    data = ledger.fresh("s")
    engine.record(cfg, data, results)
    assert "pkg/use.py::caller" not in data["callers_unrunnable"]


def test_open_difference_caller_is_never_cut(tmp_path, make_cfg, monkeypatch):
    # 1 edited function + 8 callers (7 functions, then E0.run ranked last by the static signal) + 4 re-checks of open
    # differences (E5-E8.run): 13 targets. E0.run also has an open difference, so it is kept and f6 is cut and named.
    many = "from pkg.core import helper\n\n" + "".join(f"def f{i}(x):\n    return helper(x) + {i}\n\n" for i in range(7))
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/a_ext.py": EXT, "pkg/b_many.py": many}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    from guardlib import baseline
    monkeypatch.setattr(baseline, "read_baseline", lambda cfg_, rel: (base / rel).read_text())
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    assert [d.qualname for d in deps][-1] == "E0.run"
    data = ledger.fresh("s")
    for i in (0, 5, 6, 7, 8):
        data["findings"].append({"kind": "behavior_difference", "state": "open", "item": f"E{i}.run", "file": "pkg/a_ext.py",
                                 "via": "calls helper", "via_file": "pkg/core.py", "callees": ["helper"]})
    skipped: list = []
    targets = engine.targets_for(cfg, "pkg/core.py", data, skipped=skipped)
    names = [t.qualname for t in targets]
    assert len(names) == diff.MAX_FUNCTIONS
    assert names[0] == "helper" and set(names[1:6]) == {"E0.run", "E5.run", "E6.run", "E7.run", "E8.run"}
    # Cut by dependents' budget (E5-E8 among them, but re-run as open differences, so not listed), then f6 by the trim.
    assert [s["qualname"] for s in skipped] == ["E1.run", "E2.run", "E3.run", "E4.run", "E9.run", "f6"] and "f6" not in names
    assert "places went to the edited functions, open differences" in skipped[-1]["reason"]
    assert "pkg/b_many.py::f6" in data["callers_skipped"]


def test_class_built_in_its_own_file_is_not_flagged(tmp_path, make_cfg, monkeypatch):
    own = ("from pkg.core import helper\n\nclass Local:\n    def __init__(self, downloader, opts):\n        self.d = downloader\n\n"
           "    def run(self, x):\n        return helper(x)\n\nDEFAULT = Local(None, {})\n")
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/a_ext.py": EXT, "pkg/z_local.py": own}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    monkeypatch.setattr(engine, "MAX_DEPENDENTS", 1)
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    assert [d.qualname for d in deps] == ["Local.run"]


def test_budget_wording_when_edited_functions_fill_the_run(tmp_path, make_cfg):
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/z_funcs.py": FUNCS}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    skipped: list = []
    assert engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed}, skipped=skipped, limit=0) == []
    assert len(skipped) == 2
    assert all(s["reason"] == f"the run's {diff.MAX_FUNCTIONS} places went to the edited functions" for s in skipped)
    assert "limit of 0" not in engine.skipped_note(skipped)


def test_wake_text_names_skipped_callers(tmp_path, make_cfg, monkeypatch):
    import sys

    import pytest
    from conftest import PLUGIN
    sys.path.insert(0, str(PLUGIN / "scripts"))
    try:
        import _common
        import post_edit
    finally:
        sys.path.remove(str(PLUGIN / "scripts"))
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n"})
    cfg = make_cfg(root)
    with ledger.locked(cfg.state_dir) as data:
        data["baseline_ok"] = True
    monkeypatch.setattr(post_edit, "load_guard", lambda event, fp=None: (str(root), cfg))
    target = diff.Target("m.py", "f", "function", [], "h")

    def fake_targets(cfg_, rel, data, skipped=None):
        skipped.append({"file": "x.py", "qualname": "X.run", "via_file": "m.py", "reason": "the guard is unlikely to build their class"})
        return [target]
    monkeypatch.setattr(engine, "targets_for", fake_targets)
    different = diff.Result("m.py", "f", diff.DIFFERENT, "h", inputs_compared=1,
                            differences=[{"call": "f(1)", "before": "1", "after": "2", "source": "tests", "args": [], "has_self": False,
                                          "before_raw": {"k": "return"}, "after_raw": {"k": "return"}, "special": False,
                                          "state": False, "noise": False}])
    monkeypatch.setattr(diff, "run", lambda *a, **k: [different])
    monkeypatch.setattr(engine, "affected_tests", lambda *a, **k: [])
    with pytest.raises(_common.Wake) as woke:
        post_edit.handle({"tool_input": {"file_path": str(root / "m.py")}, "cwd": str(root)})
    text = str(woke.value)
    assert "behavior difference" in text and "1 caller not run: the guard is unlikely to build their class (X.run)" in text


def _skipped_session(tmp_path, make_cfg):
    files = {"pkg/__init__.py": "", "pkg/core.py": CORE, "pkg/z_funcs.py": FUNCS}
    cfg, base, root, changed = _project(tmp_path, make_cfg, files)
    data = ledger.fresh("s")
    ledger.add_finding(data, kind="verified", status=ledger.VERIFIED, source="diff", subject="pkg/core.py::helper", file="pkg/core.py",
                       item="helper", evidence="3 inputs compared", code_hash="h", state="recorded")
    data["callers_run"]["pkg/z_funcs.py::other"] = {"status": "not_run", "inputs": 2, "via": "calls helper",
                                                     "reason": "no input ran to completion", "statements": [0, 1], "example": ""}
    data["callers_skipped"]["pkg/z_funcs.py::plain"] = {"reason": "the guard is unlikely to build their class", "via_file": "pkg/core.py"}
    return cfg, data


def test_stop_report_lists_callers_not_run(tmp_path, make_cfg):
    from guardlib import report
    cfg, data = _skipped_session(tmp_path, make_cfg)
    text = report.render(data, cfg)
    assert ("**Not checked:** 1 caller of edited code was not run: the guard is unlikely to build their class "
            "(`plain` in pkg/z_funcs.py). Nothing is known about it from execution.") in text
    data["callers_skipped"]["pkg/z_funcs.py::plain"]["reason"] = "r"
    data["callers_skipped"]["pkg/b.py::g"] = {"reason": "r", "via_file": "pkg/core.py"}
    assert "2 callers of edited code were not run: r (`plain` in pkg/z_funcs.py, `g` in pkg/b.py)" in report.render(data, cfg)


def test_session_map_shows_skipped_callers_as_not_run(tmp_path, make_cfg):
    import json
    from guardlib import sessionmap
    from indexer import __main__ as indexer_main
    import subprocess
    cfg, data = _skipped_session(tmp_path, make_cfg)
    root = cfg.root
    with open(f"{root}/.gitignore", "w") as fh:
        fh.write(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    indexer_main.refresh(cfg)
    model = sessionmap.build(cfg, data)
    nodes = {n["id"]: n for n in model["nodes"]}
    plain, other = nodes["pkg/z_funcs.py::plain"], nodes["pkg/z_funcs.py::other"]
    # Left out of the check: marked skipped, never "tried, could not be run". A caller that was tried keeps that.
    assert plain["skipped"]["reason"] == "the guard is unlikely to build their class" and plain["ran"] is None
    assert other["skipped"] is None and other["ran"]["status"] == "not_run"
    assert any("1 caller of edited code was not run" in st["text"] for st in model["story"])
    html = sessionmap.render_html(model)
    assert "t='not run: '+n.skipped.reason" in html and "No. It was not run: " in html
    blob = html.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob)["nodes"]  # the model the page reads carries the skipped entry
    assert "the guard is unlikely to build their class" in blob


def test_ci_report_lists_callers_not_run(tmp_path, make_cfg):
    from guardlib import ci
    cfg = make_cfg(tmp_path)
    out = ci.Outcome(base="abc", skipped=[{"file": "x.py", "qualname": "X.run", "via_file": "m.py", "reason": "r"}])
    assert "1 caller not run: r (X.run)" in ci.report(cfg, out)
