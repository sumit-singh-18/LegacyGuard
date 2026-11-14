"""Carved inputs (roadmap 3c, Sprint 5 task E): the arguments callers pass to an edited function, recorded on the
baseline and replayed as that function's own inputs. Plus two follow-ups from earlier reviews."""
import json

from guardlib import engine, ledger
from runner import diff, lg_codec, mutants

from conftest import write_tree

# tqdm-4-like: the changed line runs only for total=None with a numeric scale other than 1. The annotations say what
# the generator builds (an int total, a bool scale), so generated inputs never get there. The caller passes exactly that.
FMT_OLD = '''
def format_meter(n: int, total: int, unit_scale: bool = False) -> str:
    if total is None and unit_scale not in (True, False, 1):
        return "%.1f" % (n * unit_scale)
    return str(n)
'''
FMT_NEW = FMT_OLD.replace('"%.1f"', '"%.2f"')
CALLER = '''
from pkg.fmt import format_meter


def progress(count: int, unit: str) -> str:
    return format_meter(count, None, len(unit) + 0.25)
'''


def _setup(tmp_path, make_cfg, old, new, callers, **overrides):
    files = {"pkg/__init__.py": "", **callers}
    root = write_tree(tmp_path / "proj", {**files, "pkg/fmt.py": new})
    cfg = make_cfg(root, **overrides)
    base = write_tree(cfg.state_dir / "baseline", {**files, "pkg/fmt.py": old})
    changed = diff.changed_targets("pkg/fmt.py", (base / "pkg/fmt.py").read_text(), (root / "pkg/fmt.py").read_text())
    deps = engine.dependents(cfg, "pkg/fmt.py", changed, {t.qualname for t in changed})
    return cfg, base, root, changed, deps


def test_edited_function_reached_only_through_its_caller_is_detected_by_carved_inputs(tmp_path, make_cfg):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, FMT_NEW, {"pkg/ui.py": CALLER})
    assert [d.qualname for d in deps] == ["progress"]
    # Without its callers the edited function's own inputs never reach the change.
    alone = diff.run(cfg, str(base), str(root), changed, harvest={})[0]
    assert alone.status == diff.UNTESTED and "reached 0 of 1" in alone.reason, alone
    assert alone.carved_captured == 0
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    r = results["format_meter"]
    assert r.status == diff.DIFFERENT, r
    assert r.differences[0]["source"] == "carved"
    assert "format_meter(" in r.differences[0]["call"] and "total=None" in r.differences[0]["call"]
    assert 0 < r.carved_captured <= diff.MAX_CARVED and r.carved_replayed == r.carved_captured
    assert "carved" in diff.format_wake(r, "LG-1")


def test_carved_calls_reported_in_pass_note(tmp_path, make_cfg):
    same = FMT_OLD.replace("return str(n)", "return str(n + 0)")  # same behaviour, change on the common path
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, same, {"pkg/ui.py": CALLER})
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    r = results["format_meter"]
    assert r.status == diff.DONE, r
    assert r.carved_captured > 0
    note = diff.format_note([r])
    assert r.carved_added == r.carved_captured == r.carved_replayed
    assert f"{r.carved_captured} calls recorded from its callers on the code before the edit; " \
           f"{r.carved_replayed} of the {r.carved_added} added ran to a return or an exception in both versions" in note


UNSTORABLE = '''
import threading
from pkg.fmt import format_meter


def locked(count: int) -> str:
    return format_meter(count, threading.Lock(), 2.5)
'''


def test_unencodable_captured_values_are_skipped_without_failing(tmp_path, make_cfg):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, FMT_NEW, {"pkg/ui.py": UNSTORABLE})
    assert [d.qualname for d in deps] == ["locked"]
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    r = results["format_meter"]
    assert r.carved_captured == 0 and r.carved_unstored > 0
    assert r.status == diff.UNTESTED and "reached 0 of 1" in r.reason  # as without carving: never a failed check
    assert f"No call from its callers could be recorded: {r.carved_unstored} calls (repeats included) had a value that could not be stored" \
        in diff.format_note([r])
    assert results["locked"].status != diff.BROKEN


