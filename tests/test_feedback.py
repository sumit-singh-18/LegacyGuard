"""Task 6.8e: what keeps a changed statement from running is named in the untested reason, and a bounded second pass
of inputs aimed at those conditions (source "feedback", weak) reaches what the first pass missed.

The fixtures are generic shapes: an early-return guard, a dict-key check, a type-and-length check, string-method and
regex guards, a branch needing two parameters set consistently, a dead line, and a behaviour-preserving edit. Each
end-to-end case runs once with the second pass's input builder switched off (the first pass alone misses) and once
as shipped."""
import time

import pytest

from runner import diff, feedback

from conftest import write_tree


def run_case(tmp_path, make_cfg, old: str, new: str, name: str, second: bool = True, monkeypatch=None, tag: str = ""):
    module = "pkg_fb/mod.py"
    base = write_tree(tmp_path / f"base{tag}", {"pkg_fb/__init__.py": "", module: old})
    work = write_tree(tmp_path / f"work{tag}", {"pkg_fb/__init__.py": "", module: new})
    cfg = make_cfg(work)
    targets = diff.changed_targets(module, (base / module).read_text(), (work / module).read_text())
    if not second:
        monkeypatch.setattr(feedback, "build", lambda *a, **k: [])
    started = time.monotonic()
    results = diff.run(cfg, str(base), str(work), targets, harvest={})
    if not second:
        monkeypatch.undo()
    r = next(r for r in results if r.qualname == name)
    return r, time.monotonic() - started


EARLY_OLD = '''
def lookup(table, key):
    if not table:
        return None
    if key not in table:
        return None
    value = table[key]
    return value * 2
'''
EARLY_NEW = EARLY_OLD.replace("value * 2", "value * 3")

DICT_OLD = '''
def level(opts, base):
    if "verbose" in opts and opts["verbose"]:
        return base + 2
    return base
'''
DICT_NEW = DICT_OLD.replace("base + 2", "base + 3")

SEQ_OLD = '''
def head(xs):
    if isinstance(xs, (list, tuple)) and len(xs) > 2:
        return xs[0] + xs[2]
    return None
'''
SEQ_NEW = SEQ_OLD.replace("xs[0] + xs[2]", "xs[0] + xs[1]")

STR_OLD = '''
def kind(path):
    if path.startswith("v") and path.endswith(".py"):
        return "versioned:" + path[1:-3]
    return "plain"
'''
STR_NEW = STR_OLD.replace("path[1:-3]", "path[1:-2]")

RX_OLD = r'''
import re


def code_of(code):
    if re.fullmatch(r"[a-z]{2}-\d{3}", code):
        return code.upper()
    return None
'''
RX_NEW = RX_OLD.replace("code.upper()", "code.title()")

TWO_OLD = '''
def strip_suffix(text, suffix):
    if text.endswith(suffix) and len(suffix) > 4 and text != suffix:
        return text[:-len(suffix)]
    return text
'''
TWO_NEW = TWO_OLD.replace("text[:-len(suffix)]", "text[:-len(suffix) - 1]")

DEAD_OLD = '''
def sign(x):
    if x > 0:
        return 1
    return 0
    y = x + 1
'''
DEAD_NEW = DEAD_OLD.replace("y = x + 1", "y = x + 2")

SAME_OLD = '''
def total(xs):
    if isinstance(xs, (list, tuple)) and len(xs) > 2:
        result = sum(xs)
        return result
    return None
'''
SAME_NEW = SAME_OLD.replace("        result = sum(xs)\n", "        result = 0\n        for v in xs:\n            result += v\n")

DIFFERENT_CASES = [
    ("early_return", EARLY_OLD, EARLY_NEW, "lookup"),
    ("dict_key", DICT_OLD, DICT_NEW, "level"),
    ("type_and_length", SEQ_OLD, SEQ_NEW, "head"),
    ("string_methods", STR_OLD, STR_NEW, "kind"),
    ("regex", RX_OLD, RX_NEW, "code_of"),
    ("two_parameters", TWO_OLD, TWO_NEW, "strip_suffix"),
]


