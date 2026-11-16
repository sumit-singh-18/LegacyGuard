"""Task 6.8c: building `self` as a last resort, for classes whose constructor needs untyped arguments and has no
literal constructor call in the repo. Results on such an instance are labelled weaker."""
from guardlib import ci, engine, ledger
from runner import diff

from conftest import call, write_tree

SRC = '''
def make_conn(token):
    return [token]


class Store:
    def __init__(self, path, config):
        self.path = path
        self.config = config
        self.hits = 0

    def key(self, name):
        return self.path + "/" + name


class Bag:
    def __init__(self, a, b=None):
        if not isinstance(a, list):
            raise TypeError("a must be a list")
        self.items = a
        self.extra = b

    def size(self, k):
        return len(self.items) + k


class Runner:
    def __init__(self, func):
        if not callable(func):
            raise TypeError("func must be callable")
        self.func = func

    def scaled(self, x):
        return x * 2


class Locked:
    limit = 3

    def __init__(self, token, other):
        if token != "secret-%d" % 42:
            raise ValueError("bad token")
        self.conn = make_conn(token)
        self.mode = "fast"

    def ping(self, n):
        return self.conn.append(n)

    def double(self, n):
        return n * 2


CALLS = []


class Slow:
    def __init__(self, a, b):
        CALLS.append(1)
        while True:
            pass

    def count(self, n):
        return len(CALLS) + n

    def other(self, n):
        return len(CALLS) - n


class Picky:
    def __init__(self, a, b):
        CALLS.append(1)
        raise ValueError("never")

    def count(self, n):
        return len(CALLS) + n

    def other(self, n):
        return len(CALLS) - n


class Sealed:
    def __new__(cls, thing):
        return super().__new__(cls)

    def __init__(self, thing):
        raise ValueError("needs a real connection")

    def get(self, n):
        return n
'''