def test_carved_inputs_identical_across_old_and_new_runs(tmp_path, make_cfg, monkeypatch):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, FMT_NEW, {"pkg/ui.py": CALLER})
    seen = []
    real = diff._run_worker

    def spy(cfg_, py, root_, sys_path, functions, timeout, warm_names=None):
        out = real(cfg_, py, root_, sys_path, functions, timeout, warm_names)
        seen.append((root_, functions))
        return out
    monkeypatch.setattr(diff, "_run_worker", spy)
    r = next(x for x in diff.run(cfg, str(base), str(root), changed + deps, harvest={}) if x.qualname == "format_meter")
    assert r.status == diff.DIFFERENT
    capture = [fs for root_, fs in seen if any(f.get("capture") for f in fs)]
    assert len(capture) == 1 and seen[0][0] == str(base)  # one capture pass, first, on the baseline
    main = [fs for _, fs in seen if not any(f.get("capture") for f in fs)]
    assert len(main) == 3
    inputs = [json.dumps(next(f for f in fs if f["qualname"] == "format_meter")["inputs"], sort_keys=True) for fs in main]
    assert inputs[0] == inputs[1] == inputs[2]
    assert any(i["source"] == "carved" for i in json.loads(inputs[0]))


def test_no_capture_pass_without_callers(tmp_path, make_cfg, monkeypatch):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, FMT_NEW, {"pkg/ui.py": CALLER})
    calls = []
    real = diff._run_worker
    monkeypatch.setattr(diff, "_run_worker", lambda *a, **k: calls.append(a[4]) or real(*a, **k))
    diff.run(cfg, str(base), str(root), changed, harvest={})
    assert len(calls) == 3 and not any(f.get("capture") for fs in calls for f in fs)


def test_failed_capture_pass_fails_open(tmp_path, make_cfg, monkeypatch):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, FMT_NEW, {"pkg/ui.py": CALLER})
    real = diff._run_worker

    def broken(*a, **k):
        if any(f.get("capture") for f in a[4]):
            return {diff.FAILED: "timed out"}
        return real(*a, **k)
    monkeypatch.setattr(diff, "_run_worker", broken)
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    assert results["format_meter"].status == diff.UNTESTED and results["format_meter"].carved_captured == 0
    assert results["progress"].status == diff.DIFFERENT  # the caller itself still runs


METHOD_OLD = '''
class Meter:
    def __init__(self, width: int = 10):
        self.width = width

    def fmt(self, n: int, total: int) -> str:
        if total is None:
            return "%d/%d" % (n, self.width)
        return str(n)
'''
METHOD_NEW = METHOD_OLD.replace('"%d/%d"', '"%d of %d"')
METHOD_CALLER = '''
from pkg.fmt import Meter


def show(count: int) -> str:
    meter = Meter(count % 7 + 3)
    return meter.fmt(count, None)
'''


def test_method_self_is_carved_when_encodable(tmp_path, make_cfg):
    cfg, base, root, changed, _ = _setup(tmp_path, make_cfg, METHOD_OLD, METHOD_NEW, {"pkg/ui.py": METHOD_CALLER})
    # Without an index the call graph does not resolve meter.fmt(...); the caller is set up as dependents would.
    show = diff.Target("pkg/ui.py", "show", "function", [{"name": "count", "annotation": "int"}], "hs", via="calls fmt",
                       via_file="pkg/fmt.py", **diff.caller_gate(changed))
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + [show], harvest={})}
    r = results["Meter.fmt"]
    assert r.status == diff.DIFFERENT and r.differences[0]["source"] == "carved", r
    assert r.differences[0]["has_self"] and r.carved_replayed == r.carved_captured > 0


def test_worker_capture_records_arguments_and_caps(tmp_path, run_worker):
    root = write_tree(tmp_path / "p", {"m.py": '''
import threading


def callee(a, b=2, *rest, key=None, **more):
    return a


def gen(x):
    yield x
    yield x


def caller(n):
    callee(threading.Lock())
    for i in range(n):
        callee(i % 50, i, 7, key="k", extra=[i])
    list(gen(n))
    return n
'''})
    inputs = [{"args": [{"name": "n", "kind": "pos", "value": lg_codec.encode_input(60)}], "source": "generated"}]
    out = run_worker(root, [{"module": "m", "qualname": "caller", "inputs": inputs,
                             "capture": [{"module": "m", "qualname": "callee", "max": 40},
                                         {"module": "m", "qualname": "gen", "max": 40}]}])["m:caller"]
    got = out["captured"]["m:callee"]
    assert len(got) == 40 and out["unstored"]["m:callee"] == 1
    first = got[0]
    assert [(a["name"], a["kind"]) for a in first["args"]] == [("a", "pos"), ("b", "pos"), ("key", "kw"), ("rest", "varargs"), ("more", "varkw")]
    assert lg_codec.decode(first["args"][4]["value"]) == {"extra": [0]} and not first["has_self"]
    assert len(out["captured"]["m:gen"]) == 1  # a generator's resumes are not new calls


