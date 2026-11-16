"""Task 6.6: a guarded "slowdown" result for an input that times out only in the new code (roadmap 3d, run 2)."""
import importlib.util
from pathlib import Path

from guardlib import engine, ledger
from runner import diff

from conftest import call, write_tree

TOOL = Path(__file__).resolve().parent.parent / "tools" / "validate.py"
_spec = importlib.util.spec_from_file_location("lg_validate_slowdown", TOOL)
validate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate)

SLOW_INPUT = "a" * 32 + "!"
OLD = '''
import re


def is_word(text):
    return re.match(r"^[a-z]+$", text) is not None
'''
# The same language, but a nested quantifier: on a run of letters followed by "!" the regex engine backtracks
# through every way of splitting the run, which takes far longer than any time limit.
NEW = OLD.replace('r"^[a-z]+$"', 'r"^([a-z]+)+$"')
TESTS = f'''
from pkg.words import is_word


def test_words():
    assert is_word("abc")
    assert not is_word("{SLOW_INPUT}")
'''


def setup(tmp_path, make_cfg, old_src, new_src, mode="guarded"):
    files = {"pkg/__init__.py": "", "tests/test_words.py": TESTS}
    root = write_tree(tmp_path / "proj", {**files, "pkg/words.py": new_src})
    base = write_tree(tmp_path / "base", {**files, "pkg/words.py": old_src})
    cfg = make_cfg(root, mode=mode)
    targets = diff.changed_targets("pkg/words.py", (base / "pkg/words.py").read_text(), (root / "pkg/words.py").read_text())
    return cfg, str(base), str(root), targets


def test_new_only_backtracking_is_a_slowdown_not_a_difference(tmp_path, make_cfg):
    cfg, base, work, targets = setup(tmp_path, make_cfg, OLD, NEW)
    r = {x.qualname: x for x in diff.run(cfg, base, work, targets, harvest={})}["is_word"]
    assert r.status == diff.SLOWDOWN, (r.status, r.reason)
    assert not r.differences
    assert r.slowdowns and len(r.slowdowns) <= diff.MAX_SLOWDOWN_CHECKS
    s = next(s for s in r.slowdowns if SLOW_INPUT in s["call"])
    assert s["new_limit_s"] == diff.CALL_TIMEOUT_S
    assert 0 <= s["old_seconds"] <= diff.CALL_TIMEOUT_S * diff.SLOWDOWN_OLD_SHARE
    text = diff.slowdown_text(s)
    assert text.startswith(f"the new code ran past {diff.CALL_TIMEOUT_S} s on is_word(")
    assert "while the old code finished in" in text and "this is a slowdown, not a proof of a wrong result" in text
    wake = diff.format_wake(r, "LG-1")
    assert wake.startswith("LegacyGuard slowdown [LG-1]") and "LegacyGuard behavior difference" not in wake
    assert "not a proof of a wrong result" in wake and "does not block" in wake
    assert s["old_runs"] == 2 and "Both runs of the code before the edit finished" in wake


def test_both_versions_slow_is_nothing(tmp_path, make_cfg):
    old = OLD.replace('r"^[a-z]+$"', 'r"^([a-z]+)+$"')
    new = old.replace("return re.match(", "found = re.match(").replace("is not None\n", "is not None\n    return found\n")
    cfg, base, work, targets = setup(tmp_path, make_cfg, old, new)
    r = {x.qualname: x for x in diff.run(cfg, base, work, targets, harvest={})}["is_word"]
    assert r.status != diff.SLOWDOWN and not r.slowdowns


SLEEPY = '''
import time


def pause(n):
    {body}
    return n
'''


def _target(qualname="pause"):
    return diff.Target("pkg/sleepy.py", qualname, "function", [{"name": "n"}], "h")


def _ran(*outcomes, seconds=None, limits=None):
    return {"status": "ran", "results": list(outcomes), "lines": None,
            "seconds": seconds or [0.001] * len(outcomes), "limits": limits or [diff.CALL_TIMEOUT_S] * len(outcomes)}