@pytest.mark.parametrize("label,old,new,name", DIFFERENT_CASES, ids=[c[0] for c in DIFFERENT_CASES])
def test_second_pass_reaches_what_the_first_missed(tmp_path, make_cfg, monkeypatch, label, old, new, name):
    first, first_s = run_case(tmp_path, make_cfg, old, new, name, second=False, monkeypatch=monkeypatch, tag="1")
    assert first.status == diff.UNTESTED, (first.status, first.reason)
    assert first.statements_reached < first.statements_total
    assert " is under " in first.reason or " only runs when " in first.reason, first.reason   # the chain, in words
    both, both_s = run_case(tmp_path, make_cfg, old, new, name, tag="2")
    assert both.status == diff.DIFFERENT, both.reason
    assert both.feedback_tried and both.feedback_tried <= feedback.MAX_FEEDBACK_ITEMS
    assert any(d["source"] == "feedback" for d in both.differences), [d["source"] for d in both.differences]
    print(f"{label}: first pass {first_s:.2f} s, with second pass {both_s:.2f} s (+{both_s - first_s:.2f} s)")


def test_behaviour_preserving_edit_is_verified_only_once_every_line_is_reached(tmp_path, make_cfg, monkeypatch):
    first, _ = run_case(tmp_path, make_cfg, SAME_OLD, SAME_NEW, "total", second=False, monkeypatch=monkeypatch, tag="1")
    assert first.status == diff.UNTESTED and first.statements_reached < first.statements_total, first.reason
    both, _ = run_case(tmp_path, make_cfg, SAME_OLD, SAME_NEW, "total", tag="2")
    assert both.status == diff.DONE, both.reason
    assert both.statements_reached == both.statements_total
    assert both.feedback_tried and both.feedback_reached == both.statements_total - first.statements_reached


def test_dead_changed_line_stays_untested_with_its_cause(tmp_path, make_cfg):
    r, _ = run_case(tmp_path, make_cfg, DEAD_OLD, DEAD_NEW, "sign")
    assert r.status == diff.UNTESTED
    assert r.statements_reached < r.statements_total
    assert "cannot run: it follows `return 0`" in r.reason, r.reason
    assert r.feedback_tried == 0 and "cannot run" in r.feedback_note


# ---- diagnosis, without running anything ----

CHAIN_SRC = '''def install(d, hooks_dir=None):
    if not os.path.isdir(d):
        return
    for name in names:
        if hooks_dir is None:
            hooks_dir = d
        try:
            copy(name)
        except KeyError:
            log(name)
'''


def test_diagnose_names_enclosing_conditions_and_early_exits():
    plans = feedback.diagnose(CHAIN_SRC, 50, [[55, 55], [59, 59]], executed=[51, 52, 53, 54])
    by_line = {p["line"]: p["text"] for p in plans}
    assert by_line[55] == ("line 55 only runs when the `if not os.path.isdir(d)` on line 51 does not return, is inside the "
                           "loop `for name in names` (line 53), so `names` must not be empty and is under `if hooks_dir is "
                           "None` (line 54); the inputs reached line 54 but `hooks_dir is None` was never true")
    assert "in `except KeyError` (line 58), so the try above it must raise" in by_line[59]


def test_diagnose_else_while_assert_and_nested():
    src = '''def f(x, y):
    assert x
    while y:
        if x > 1:
            pass
        else:
            z = 1
    def g():
        return 2
'''
    plans = feedback.diagnose(src, 1, [[7, 7], [9, 9]])
    texts = [p["text"] for p in plans]
    assert texts[0] == ("line 7 only runs when `assert x` on line 2 passes, is inside `while y` (line 3) and runs only "
                        "when `x > 1` (line 4) is false")
    assert "inside the nested function `g` (line 8)" in texts[1]


def test_build_meets_whole_chain_and_stays_bounded_and_deterministic():
    src = '''def head(xs, tag):
    if not isinstance(xs, (list, tuple)):
        return None
    if len(xs) > 2 and tag.startswith("v") and tag.endswith(".py"):
        return xs[0]
'''
    sym = {"params": [{"name": "xs", "annotation": None, "default": None}, {"name": "tag", "annotation": None, "default": None}]}
    from runner import lg_codec
    base = {"args": [{"name": "xs", "kind": "pos", "value": lg_codec.encode_input("abc")},
                     {"name": "tag", "kind": "pos", "value": lg_codec.encode_input("")}], "source": "generated", "weak": True}
    plans = feedback.diagnose(src, 1, [[5, 5]])
    items = feedback.build(sym, [base], None, plans)
    assert 0 < len(items) <= feedback.MAX_FEEDBACK_ITEMS
    assert all(i["source"] == "feedback" and i["weak"] for i in items)
    first = {a["name"]: lg_codec.decode(a["value"]) for a in items[0]["args"]}
    assert isinstance(first["xs"], (list, tuple)) and len(first["xs"]) > 2
    assert first["tag"].startswith("v") and first["tag"].endswith(".py")
    assert items == feedback.build(sym, [base], None, plans)