# ---- a recorded self when the class changed (review of task E) ----

RESHAPED_NEW = METHOD_OLD.replace("        self.width = width\n", "        self.width = width\n        self.sep = \"/\"\n") \
                         .replace('"%d/%d" % (n, self.width)', '"%d%s%d" % (n, self.sep, self.width)')


def _old_meter_pickle(base):
    """A Meter built by the baseline code, encoded the way the harvest plugin stores a test's `self`."""
    import importlib
    import sys
    sys.path.insert(0, str(base))
    try:
        mod = importlib.import_module("pkg.fmt")
        return lg_codec.encode_input(mod.Meter(4))
    finally:
        sys.path.remove(str(base))
        for name in [n for n in sys.modules if n == "pkg" or n.startswith("pkg.")]:
            del sys.modules[name]


def test_recorded_self_of_a_reshaped_class_is_rebuilt(tmp_path, make_cfg):
    assert "self.sep" in RESHAPED_NEW
    cfg, base, root, changed, _ = _setup(tmp_path, make_cfg, METHOD_OLD, RESHAPED_NEW, {"pkg/ui.py": METHOD_CALLER})
    fmt = next(t for t in changed if t.qualname == "Meter.fmt")
    show = diff.Target("pkg/ui.py", "show", "function", [{"name": "count", "annotation": "int"}], "hs", via="calls fmt",
                       via_file="pkg/fmt.py", **diff.caller_gate([fmt]))
    # A test's own pickled self, of the old shape, too.
    harvested = {"pkg/fmt.py::Meter.fmt": {"inputs": [[{"name": "self", "kind": "pos", "value": _old_meter_pickle(base)},
                                                       {"name": "n", "kind": "pos", "value": lg_codec.encode_input(2)},
                                                       {"name": "total", "kind": "pos", "value": lg_codec.encode_input(None)}]]}}
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + [show], harvest=harvested)}
    r = results["Meter.fmt"]
    # No input runs the new code on an old-shaped instance, and fmt is judged on what it does to self (nothing), not on
    # the field the new __init__ adds: that is __init__'s own difference.
    assert r.status != diff.DIFFERENT, r.differences[:2]
    assert r.carved_captured > 0 and r.carved_replayed == r.carved_added  # still recorded and replayed, on fresh instances
    init = results["Meter.__init__"]
    assert init.status == diff.DIFFERENT and init.differences[0]["state"]
    assert diff._class_reshaped(str(base), str(root), fmt)
    same_shape = METHOD_NEW  # only fmt itself changed: the recorded self is kept (test_method_self_is_carved_when_encodable)
    root2 = write_tree(tmp_path / "proj2", {"pkg/__init__.py": "", "pkg/fmt.py": same_shape})
    assert not diff._class_reshaped(str(base), str(root2), fmt)


def test_method_judged_on_its_own_effect_on_self(tmp_path, run_worker):
    root = write_tree(tmp_path / "p", {"m.py": '''
class C:
    def __init__(self):
        self.a = 1
        self.items = []
        self.gone = 0

    def read(self):
        return self.a

    def work(self):
        self.items.append(self.a)
        self.b = 2
        del self.gone
        return None
'''})
    out = run_worker(root, [{"module": "m", "qualname": f"C.{n}", "inputs": [{"args": [], "source": "generated"}]}
                            for n in ("read", "work", "__init__")])
    assert "state" not in out["m:C.read"]["results"][0]  # read nothing changed: no state at all
    changed = {lg_codec.display(k): v for k, v in out["m:C.work"]["results"][0]["state"]["self"]["state"]["v"]}
    assert changed == {"'b'": {"t": "v", "v": 2}, "'gone'": {"t": "removed"}, "'items'": lg_codec.canon([1])}
    init = out["m:C.__init__"]["results"][0]["state"]["self"]["state"]["v"]
    assert len(init) == 3  # __init__ is compared on the whole instance it leaves