def test_candidates_need_both_old_runs_fast():
    t, items = _target(), [call(1), call(2), call(3)]
    ret = {"outcome": "return", "value": {"t": "v", "v": 1}}
    a = _ran(ret, ret, ret, seconds=[0.001, 0.9, 0.001])     # input 2: OLD-A took more than 10% of the limit
    b = _ran(ret, ret, {"outcome": "timeout"}, seconds=[0.002, 0.001, None])  # input 3: OLD-B timed out
    n = _ran({"outcome": "timeout"}, {"outcome": "timeout"}, {"outcome": "timeout"}, seconds=[None] * 3)
    got = diff.slowdown_candidates(items, a, b, n, use_self_diff=True)
    assert [c["index"] for c in got] == [0]
    assert got[0]["reduced"] is False
    n2 = _ran({"outcome": "timeout"}, ret, ret, limits=[0.5, 5, 5])
    assert diff.slowdown_candidates(items, a, b, n2, use_self_diff=True)[0]["reduced"] is True
    # A run without timings (an older worker, a route) gives no candidates.
    assert diff.slowdown_candidates(items, {"status": "ran", "results": a["results"]}, b, n, use_self_diff=True) == []


def test_slow_only_under_the_reduced_limit_is_nothing(tmp_path, make_cfg):
    # The new code takes about 1 s on n=1: past the reduced 0.5 s limit, well within the full one. The re-run alone with
    # the full limit returns, so no slowdown is reported.
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/sleepy.py": SLEEPY.format(body="time.sleep(1.0 if n == 1 else 0)")})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/sleepy.py": SLEEPY.format(body="pass")})
    cfg = make_cfg(root)
    t, items = _target(), [call(1)]
    ret = {"outcome": "return", "value": {"t": "v", "v": 1}}
    a, b = _ran(ret), _ran(ret)
    n = _ran({"outcome": "timeout"}, seconds=[None], limits=[0.5])
    r = diff.compare_one(t, items, a, b, n, True)
    assert r.slowdown_candidates
    spec = {"module": "pkg.sleepy", "qualname": "pause", "inputs": items}
    diff.confirm_slowdowns(cfg, diff.cfgmod.target_python(cfg), str(base), str(root),
                           [str(base)], [str(root)], [spec], [spec], {"pkg.sleepy:pause": (t, items)}, {"pause": r})
    assert r.status != diff.SLOWDOWN and not r.slowdowns


def test_confirmation_reports_a_hang_after_the_reduced_limit(tmp_path, make_cfg):
    # The same shape, but the new code really hangs: the re-run with the full limit times out again, the old code is fast.
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/sleepy.py": SLEEPY.format(body="time.sleep(60 if n == 1 else 0)")})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/sleepy.py": SLEEPY.format(body="pass")})
    cfg = make_cfg(root)
    t, items = _target(), [call(1)]
    ret = {"outcome": "return", "value": {"t": "v", "v": 1}}
    r = diff.compare_one(t, items, _ran(ret), _ran(ret), _ran({"outcome": "timeout"}, seconds=[None], limits=[0.5]), True)
    spec = {"module": "pkg.sleepy", "qualname": "pause", "inputs": items}
    diff.confirm_slowdowns(cfg, diff.cfgmod.target_python(cfg), str(base), str(root),
                           [str(base)], [str(root)], [spec], [spec], {"pkg.sleepy:pause": (t, items)}, {"pause": r})
    assert r.status == diff.SLOWDOWN and len(r.slowdowns) == 1
    assert r.slowdowns[0]["call"].startswith("pause(") and r.slowdowns[0]["new_limit_s"] == diff.CALL_TIMEOUT_S


