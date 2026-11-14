"""Sprint 6 task 6.1: carved calls and callers' own example calls as bases for edit-derived inputs.

Mirrors youtube-dl 1 without the fix's tests (docs/validation/2026-11-run2.md, miss 1): the edited helper has no example
calls of its own, its caller's tests pass dicts with numbers only, and the bug shows only for a bool value. OLD is the
fixed code, NEW the planted bug, as in the harness."""
import json

from guardlib import engine
from runner import diff, inputs, lg_codec

from conftest import write_tree

HELPER_OLD = '''
def _match_one(filter_part, dct):
    key = filter_part.strip()
    if key.startswith('!'):
        return dct.get(key[1:]) is None
    v = dct.get(key)
    if isinstance(v, bool):
        return v is True
    return v is not None


def match_str(filter_str, dct):
    return all(_match_one(part, dct) for part in filter_str.split('&'))
'''
HELPER_NEW = HELPER_OLD.replace("    if isinstance(v, bool):\n        return v is True\n", "")
TESTS = '''
from pkg.utils import match_str


def test_match_str():
    assert match_str('x', {'x': 1200})
    assert not match_str('!x', {'x': 1200})
    assert match_str('x', {'x': 0})
    assert not match_str('x', {})
'''


def _setup(tmp_path, make_cfg):
    files = {"pkg/__init__.py": "", "tests/test_utils.py": TESTS}
    root = write_tree(tmp_path / "proj", {**files, "pkg/utils.py": HELPER_NEW})
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", {**files, "pkg/utils.py": HELPER_OLD})
    changed = diff.changed_targets("pkg/utils.py", (base / "pkg/utils.py").read_text(), (root / "pkg/utils.py").read_text())
    deps = engine.dependents(cfg, "pkg/utils.py", changed, {t.qualname for t in changed})
    return cfg, base, root, changed, deps


def _values(d):
    return [lg_codec.decode(a["value"]) for a in d["args"]]


def test_helper_reached_only_through_its_caller_is_detected_by_edit_input_on_a_carved_base(tmp_path, make_cfg, monkeypatch):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg)
    assert [t.qualname for t in changed] == ["_match_one"]
    assert [d.qualname for d in deps] == ["match_str"]
    # The caller's own edit inputs would hand the helper {'x': False} as a carved call; without them, only an edit
    # input built on a carved call can find it.
    monkeypatch.setattr(diff, "_callee_edit", lambda *a: None)
    seen = []
    real = diff._run_worker
    monkeypatch.setattr(diff, "_run_worker", lambda *a, **k: seen.append(a[4]) or real(*a, **k))
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    main = [fs for fs in seen if not any(f.get("capture") for f in fs)]
    lists = [json.dumps(next(f for f in fs if f["qualname"] == "_match_one")["inputs"], sort_keys=True) for fs in main]
    assert len(lists) == 3 and lists[0] == lists[1] == lists[2]  # added before the three main runs
    assert any(i.get("base") == "carved" and i["source"] == "edit" and i["weak"] for i in json.loads(lists[0]))
    helper = results["_match_one"]
    assert helper.status == diff.DIFFERENT, helper.reason
    assert all(d["source"] != "carved" for d in helper.differences)
    edit = [d for d in helper.differences if d["source"] == "edit"]
    assert edit and all(d.get("base") == "carved" for d in edit), helper.differences
    assert any(_values(d)[1] == {"x": False} for d in edit), [d["call"] for d in edit]
    # The helper had no example calls of its own: the dict came from a carved call ({'x': 1200} and the like).
    assert helper.carved_captured > 0
    assert results["match_str"].status != diff.DIFFERENT  # as in validation run 2, without edit inputs of its own


def test_caller_gets_edit_inputs_from_its_callee_and_inputs_match_across_runs(tmp_path, make_cfg, monkeypatch):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg)
    seen = []
    real = diff._run_worker

    def spy(cfg_, py, root_, sys_path, functions, timeout, warm_names=None):
        seen.append(functions)
        return real(cfg_, py, root_, sys_path, functions, timeout, warm_names)
    monkeypatch.setattr(diff, "_run_worker", spy)
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    assert results["_match_one"].status == diff.DIFFERENT, results["_match_one"].reason
    caller = results["match_str"]
    assert caller.status == diff.DIFFERENT, caller.reason
    assert "match_str(filter_str='x', dct={'x': False})" in [d["call"] for d in caller.differences if d["source"] == "edit"]
    # Inputs are built once: the three main runs see the same list, carved-based edit inputs included, within max_inputs.
    main = [fs for fs in seen if not any(f.get("capture") for f in fs)]
    assert len(main) == 3
    for name in ("_match_one", "match_str"):
        lists = [json.dumps(next(f for f in fs if f["qualname"] == name)["inputs"], sort_keys=True) for fs in main]
        assert lists[0] == lists[1] == lists[2]
        assert len(json.loads(lists[0])) <= int(cfg.behavioral_diff["max_inputs"])
    # The helper's carved calls now include what the caller's edit inputs passed it.
    helper_inputs = next(f for f in main[0] if f["qualname"] == "_match_one")["inputs"]
    assert any(i["source"] == "carved" and _values(i)[1] == {"x": False} for i in helper_inputs)


def test_without_its_caller_the_helper_gets_no_carved_bases(tmp_path, make_cfg):
    cfg, base, root, changed, deps = _setup(tmp_path, make_cfg)
    r = diff.run(cfg, str(base), str(root), changed, harvest={})[0]
    assert not any(d["source"] == "edit" and _values(d)[1] == {"x": False} for d in r.differences)


def test_carved_edit_items_respect_the_input_limit():
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "k"}, {"name": "d"}]}
    carved = [{"args": [{"name": "k", "kind": "pos", "value": lg_codec.encode_input("x")},
                        {"name": "d", "kind": "pos", "value": lg_codec.encode_input({"x": n, "y": n})}],
               "has_self": False, "source": "carved", "carved_id": n} for n in range(10)]
    edit = {"literals": [1, 2, "a"], "fragments": [], "numbers": [[1, 2]], "regexes": []}
    head = [{"args": [], "has_self": False, "source": "examples"}] * 5
    tail = [{"args": [], "has_self": False, "source": "generated"}] * 30
    items, added = inputs.with_carved_edit_items(sym, head + carved + tail, edit, "/nonexistent", 40)
    assert len(items) == 40 and added > 0
    assert items[:15] == head + carved  # carved calls and what came before them are never cut
    new = [i for i in items if i["source"] == "edit"]
    assert len(new) == added and all(i["weak"] and i["base"] == "carved" for i in new)
    # Every edit input is a carved call with one value replaced.
    assert all(json.dumps(i["args"], sort_keys=True) not in {json.dumps(c["args"], sort_keys=True) for c in carved} for i in new)
    same, none = inputs.with_carved_edit_items(sym, head + carved, None, "/nonexistent", 40)
    assert none == 0 and same == head + carved


def test_callee_edit_info_merged_for_a_caller():
    a = {"literals": [True, "!"], "fragments": ["ab"], "numbers": [[1, 2]], "regexes": [{"fn": "search", "old": "a", "new": "b"}]}
    b = {"literals": ["!", 3], "fragments": [], "numbers": [[3, 4]], "regexes": []}
    got = diff._merged_edit([a, b])
    assert got["literals"] == [True, "!", 3]
    assert got["numbers"] == [[1, 2], [3, 4]] and got["fragments"] == ["ab"] and len(got["regexes"]) == 1
    assert diff._merged_edit([]) is None