BASE_OLD = '''
class Base:
    def __init__(self, width: int = 10):
        self.width = width
'''
SUB_OLD = '''
from pkg.base import Base


class Meter(Base):
    def fmt(self, n: int, total: int) -> str:
        if total is None:
            return "%d/%d" % (n, self.width)
        return str(n)
'''
SUB_NEW = SUB_OLD.replace('"%d/%d" % (n, self.width)', '"%d%s%d" % (n, self.sep, self.width)')
BASE_NEW = BASE_OLD + "        self.sep = \"/\"\n"


def test_reshaped_base_class_counts(tmp_path, make_cfg):
    # The base in the same file (the reviewer's case) and in another module: both make the recorded self unusable.
    same_old = BASE_OLD + "\n\n" + SUB_OLD.replace("from pkg.base import Base\n", "")
    same_new = BASE_NEW + "\n\n" + SUB_NEW.replace("from pkg.base import Base\n", "")
    cfg, base, root, changed, _ = _setup(tmp_path, make_cfg, same_old, same_new, {"pkg/ui.py": METHOD_CALLER})
    fmt = next(t for t in changed if t.qualname == "Meter.fmt")
    show = diff.Target("pkg/ui.py", "show", "function", [{"name": "count", "annotation": "int"}], "hs", via="calls fmt",
                       via_file="pkg/fmt.py", **diff.caller_gate([fmt]))
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + [show], harvest={})}
    assert results["Meter.fmt"].status != diff.DIFFERENT, results["Meter.fmt"].differences[:2]
    assert results["Meter.fmt"].carved_captured > 0
    two = tmp_path / "two"
    files = {"pkg/__init__.py": "", "pkg/fmt.py": SUB_NEW}
    old_tree = write_tree(two / "old", {**files, "pkg/fmt.py": SUB_OLD, "pkg/base.py": BASE_OLD})
    new_tree = write_tree(two / "new", {**files, "pkg/base.py": BASE_NEW})
    sub = diff.Target("pkg/fmt.py", "Meter.fmt", "method", [], "h")
    assert diff._class_reshaped(str(old_tree), str(new_tree), sub)
    same_base = write_tree(two / "same", {**files, "pkg/base.py": BASE_OLD})
    assert not diff._class_reshaped(str(old_tree), str(same_base), sub)


# ---- readable state differences ----

def test_state_difference_names_what_changed():
    obj = lambda pairs, effect=True: {"t": "obj", "cls": "m:Acc", "effect": effect,  # noqa: E731
                                      "state": {"t": "dict", "v": [[lg_codec.canon(k), lg_codec.canon(v)] for k, v in pairs]}}
    before = {"k": "return", "v": lg_codec.canon(None), "s": {"self": obj([("last", 1)])}}
    after = {"k": "return", "v": lg_codec.canon(None)}
    assert diff._show_outcome(before, after) == "None, then self.last=1"
    assert diff._show_outcome(after, before) == "None, then self unchanged"
    two = {"k": "return", "v": lg_codec.canon(None), "s": {"self": obj([("last", 2), ("total", "x" * 100)])}}
    shown = diff._show_outcome(two, before)
    assert shown.startswith("None, then self.last=2, self.total='xxx") and shown.endswith("…") and len(shown) < 120
    gone = {"k": "return", "v": lg_codec.canon(None), "s": {"self": {**obj([]), "state": {"t": "dict", "v": [[lg_codec.canon("x"), {"t": "removed"}]]}}}}
    assert diff._show_outcome(gone, after) == "None, then self.x removed"
    lst = lambda v: {"k": "return", "v": lg_codec.canon(0), "s": {"items": lg_codec.canon(v)}}  # noqa: E731
    assert diff._show_outcome(lst([1, 2, 3]), lst([1, 9, 3])) == "0, then items[1]=2"
    assert diff._show_outcome(lst([1, 2]), lst([1, 2, 2])) == "0, then items=[1, 2]"
    dct = lambda v: {"k": "return", "v": lg_codec.canon(0), "s": {"d": lg_codec.canon(v)}}  # noqa: E731
    assert diff._show_outcome(dct({"a": 1, "b": 2}), dct({"a": 1})) == "0, then d['b']=2"
    assert diff._show_outcome(dct({"a": 1}), dct({"a": 1, "b": 2})) == "0, then d['b'] absent"
    wake = diff.format_wake(diff.Result("m.py", "Acc.add", diff.DIFFERENT, "h", inputs_compared=1, differences=[
        {"call": "add(x=1)", "before": diff._show_outcome(before, after), "after": diff._show_outcome(after, before), "source": "tests"}]), "LG-1")
    assert "add(x=1)  before None, then self.last=1  after None, then self unchanged  [tests]" in wake