def test_worker_reports_timings_and_honours_full_limit(tmp_path, run_worker):
    root = write_tree(tmp_path / "proj", {"mod.py": SLEEPY.format(body="time.sleep(0.7 if n == 1 else 0)")})
    items = [call(9), call(9), call(1)]
    spec = {"module": "mod", "qualname": "pause", "inputs": items}
    out = run_worker(root, [spec])["mod:pause"]
    assert out["limits"] == [5, 5, 5] and len(out["seconds"]) == 3 and out["seconds"][2] >= 0.6
    # Re-run alone: the original position (index) is kept, and the full limit applies whatever came before.
    out = run_worker(root, [{**spec, "inputs": [items[2]], "indexes": [2], "full_limit": True}])["mod:pause"]
    assert out["results"][0]["outcome"] == "return" and out["limits"] == [5]


def test_confirmation_is_bounded_and_fails_open(tmp_path, make_cfg, monkeypatch):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": ""})
    cfg = make_cfg(root)
    t, items = _target(), [call(i) for i in range(6)]
    ret = {"outcome": "return", "value": {"t": "v", "v": 1}}
    r = diff.compare_one(t, items, _ran(*[ret] * 6), _ran(*[ret] * 6), _ran(*[{"outcome": "timeout"}] * 6, seconds=[None] * 6), True)
    assert len(r.slowdown_candidates) == 6
    seen = []

    def fake_worker(cfg, py, root, sys_path, functions, timeout, warm_names=None):
        seen.append(len(functions[0]["inputs"]))
        assert functions[0]["full_limit"] is True and "mutants" not in functions[0]
        raise RuntimeError("boom")
    monkeypatch.setattr(diff, "_run_worker", fake_worker)
    spec = {"module": "pkg.sleepy", "qualname": "pause", "inputs": items, "mutants": [1]}
    diff.confirm_slowdowns(cfg, "python", "b", "w", [], [], [spec], [spec], {"pkg.sleepy:pause": (t, items)}, {"pause": r})
    assert seen == [diff.MAX_SLOWDOWN_CHECKS]  # the first (old) run raised; nothing else ran
    assert r.status != diff.SLOWDOWN and not r.slowdowns


def _slow_result(via=""):
    return diff.Result("m.py", "f", diff.SLOWDOWN, "h1", inputs_compared=1, via=via, via_file="m.py" if via else "",
                       slowdowns=[{"call": "f('aaa!')", "new_limit_s": 5, "old_seconds": 0.001, "source": "tests"}])


def test_engine_records_a_slowdown_and_wakes_only_when_guarded(tmp_path, make_cfg):
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n"})
    for mode, wakes in (("guarded", True), ("strict", True), ("advisory", False)):
        cfg = make_cfg(root, mode=mode)
        data = ledger.fresh("s")
        wake = engine.record(cfg, data, [_slow_result()])
        f = next(f for f in data["findings"] if f["kind"] == "slowdown")
        assert f["status"] == ledger.SLOWDOWN and "not a proof of a wrong result" in f["evidence"]
        assert not any(f["kind"] == "behavior_difference" for f in data["findings"])
        assert bool(wake) is wakes
    # A later pass resolves the open slowdown.
    cfg = make_cfg(root)
    data = ledger.fresh("s")
    engine.record(cfg, data, [_slow_result()])
    engine.record(cfg, data, [diff.Result("m.py", "f", diff.DONE, "h2", inputs_compared=3, inputs_completed=3)])
    assert next(f for f in data["findings"] if f["kind"] == "slowdown")["state"] == "resolved"


def test_note_mentions_a_slowdown_that_did_not_wake():
    note = diff.format_note([_slow_result()])
    assert "ran past 5 s" in note and "not a proof" in note