def test_build_respects_type_hints():
    src = '''def f(n: int):
    if n == "x":
        return 1
'''
    sym = {"params": [{"name": "n", "annotation": "int", "default": None}]}
    from runner import lg_codec
    base = {"args": [{"name": "n", "kind": "pos", "value": lg_codec.encode_input(0)}], "source": "generated"}
    items = feedback.build(sym, [base], None, feedback.diagnose(src, 1, [[3, 3]]))
    values = [lg_codec.decode(i["args"][0]["value"]) for i in items]
    assert all(isinstance(v, int) for v in values), values


def test_conditions_calling_project_code_are_never_evaluated():
    import ast
    ev = feedback._Evaluator({"x"}, {})
    assert ev.value(ast.parse("os.remove(x)", mode="eval").body, {"x": "a"}) == (False, None)
    assert ev.value(ast.parse("x.__class__", mode="eval").body, {"x": "a"}) == (False, None)
    assert ev.value(ast.parse("helper(x)", mode="eval").body, {"x": "a"}) == (False, None)
    assert ev.value(ast.parse("x.startswith('a')", mode="eval").body, {"x": "ab"}) == (True, True)


def test_regex_examples_match():
    import re
    for pattern, fn in ((r"[a-z]{2}-\d{3}", "fullmatch"), (r"^v(\d+)\.(\d+)$", "match"), (r"(ab|cd)+x?", "search")):
        got = feedback._regex_examples(pattern, 0, fn)
        assert got and all(getattr(re, fn)(pattern, s) for s in got), (pattern, got)


def test_worker_reports_each_inputs_lines_and_carries_time_over(tmp_path, run_worker):
    from conftest import call
    root = write_tree(tmp_path / "proj", {"m.py": "def f(x):\n    if x:\n        return 1\n    return 0\n"})
    out = run_worker(root, [{"module": "m", "qualname": "f", "inputs": [call(1), call(0)], "input_lines": True}])["m:f"]
    assert out["input_lines"] == [[2, 3], [2, 4]]
    assert out["lines"] == [2, 3, 4]
    plain = run_worker(root, [{"module": "m", "qualname": "f", "inputs": [call(1)]}])["m:f"]
    assert "input_lines" not in plain
    # A second pass starting with the first pass's timeouts already at the limit runs nothing.
    spent = run_worker(root, [{"module": "m", "qualname": "f", "inputs": [call(1)], "timeouts_before": 10}])["m:f"]
    assert spent["results"] == [{"outcome": "skipped_slow"}]


def test_merge_counts_only_the_lines_of_kept_second_pass_inputs():
    first = {"status": "ran", "results": [{"outcome": "return"}], "lines": [2, 4], "input_lines": [[2, 4]]}
    second = {"status": "ran", "results": [{"outcome": "raise"}, {"outcome": "return"}], "lines": [2, 3, 5],
              "input_lines": [[2, 3], [2, 5]]}
    merged = diff._merge_run(first, second, [False, True])
    assert merged["lines"] == [2, 4, 5]          # line 3 ran only under an input that was not compared
    assert len(merged["results"]) == 3 and merged["input_lines"][1:] == [[2, 3], [2, 5]]
    assert diff._merge_run(first, second, [True, True])["lines"] == [2, 3, 4, 5]
    # Without a keep list, or with lengths that do not match the second pass's inputs, no second-pass line counts.
    assert diff._merge_run(first, second)["lines"] == [2, 4]
    assert diff._merge_run(first, second, [True])["lines"] == [2, 4]
    assert diff._merge_run(first, {**second, "input_lines": [[2, 3]]}, [True, True])["lines"] == [2, 4]
    assert diff._merge_run(first, {**second, "input_lines": None}, [True, True])["lines"] == [2, 4]
    # Not the same way (a different `self`): the first run stands alone.
    assert diff._merge_run(first, {**second, "self_recipe": "other"}, [True, True]) is first