# ---- accounting ----

def _c(v, **extra):
    return {"args": [{"name": "x", "kind": "pos", "value": lg_codec.encode_input(v)}], "has_self": False, **extra}


def test_merge_carved_counts_duplicates_and_stays_within_max_inputs():
    items = [_c(1, source="tests"), _c(2, source="generated"), _c(3, source="generated")]
    carved = [_c(1, source="carved"), _c(5, source="carved"), _c(6, source="carved"), _c(7, source="carved")]
    merged, counts = diff._merge_carved(items, carved, 3)
    assert len(merged) == 3 and merged[0] == items[0]  # the head is never cut
    assert [m.get("carved_id") for m in merged[1:]] == [0, 1]
    assert counts == {"already": 1, "cut": 1, "added": 2}
    merged, counts = diff._merge_carved(items, carved, 10)
    assert len(merged) == 6 and counts == {"already": 1, "cut": 0, "added": 3}


def test_replayed_counts_only_return_or_raise_in_both_versions():
    t = diff.Target("m.py", "f", "function", [{"name": "x"}], "h")
    items = [_c(i, source="carved", carved_id=i) for i in range(5)]
    old = [{"outcome": "return", "value": lg_codec.canon(1)}, {"outcome": "raise", "type": "E", "message": ""},
           {"outcome": "blocked", "detail": ""}, {"outcome": "timeout"}, {"outcome": "return", "value": lg_codec.canon(1)}]
    new = [old[0], old[1], old[0], old[0], {"outcome": "exit", "detail": "SystemExit"}]
    run = lambda rs: {"status": "ran", "results": rs, "lines": None}  # noqa: E731
    r = diff.compare_one(t, items, run(old), run(old), run(new), True)
    assert r.carved_replayed == 2


def test_carved_text_cases():
    r = diff.Result("m.py", "f", diff.DONE, "h", carved_captured=5, carved_already=1, carved_added=3, carved_replayed=2, carved_unstored=4)
    assert diff.carved_text(r) == ("5 calls recorded from its callers on the code before the edit (1 already among its inputs; 1 left out at "
                                   "the input limit; 4 more calls, repeats included, had a value that could not be stored); 2 of the 3 added "
                                   "ran to a return or an exception in both versions")
    assert diff.carved_text(diff.Result("m.py", "f", diff.DONE, "h")) == ""


# ---- time: a slow recording run ----

def _timing(tmp_path, make_cfg, monkeypatch, capture_reply, main_reply=None):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg, FMT_OLD, FMT_NEW, {"pkg/ui.py": CALLER}, behavioral_diff={"timeout_s": 8})
    deadlines = []
    real = diff._run_worker

    def fake(cfg_, py, root_, sys_path, functions, timeout, warm_names=None):
        if any(f.get("capture") for f in functions):
            import time
            time.sleep(0.6)
            return capture_reply(real, (cfg_, py, root_, sys_path, functions, timeout, warm_names))
        deadlines.append(timeout)
        return main_reply or real(cfg_, py, root_, sys_path, functions, timeout, warm_names)
    monkeypatch.setattr(diff, "_run_worker", fake)
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}, deadlines


def test_recording_run_that_fails_keeps_the_limit_and_says_so(tmp_path, make_cfg, monkeypatch):
    results, deadlines = _timing(tmp_path, make_cfg, monkeypatch, lambda real, a: {diff.FAILED: "timed out"})
    assert deadlines == [8.0, 8.0, 8.0]  # nothing recorded: the three runs keep the full limit
    r = results["format_meter"]
    assert r.carved_captured == 0 and "the run that records its callers' calls stopped after" in r.carved_note
    assert "(timed out), so none were recorded" in r.carved_note
    assert "The run that records its callers' calls stopped after" in diff.format_note([r])


