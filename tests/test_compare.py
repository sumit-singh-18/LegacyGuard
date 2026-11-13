"""compare_one: the verdict from three worker results (old twice, new once)."""
from runner import diff
from runner import lg_codec

from conftest import call


def target(**kw):
    return diff.Target("m.py", "f", "function", [{"name": "a0"}], "h", **kw)


def ran(*outcomes, lines=None):
    return {"status": "ran", "results": list(outcomes), "lines": lines}


def ret(v):
    return {"outcome": "return", "value": lg_codec.canon(v)}


def test_difference_found():
    items = [call(1), call(2)]
    r = diff.compare_one(target(), items, ran(ret(1), ret(2)), ran(ret(1), ret(2)), ran(ret(1), ret(3)), True)
    assert r.status == diff.DIFFERENT
    assert r.differences[0]["before"] == "2" and r.differences[0]["after"] == "3"


def test_verified_needs_gate_reached():
    items = [call(1)]
    t = target(new_gate=[[5, 5]])
    hit = diff.compare_one(t, items, ran(ret(1)), ran(ret(1)), ran(ret(1), lines=[5]), True)
    assert hit.status == diff.DONE
    miss = diff.compare_one(t, items, ran(ret(1)), ran(ret(1)), ran(ret(1), lines=[4]), True)
    assert miss.status == diff.UNTESTED and "changed lines" in miss.reason


def test_nondeterministic_inputs_dropped():
    items = [call(1)]
    r = diff.compare_one(target(), items, ran(ret(1)), ran(ret(2)), ran(ret(3)), True)
    assert r.status == diff.UNTESTED and r.dropped_nondeterministic == 1


def test_weak_both_raise_dropped():
    item = {**call("x"), "weak": True}
    err = {"outcome": "raise", "type": "TypeError", "message": "a"}
    err2 = {"outcome": "raise", "type": "ValueError", "message": "b"}
    r = diff.compare_one(target(), [item], ran(err), ran(err), ran(err2), True)
    assert r.dropped_invalid == 1 and r.status != diff.DIFFERENT


def test_import_error_after_edit_is_broken():
    r = diff.compare_one(target(), [call(1)], ran(ret(1)), ran(ret(1)), {"status": "import_error", "reason": "SyntaxError"}, True)
    assert r.status == diff.BROKEN


def test_blocked_reason_reported():
    blocked = {"outcome": "blocked", "detail": "x", "blocked_kinds": ["network"]}
    r = diff.compare_one(target(), [call(1)], ran(blocked), ran(blocked), ran(blocked), True)
    assert r.status == diff.UNTESTED and "network" in r.reason