RAISE_OLD = '''
def pick(opts):
    if "k" in opts and opts["k"]:
        return int(opts["k"]) + 1
    return 0
'''
RAISE_NEW = RAISE_OLD.replace("int(opts[\"k\"]) + 1", "int(opts[\"k\"]) + 2")


def test_second_pass_lines_where_both_versions_raised_do_not_count(tmp_path, make_cfg, monkeypatch):
    # Every second-pass input makes int() raise in both versions: the line ran, but nothing was compared on it.
    real = feedback.build
    monkeypatch.setattr(feedback, "build", lambda *a, **k: [
        {**i, "args": [{**i["args"][0], "value": {"t": "dict", "v": [[{"t": "v", "v": "k"}, {"t": "v", "v": "x"}]]}}]}
        for i in real(*a, **k)[:1]])
    r, _ = run_case(tmp_path, make_cfg, RAISE_OLD, RAISE_NEW, "pick")
    assert r.status == diff.UNTESTED and r.statements_reached < r.statements_total, r.reason
    assert r.feedback_tried and r.feedback_raised == 1 and r.feedback_reached == 0
    assert "ran 1 more but both versions raised there, so it does not count" in r.reason, r.reason


NOISY_OLD = '''
import os


def token(opts):
    if "k" in opts and opts["k"]:
        return os.urandom(8) + b"a"
    return b""
'''
NOISY_NEW = NOISY_OLD.replace('b"a"', 'b"b"')


def test_second_pass_lines_on_inputs_dropped_as_non_deterministic_do_not_count(tmp_path, make_cfg):
    r, _ = run_case(tmp_path, make_cfg, NOISY_OLD, NOISY_NEW, "token")
    assert r.status == diff.UNTESTED, r.reason
    assert r.statements_reached < r.statements_total
    assert r.feedback_tried and r.feedback_dropped == 1 and r.feedback_reached == 0, r.reason
    assert "only on inputs that were not compared" in r.reason, r.reason


def test_second_pass_only_for_unreached_reasons():
    t = diff.Target("m.py", "f", "function", [], "h")
    r = diff.Result("m.py", "f", diff.UNTESTED, "h", reason="no input ran to completion", unreached_ranges=[[3, 3, False]])
    assert diff._wants_second_pass(t, r)
    for reason in ("the function opened a network connection", "inputs could not be set up (x)",
                   "non-deterministic: old code disagreed with itself on 2 of 3 inputs"):
        assert not diff._wants_second_pass(t, diff.Result("m.py", "f", diff.UNTESTED, "h", reason=reason, unreached_ranges=[[3, 3, False]]))
    caller = diff.Target("m.py", "g", "function", [], "h", via="calls f")
    assert not diff._wants_second_pass(caller, r)


def test_regex_conditions_off_the_main_thread_use_short_text_only():
    import ast
    import threading
    ev = feedback._Evaluator({"x"}, {})
    node = ast.parse("re.match('(a+)+$', x)", mode="eval").body
    got = {}
    worker = threading.Thread(target=lambda: got.update(long=ev.value(node, {"x": "a" * 40 + "b"}),
                                                        short=ev.value(node, {"x": "aa"})))
    worker.start()
    worker.join(10)
    assert got["long"] == (False, None)
    assert got["short"][0] is True and got["short"][1]


# ---- reviewer corrections: bounded build and evaluation, time budget, hedged wake ----

def _peak_mb() -> float:
    import resource
    import sys
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


@pytest.mark.parametrize("threshold", [50000000, 999999999, 5000000000])
def test_length_conditions_too_large_to_build_are_skipped_fast(tmp_path, make_cfg, threshold):
    old = f"def f(xs, n):\n    if len(xs) > {threshold}:\n        return xs[0]\n    return n\n"
    src_plans = feedback.diagnose(old, 1, [[3, 3]])
    sym = {"params": [{"name": "xs", "annotation": None, "default": None}, {"name": "n", "annotation": None, "default": None}]}
    from runner import lg_codec
    base = {"args": [{"name": "xs", "kind": "pos", "value": lg_codec.encode_input([1])},
                     {"name": "n", "kind": "pos", "value": lg_codec.encode_input(0)}], "source": "generated", "weak": True}
    before, started, notes = _peak_mb(), time.monotonic(), []
    items = feedback.build(sym, [base], None, src_plans, notes=notes)
    assert time.monotonic() - started < 0.5 and _peak_mb() - before < 100
    assert items == [] and notes == [f"a condition needs a value of length {threshold + 1}; too large to build"]
    r, seconds = run_case(tmp_path, make_cfg, old, old.replace("xs[0]", "xs[-1]"), "f")
    assert r.status == diff.UNTESTED and "too large to build" in r.reason, r.reason
    assert seconds < 10 and _peak_mb() - before < 300
    print(f"len(xs) > {threshold}: check {seconds:.2f} s")