def test_main_timeout_after_a_long_recording_run_says_so(tmp_path, make_cfg, monkeypatch):
    results, deadlines = _timing(tmp_path, make_cfg, monkeypatch, lambda real, a: real(*a), main_reply={diff.FAILED: "timed out"})
    assert len(deadlines) == 3 and all(6.0 <= d < 8.0 - 0.5 for d in deadlines)  # the recording time came out of the limit
    r = results["format_meter"]
    assert r.status == diff.UNTESTED and r.reason.startswith("timed out: the runs had ") and "spent recording its callers' calls" in r.reason


# ---- follow-up (a): the old module's text reaches the mutant planner ----

def test_with_mutants_passes_old_module_source(tmp_path, monkeypatch):
    base = write_tree(tmp_path / "base", {"m.py": "from re import sub\n\n\ndef f(s):\n    return sub(r'a+', 'b', s)\n"})
    t = diff.Target("m.py", "f", "function", [], "h", old_gate=[[5, 5]], source="def f(s):\n    return sub(r'a+', 'b', s)\n", source_start=4)
    seen = {}
    real = mutants.generate

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)
    monkeypatch.setattr(mutants, "generate", spy)
    out = diff._with_mutants([{"module": "m", "qualname": "f", "inputs": []}], {"m:f": (t, [])}, 8, str(base))
    assert seen["module_source"].startswith("from re import sub")
    assert any(m["change"].startswith("regex") for m in out[0]["mutants"]["mutants"])
    # An unreadable baseline leaves the plan as before.
    out2 = diff._with_mutants([{"module": "m", "qualname": "f", "inputs": []}], {"m:f": (t, [])}, 8, str(tmp_path / "missing"))
    assert out2[0].get("mutants") is not None and seen["module_source"] is None


# ---- follow-up (b): a caller is quiet when any edited function it reaches has its own difference ----

def test_caller_quiet_when_any_callee_differs(tmp_path, make_cfg):
    root = write_tree(tmp_path / "p", {"c.py": "def c():\n    return 1\n"})
    cfg = make_cfg(root)
    own_a = diff.Result("m.py", "a", diff.DIFFERENT, "ha", inputs_compared=1,
                        differences=[{"call": "a(1)", "before": "1", "after": "2", "source": "tests"}])
    own_b = diff.Result("m.py", "b", diff.UNTESTED, "hb", reason="no input ran to completion")
    caller = diff.Result("c.py", "c", diff.DIFFERENT, "hc", inputs_compared=1, via="calls b", via_file="m.py", callees=["b", "a"],
                         differences=[{"call": "c()", "before": "1", "after": "2", "source": "generated"}])
    data = ledger.fresh("s")
    engine.record(cfg, data, [own_a, own_b, caller])
    assert not any(f["kind"] == "behavior_difference" and f["item"] == "c" for f in data["findings"])
    # From the ledger too (at Stop only the caller is re-run).
    data2 = ledger.fresh("s")
    data2["findings"].append({"id": "LG-9", "kind": "behavior_difference", "state": "open", "file": "m.py", "item": "a"})
    engine.record(cfg, data2, [caller])
    assert not any(f["kind"] == "behavior_difference" and f["item"] == "c" for f in data2["findings"])
    # A caller whose callees all agree still reports.
    data3 = ledger.fresh("s")
    engine.record(cfg, data3, [own_b, caller])
    assert any(f["kind"] == "behavior_difference" and f["item"] == "c" for f in data3["findings"])



def test_reshaped_base_class_found_in_a_src_layout(tmp_path):
    # src/pkg/base.py imported as `from pkg.base import Base`: the base must be found through the package root.
    from runner import diff as _diff
    files = {"src/pkg/__init__.py": "", "src/pkg/base.py": "class Base:\n    def __init__(self):\n        self.w = 3\n",
             "src/pkg/meter.py": "from pkg.base import Base\n\nclass Meter(Base):\n    def fmt(self, n):\n        return n\n"}
    old = write_tree(tmp_path / "old", files)
    new = write_tree(tmp_path / "new", {**files, "src/pkg/base.py": files["src/pkg/base.py"].replace("self.w = 3", "self.w = 3\n        self.sep = '/'")})
    found = _diff._imported_bases({}, str(new), "src/pkg/meter.py", "Meter")
    assert ("src/pkg/base.py", "Base") in found
    assert old.exists()