def test_harness_lists_slowdowns_separately():
    f = validate._function(_slow_result())
    assert f["status"] == diff.SLOWDOWN and f["slowdowns"]
    got = validate.classify("bug", [f], ["m.py"])
    assert got["verdict"] == validate.QUIET and not got["different"]
    assert got["slowdowns"] == [f]
    rec = {"name": "case1", "kind": "bug", "expect": "detected", "verdict": got["verdict"], "match": False, "seconds": 1.0,
           "functions": [f], "mutants_killed": 0, "mutants_total": 0, "slowdowns": got["slowdowns"]}
    m = validate.metrics([rec])
    assert m["detection"]["detected"] == 0
    assert m["slowdown_cases"] == ["case1"]
    assert "Slowdowns (not counted as detections): case1" in validate.metrics_text(m)
    full = {"fix_tests": None, "different": [], "other_different": [], "untested": [], "verified": [], "error": "", "warnings": [],
            "deciding_input": None, "python_version": None}
    md = validate.markdown([{**full, **rec}], {"with": m}, "manifest.json")
    assert "- Slowdown (not counted as a detection): `m.py::f`: the new code ran past 5 s on f('aaa!')" in md
    assert "Different:" not in md


# ---- review fixes ----

def test_slowdown_counts_as_checked_and_quiet():
    f = validate._function(_slow_result())
    assert validate.classify("bug", [f], ["m.py"])["verdict"] == validate.QUIET
    assert validate.classify("change", [f], [])["verdict"] == validate.QUIET
    # Something else differing still decides the verdict.
    other = {"file": "m.py", "qualname": "g", "status": diff.DIFFERENT}
    assert validate.classify("change", [f, other], [])["verdict"] == validate.DETECTED
    rec = {"name": "c", "kind": "change", "expect": "quiet", "verdict": "quiet", "match": True, "seconds": 1.0,
           "functions": [f], "mutants_killed": 0, "mutants_total": 0, "slowdowns": [f]}
    m = validate.metrics([rec])
    assert m["checked"] == 1 and m["false_alarm"] == {"alarms": 0, "total": 1, "rate": 0.0}


def test_stop_does_not_rerun_a_recorded_slowdown(tmp_path, make_cfg, monkeypatch):
    from guardlib import audit
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n"})
    cfg = make_cfg(root)
    data = ledger.fresh("s")
    engine.record(cfg, data, [_slow_result()])
    t = diff.Target("m.py", "f", "function", [], "h1")
    monkeypatch.setattr(engine, "is_guarded_source", lambda cfg, rel: True)
    monkeypatch.setattr(engine, "targets_for", lambda cfg, rel, data: [t])

    def no_run(*a, **k):
        raise AssertionError("Stop re-ran a function whose slowdown is already recorded for this code")
    monkeypatch.setattr(audit.differ, "run", no_run)
    audit._behavior_findings(cfg, data, ["m.py"])
    assert next(f for f in data["findings"] if f["kind"] == "slowdown")["state"] == "open"


def test_session_map_and_report_show_slowdowns(tmp_path, make_cfg):
    from guardlib import report, sessionmap
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n", "c.py": "def c():\n    return 1\n"})
    cfg = make_cfg(root)
    data = ledger.fresh("s")
    caller = diff.Result("c.py", "c", diff.SLOWDOWN, "hc", inputs_compared=1, via="calls g", via_file="n.py", callees=["g"],
                         slowdowns=[{"call": "c()", "new_limit_s": 5, "old_seconds": 0.002, "source": "generated"}])
    engine.record(cfg, data, [_slow_result(), caller])
    assert data["callers_run"]["c.py::c"]["status"] == "slow"
    assert "ran past 5 s" in data["callers_run"]["c.py::c"]["example"]
    model = sessionmap.build(cfg, data)
    node = next(n for n in model["nodes"] if n["id"] == "m.py::f")
    assert node["status"] == ledger.SLOWDOWN
    html = sessionmap.render_html(model)
    assert "'Slowdown — not a proof of a wrong result':'slow'" in html and ".s-slow rect" in html and "--slow-bg" in html
    assert "run too: slower in the new code" in html and ".t-slowdown" in html
    steps = [s for s in report.story(data) if s["kind"] == "slowdown"]
    assert steps and "not a proof of a wrong result" in steps[0]["text"] and "f('aaa!')" in steps[0]["text"]
    # The caller's own slowdown is a finding too: the function it reaches (g) showed nothing in this check.
    assert "2 slowdowns (not a proof of a wrong result)" in report.summary(data, cfg)
    assert "**Slower:**" in report.render(data, cfg)