def test_built_values_are_capped():
    assert len(feedback._sized("a", 10**9)) == feedback.MAX_SIZE
    assert len(feedback._sized([1], 10**9)) == feedback.MAX_SIZE
    import re
    # A walk that would grow past MAX_REGEX_TEXT gives up instead of building it.
    started = time.monotonic()
    assert feedback._regex_examples(r"((a{50}){50}){50}", 0, "fullmatch") == []
    assert time.monotonic() - started < 0.5
    assert re  # noqa: B018


@pytest.mark.parametrize("expr", ["len(list(range(1000000))*1000000)", "len(sorted(list(range(1000000))*100))",
                                  "'a' * 10**9", "'%999999999s' % x", "xs * n", "x * n", "bytes(n)", "list(range(n))",
                                  "len(x * 1000) > 1"])
def test_evaluator_refuses_expressions_that_could_grow_large(expr):
    import ast
    ev = feedback._Evaluator({"x", "xs", "n"}, {})
    started = time.monotonic()
    assert ev.value(ast.parse(expr, mode="eval").body, {"x": "ab", "xs": [1, 2], "n": 10**9}) == (False, None)
    assert time.monotonic() - started < 0.1


def test_evaluator_still_reads_small_arithmetic_and_skips_large_values():
    import ast
    ev = feedback._Evaluator({"x", "xs"}, {})
    assert ev.value(ast.parse("x * 2 > 3", mode="eval").body, {"x": 3}) == (True, True)
    assert ev.value(ast.parse("x % 2 == 1", mode="eval").body, {"x": 3}) == (True, True)
    assert ev.value(ast.parse("len(sorted(xs)) > 1", mode="eval").body, {"xs": [2, 1]}) == (True, True)
    assert ev.value(ast.parse("len(xs) > 1", mode="eval").body, {"xs": list(range(feedback.MAX_SIZE + 1))}) == (False, None)


def test_catastrophic_regex_off_the_main_thread_finishes_fast():
    import ast
    import threading
    ev = feedback._Evaluator({"x"}, {})
    node = ast.parse("re.match('(a+)+$', x)", mode="eval").body
    got = {}

    def go():
        started = time.monotonic()
        got["v"] = ev.value(node, {"x": "a" * 60 + "b"})
        got["examples"] = feedback._regex_examples("(a+)+$", 0, "match")
        got["s"] = time.monotonic() - started
    worker = threading.Thread(target=go)
    worker.start()
    worker.join(10)
    assert got["v"] == (False, None) and got["s"] < 0.5


def test_second_pass_never_runs_past_the_check_limit(tmp_path, make_cfg, monkeypatch):
    module = "pkg_fb/mod.py"
    base = write_tree(tmp_path / "base", {"pkg_fb/__init__.py": "", module: SEQ_OLD})
    work = write_tree(tmp_path / "work", {"pkg_fb/__init__.py": "", module: SEQ_NEW})
    cfg = make_cfg(work, behavioral_diff={"timeout_s": 20})
    targets = diff.changed_targets(module, (base / module).read_text(), (work / module).read_text())
    calls, real = [], diff._run_worker
    started = time.monotonic()

    def spy(cfg_, py, root, paths, functions, timeout, warm_names=None):
        if any("timeouts_before" in f for f in functions):
            calls.append(time.monotonic() - started + timeout)
        return real(cfg_, py, root, paths, functions, timeout, warm_names)
    monkeypatch.setattr(diff, "_run_worker", spy)
    r = next(x for x in diff.run(cfg, str(base), str(work), targets, harvest={}) if x.qualname == "head")
    assert r.feedback_tried and calls and max(calls) <= 20 + 0.05