def run(tmp_path, make_cfg, old, new):
    files = {"pkg/__init__.py": ""}
    root = write_tree(tmp_path / "proj", {**files, "pkg/things.py": new})
    base = write_tree(tmp_path / "base", {**files, "pkg/things.py": old})
    cfg = make_cfg(root)
    targets = diff.changed_targets("pkg/things.py", (base / "pkg/things.py").read_text(), (root / "pkg/things.py").read_text())
    return cfg, {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_untyped_path_and_config_get_placeholders(run_worker, tmp_path):
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    out = run_worker(root, [{"module": "m", "qualname": "Store.key", "inputs": [call("a")]}])["m:Store.key"]
    assert out["self_recipe"] == "placeholder constructor arguments Store(path='x', config={})"
    assert out["self_weak"] is True
    assert out["results"][0]["value"] == {"t": "v", "v": "x/a"}


def test_list_and_callable_parameters_are_found(run_worker, tmp_path):
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    out = run_worker(root, [{"module": "m", "qualname": "Bag.size", "inputs": [call(2)]},
                            {"module": "m", "qualname": "Runner.scaled", "inputs": [call(2)]}])
    assert out["m:Bag.size"]["self_recipe"] == "placeholder constructor arguments Bag(a=[])"
    assert out["m:Bag.size"]["results"][0]["value"] == {"t": "v", "v": 2}
    assert out["m:Runner.scaled"]["self_recipe"] == "placeholder constructor arguments Runner(func=<function>)"
    assert out["m:Runner.scaled"]["results"][0]["value"] == {"t": "v", "v": 4}


def test_bypass_sets_init_attributes_and_skips_new_with_arguments(run_worker, tmp_path):
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    out = run_worker(root, [{"module": "m", "qualname": "Locked.ping", "inputs": [call(1)]},
                            {"module": "m", "qualname": "Sealed.get", "inputs": [call(1)]}])
    locked = out["m:Locked.ping"]
    assert locked["self_weak"] is True
    assert locked["self_recipe"] == "an instance made without calling its constructor (attributes from __init__: mode)"
    assert locked["results"][0]["outcome"] == "raise" and locked["results"][0]["type"] == "AttributeError"
    sealed = out["m:Sealed.get"]["results"][0]
    assert sealed["outcome"] == "setup_failed"
    assert "could not build a Sealed" in sealed["detail"] and "its __new__ needs arguments" in sealed["detail"]


def test_placeholder_choice_is_deterministic(run_worker, tmp_path):
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    spec = [{"module": "m", "qualname": "Store.key", "inputs": [call("a"), call("b")]}]
    first = run_worker(root, spec)["m:Store.key"]
    second = run_worker(root, spec)["m:Store.key"]
    assert first["self_recipe"] == second["self_recipe"] and first["results"] == second["results"]


def test_difference_on_placeholder_self_is_detected_and_hedged(tmp_path, make_cfg):
    new = SRC.replace('return self.path + "/" + name', 'return self.path + ":" + name')
    cfg, results = run(tmp_path, make_cfg, SRC, new)
    r = results["Store.key"]
    assert r.status == diff.DIFFERENT, r.reason
    assert r.dropped_nondeterministic == 0
    assert r.self_placeholder.startswith("placeholder constructor arguments Store(")
    assert "may come from the placeholder state rather than from the edit" in r.placeholder_note
    wake = diff.format_wake(r, "F1")
    assert "placeholder instance" in wake and "may come from the placeholder state" in wake
    data = ledger.fresh("s")
    wakes = engine.record(cfg, data, [r])
    assert wakes, "a difference on a placeholder instance still wakes, hedged like other guessed inputs"
    found = next(f for f in data["findings"] if f["kind"] == "behavior_difference")
    assert "placeholder state" in found["evidence"] and found["placeholder_self"].startswith("placeholder constructor")
    out = ci.Outcome(base="abc", results=[r])
    report = ci.report(cfg, out)
    assert "placeholder state" in report and "Differs only on a placeholder instance" in report
    notes = ci.annotations(cfg, out)
    assert any(line.startswith("::warning") and "placeholder state" in line for line in notes)
    assert not any(line.startswith("::error") for line in notes)
    assert out.failing(cfg) == [] and ci.exit_code(cfg, out, "difference") == 0

def test_behaviour_preserving_edit_is_verified_with_weak_label(tmp_path, make_cfg):
    new = SRC.replace("return len(self.items) + k", "return k + len(self.items)")
    cfg, results = run(tmp_path, make_cfg, SRC, new)
    r = results["Bag.size"]
    assert r.status == diff.DONE, r.reason
    assert r.placeholder_inputs == r.inputs_compared > 0
    assert "placeholder instance (placeholder constructor arguments Bag(a=[]))" in diff.placeholder_text(r)
    assert "weaker evidence" in diff.format_note([r])
    data = ledger.fresh("s")
    engine.record(cfg, data, [r])
    found = next(f for f in data["findings"] if f["kind"] == "verified")
    assert "placeholder instance" in found["evidence"]


def test_callable_parameter_edit_detected(tmp_path, make_cfg):
    new = SRC.replace("return x * 2", "return x * 3")
    _, results = run(tmp_path, make_cfg, SRC, new)
    r = results["Runner.scaled"]
    assert r.status == diff.DIFFERENT and r.placeholder_note, r.reason


def test_unbuildable_attribute_stays_untested_with_reason(tmp_path, make_cfg):
    # Every placeholder is rejected by __init__, the instance made without it lacks `conn`: both versions raise on a
    # guessed instance, so nothing is verified, and the reason names the placeholder.
    new = SRC.replace("return self.conn.append(n)", "return self.conn.append(n + 1)")
    _, results = run(tmp_path, make_cfg, SRC, new)
    r = results["Locked.ping"]
    assert r.status == diff.UNTESTED
    assert "no input ran to completion" in r.reason and "the instance was a placeholder" in r.reason


def test_new_needing_arguments_is_skipped_with_reason(tmp_path, make_cfg):
    new = SRC.replace("        return n\n", "        return n + 0\n")
    _, results = run(tmp_path, make_cfg, SRC, new)
    r = results["Sealed.get"]
    assert r.status == diff.UNTESTED and "its __new__ needs arguments" in r.reason


def test_changed_constructor_makes_placeholder_inputs_incomparable(tmp_path, make_cfg):
    # The edit changes what __init__ accepts: old and new code pick different placeholders, so the inputs on them
    # are not compared (a difference would come from the instance, not the method).
    new = SRC.replace("        self.path = path\n", "        if path == 'x':\n            raise ValueError(path)\n        self.path = path\n")
    new = new.replace('return self.path + "/" + name', 'return self.path + ":" + name')
    _, results = run(tmp_path, make_cfg, SRC, new)
    r = results["Store.key"]
    assert r.status == diff.UNTESTED and "built differently by the old code" in r.reason


def test_ci_fails_when_a_real_difference_is_mixed_in(tmp_path, make_cfg):
    root = write_tree(tmp_path / "p", {"m.py": "x = 1\n"})
    cfg = make_cfg(root)
    ph = diff.Result("m.py", "C.m", diff.DIFFERENT, "h1", inputs_compared=1, self_placeholder="p", placeholder_inputs=1,
                     placeholder_note="Every differing input ran on a placeholder instance (p)",
                     differences=[{"call": "m(1)", "before": "1", "after": "2", "source": "generated", "placeholder_self": True}])
    real = diff.Result("m.py", "f", diff.DIFFERENT, "h2", inputs_compared=1,
                       differences=[{"call": "f(1)", "before": "1", "after": "2", "source": "tests"}])
    assert ci.exit_code(cfg, ci.Outcome(base="abc", results=[ph]), "difference") == 0
    out = ci.Outcome(base="abc", results=[ph, real])
    assert out.failing(cfg) == [real] and ci.exit_code(cfg, out, "difference") == 1
    data = ledger.fresh("s")
    assert len(engine.record(cfg, data, [ph])) == 1  # the hedged wake still happens


def test_bypass_policy_when_init_rejects_every_placeholder(tmp_path, make_cfg):
    # Locked.__init__ rejects every placeholder. Policy: the instance is made without __init__. (a) A method that does not
    # depend on what __init__ computes is checked on it, labelled as on a placeholder; (b) one that reads an attribute
    # only __init__ would compute stays untested, with the reason naming the instance made without the constructor.
    new = SRC.replace("return n * 2", "return n * 3").replace("return self.conn.append(n)", "return self.conn.append(n + 1)")
    _, results = run(tmp_path, make_cfg, SRC, new)
    a = results["Locked.double"]
    assert a.status == diff.DIFFERENT and a.self_placeholder.startswith("an instance made without calling its constructor")
    assert a.placeholder_note and diff.placeholder_only(a)
    b = results["Locked.ping"]
    assert b.status == diff.UNTESTED and "an instance made without calling its constructor" in b.reason


def test_search_runs_once_per_class_and_is_bounded_by_counts(run_worker, tmp_path):
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    out = run_worker(root, [{"module": "m", "qualname": "Picky.count", "inputs": [call(0)]},
                            {"module": "m", "qualname": "Picky.other", "inputs": [call(0)]}])
    # Every placeholder attempt is rejected, then the bypass is used. The second method reuses the cached outcome, so
    # __init__ ran PLACEHOLDER_ATTEMPTS times in all, not twice that.
    first, second = out["m:Picky.count"], out["m:Picky.other"]
    assert first["self_recipe"] == second["self_recipe"] and first["self_recipe"].startswith("an instance made without")
    assert first["results"][0]["value"] == {"t": "v", "v": 24} and second["results"][0]["value"] == {"t": "v", "v": 24}


def test_hanging_constructor_stops_after_two_timeouts(run_worker, tmp_path):
    import time
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    began = time.monotonic()
    out = run_worker(root, [{"module": "m", "qualname": "Slow.count", "inputs": [call(0)]},
                            {"module": "m", "qualname": "Slow.other", "inputs": [call(0)]}])
    assert time.monotonic() - began < 15
    assert out["m:Slow.count"]["results"][0]["value"] == {"t": "v", "v": 2}  # two attempts, then the search stopped
    assert out["m:Slow.other"]["results"][0]["value"] == {"t": "v", "v": 2}  # cached: not searched again


def test_old_and_new_choose_the_same_recipe_for_several_methods(tmp_path, make_cfg):
    new = SRC.replace("return len(CALLS) + n", "return len(CALLS) + n + 1").replace("return len(CALLS) - n", "return len(CALLS) - n - 1")
    _, results = run(tmp_path, make_cfg, SRC, new)
    for name in ("Picky.count", "Picky.other"):
        r = results[name]
        assert r.status == diff.DIFFERENT and r.dropped_nondeterministic == 0, (name, r.reason)
        assert r.self_placeholder.startswith("an instance made without calling its constructor")