def test_backtest_maps_slowdown_without_a_wake(tmp_path, make_cfg, monkeypatch):
    from guardlib import backtest
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n"})
    cfg = make_cfg(root)
    monkeypatch.setattr(backtest.differ, "run", lambda *a, **k: [_slow_result()])
    rec = backtest.evaluate_edit(cfg, root, root, "m.py", "def f(x):\n    return x\n", "def f(x):\n    return x + 0\n", {})
    assert [f["status"] for f in rec["functions"]] == [ledger.SLOWDOWN]
    assert rec["wake"] is False


def test_ci_lists_slowdowns_and_never_fails_on_them(tmp_path, make_cfg):
    from guardlib import ci
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n"})
    cfg = make_cfg(root)
    out = ci.Outcome(base="abcdef1234", results=[_slow_result()])
    text = ci.report(cfg, out)
    assert "<details><summary>Slower in the new code (not counted as a difference)</summary>" in text
    assert diff.slowdown_text(_slow_result().slowdowns[0]) in text and "**0 behave differently**" in text
    assert all(ci.exit_code(cfg, out, fail_on) == 0 for fail_on in ("difference", "any", "none"))


def test_waived_slowdown_does_not_wake_again(tmp_path, make_cfg, monkeypatch):
    import argparse
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "legacyguard" / "scripts"))
    import lg_more
    root = write_tree(tmp_path / "p", {"m.py": "def f(x):\n    return x\n"})
    cfg = make_cfg(root)
    with ledger.locked(cfg.state_dir) as data:
        assert engine.record(cfg, data, [_slow_result()])
        fid = next(f["id"] for f in data["findings"] if f["kind"] == "slowdown")
    monkeypatch.setattr(lg_more, "_cfg", lambda: cfg)
    assert lg_more.cmd_waive(argparse.Namespace(id=fid, reason="expected: larger inputs now")) == 0
    assert ledger.load(cfg.state_dir)["findings"][0]["state"] == "waived"
    later = ledger.fresh("s2")  # a new session on the same code: the waiver holds and nothing wakes
    assert engine.record(cfg, later, [_slow_result()]) == []
    assert next(f for f in later["findings"] if f["kind"] == "slowdown")["state"] == "waived"


def test_wording_for_one_old_run_and_plural_note():
    one = diff.Result("m.py", "f", diff.SLOWDOWN, "h",
                      slowdowns=[{"call": "f(1)", "new_limit_s": 5, "old_seconds": 0.01, "source": "tests", "old_runs": 1}])
    wake = diff.format_wake(one, "LG-2")
    assert "The run of the code before the edit finished" in wake and "Both runs" not in wake
    two = diff.Result("m.py", "f", diff.SLOWDOWN, "h", slowdowns=[{**one.slowdowns[0], "old_runs": 2}] * 3)
    assert "Both runs of the code before the edit finished" in diff.format_wake(two, "LG-3")
    assert "(2 more such inputs)" in diff.format_note([two])
    assert "(1 more such input)" in diff.format_note([diff.Result("m.py", "f", diff.SLOWDOWN, "h", slowdowns=two.slowdowns[:2])])
    text = diff.slowdown_text(one.slowdowns[0])
    assert "while the old code finished in" in text and "returned" not in text


def test_call_limit_matches_the_worker():
    # Read from the file, not imported: importing the worker into the guard's own process is not its job.
    worker = (Path(__file__).resolve().parent.parent / "legacyguard" / "runner" / "lg_worker.py").read_text()
    import re
    assert int(re.search(r"^CALL_TIMEOUT_S = (\d+)", worker, re.M).group(1)) == diff.CALL_TIMEOUT_S