def test_second_pass_skipped_when_building_leaves_too_little_time(tmp_path, make_cfg, monkeypatch):
    module = "pkg_fb/mod.py"
    base = write_tree(tmp_path / "base", {"pkg_fb/__init__.py": "", module: SEQ_OLD})
    work = write_tree(tmp_path / "work", {"pkg_fb/__init__.py": "", module: SEQ_NEW})
    cfg = make_cfg(work, behavioral_diff={"timeout_s": 6})
    targets = diff.changed_targets(module, (base / module).read_text(), (work / module).read_text())
    real = feedback.build
    deadline = {}

    def slow(*a, **k):
        out = real(*a, **k)
        time.sleep(max(0.0, deadline["end"] - time.monotonic() - 1.0))  # leave about 1 s of the check's limit
        return out
    monkeypatch.setattr(feedback, "build", slow)
    deadline["end"] = time.monotonic() + 6
    r = next(x for x in diff.run(cfg, str(base), str(work), targets, harvest={}) if x.qualname == "head")
    assert r.status == diff.UNTESTED and not r.feedback_tried
    assert "no second pass:" in r.reason and "s of the check's limit were left" in r.reason, r.reason


def test_feedback_only_difference_is_hedged_in_wake_and_ledger(tmp_path, make_cfg):
    from guardlib import engine, ledger
    r, _ = run_case(tmp_path, make_cfg, SEQ_OLD, SEQ_NEW, "head")
    assert r.status == diff.DIFFERENT and r.feedback_only
    wake = diff.format_wake(r, "F1")
    assert "only on second-pass guesses" in wake and "confirm with a realistic input" in wake
    cfg = make_cfg(tmp_path / "work")
    data = ledger.fresh("s")
    assert engine.record(cfg, data, [r])          # still wakes (and fails CI) like other edit-derived guesses
    found = next(f for f in data["findings"] if f["kind"] == "behavior_difference")
    assert "only on second-pass guesses" in found["evidence"] and found["feedback_only"] is True


def test_second_pass_input_that_timed_out_in_both_versions_does_not_count():
    # Line 3 is the changed statement. The only second-pass input ran it, then timed out in every run: compare_one has no
    # comparable outcome for it, so line 3 stays unreached and the result stays untested.
    t = diff.Target("m.py", "f", "function", [], "h", new_gate=[[3, 3]])
    item = {"args": [], "source": "generated"}
    fb = {"args": [], "source": "feedback", "weak": True}
    first = {"status": "ran", "results": [{"outcome": "return", "value": {"t": "v", "v": 0}}], "lines": [2], "input_lines": [[2]]}
    second = {"status": "ran", "results": [{"outcome": "timeout"}], "lines": [2, 3], "input_lines": [[2, 3]]}
    r2, ran_all, keep, _ = diff._second_compare(t, [item], [fb], (first, second), (first, second), (first, second), True)
    assert keep == [False]
    assert ran_all.statements_reached == 1             # it did run line 3 ...
    assert r2.statements_reached == 0 and r2.status == diff.UNTESTED   # ... but that does not count
    # The same input returning in every run is compared, and then line 3 counts.
    done = {**second, "results": [{"outcome": "return", "value": {"t": "v", "v": 1}}]}
    r3, _, keep, _ = diff._second_compare(t, [item], [fb], (first, done), (first, done), (first, done), True)
    assert keep == [True] and r3.statements_reached == 1 and r3.status == diff.DONE, r3.reason


def test_huge_string_multiply_is_refused_off_the_main_thread():
    import ast
    import threading
    ev = feedback._Evaluator({"s"}, {})
    got = {}

    def go():
        started = time.monotonic()
        got["v"] = ev.value(ast.parse("s == 'a' * 999999 * 999", mode="eval").body, {"s": "x"})
        got["s"] = time.monotonic() - started
    worker = threading.Thread(target=go)
    worker.start()
    worker.join(10)
    assert got["v"] == (False, None) and got["s"] < 0.1


def test_build_stops_at_its_deadline_inside_a_plan():
    src = """def f(a, b, c):
    if a == 1 and b == 2 and c == 3:
        return 1
"""
    sym = {"params": [{"name": p, "annotation": None, "default": None} for p in "abc"]}
    from runner import lg_codec
    base = {"args": [{"name": p, "kind": "pos", "value": lg_codec.encode_input(0)} for p in "abc"], "source": "generated"}
    plans = feedback.diagnose(src, 1, [[3, 3]])
    assert feedback.build(sym, [base], None, plans)
    assert feedback.build(sym, [base], None, plans, deadline=time.monotonic() - 1) == []
