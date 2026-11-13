"""Planted bugs on the changed lines measure how strong a pass is (roadmap B6)."""
from runner import diff, mutants

from conftest import call, write_tree

FN = '''def tier(points: int) -> str:
    if points >= 500:
        return "gold"
    return "basic"
'''


def test_generate_only_on_changed_lines():
    plan = mutants.generate(FN, 10, [[11, 11]])  # line 2 of the function (the `if`) is line 11 of the file
    assert plan["name"] == "tier"
    changes = {m["change"] for m in plan["mutants"]}
    assert "GtE -> Gt" in changes and "500 -> 501" in changes
    assert not any("return" in c for c in changes)  # the return lines did not change


def test_generate_skips_decorated_and_unchanged():
    assert mutants.generate("@cache\n" + FN, 1, [[3, 3]]) is None
    assert mutants.generate(FN, 1, []) is None
    assert mutants.generate(FN, 1, [[50, 60]]) is None


SRC = '''
def tier(points: int) -> str:
    if points >= 500:
        return "gold"
    return "basic"


def make_adder(n):
    def add(x: int) -> int:
        return x + n
    return add


def countdown(n: int) -> int:
    n = min(n, 50)
    steps = 0
    while n > 0:
        n -= 1
        steps += 1
    return steps
'''


def run(tmp_path, make_cfg, new_src, **bd):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new_src})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/m.py": SRC})
    cfg = make_cfg(root, behavioral_diff=bd) if bd else make_cfg(root)
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_pass_reports_confidence(tmp_path, make_cfg):
    new = SRC.replace("if points >= 500:", "if not points < 500:")
    r = run(tmp_path, make_cfg, new)["tier"]
    assert r.status == diff.DONE
    assert r.mutants_total >= 2 and r.mutants_killed == r.mutants_total  # 500 is in the repo's own values, so the edge is tried
    assert "planted bugs in the changed lines were caught" in diff.format_note([r])


def test_looping_mutant_counts_as_caught(tmp_path, make_cfg):
    new = SRC.replace("        n -= 1\n", "        n = n - 1\n")
    r = run(tmp_path, make_cfg, new)["countdown"]
    assert r.status == diff.DONE and r.mutants_total >= 1 and r.mutants_killed == r.mutants_total


def test_mutants_can_be_switched_off(tmp_path, make_cfg):
    new = SRC.replace("if points >= 500:", "if not points < 500:")
    r = run(tmp_path, make_cfg, new, mutants=0)["tier"]
    assert r.status == diff.DONE and r.mutants_total == 0 and diff.confidence_text(r) == ""


def test_worker_refuses_when_recompiling_changes_behavior(tmp_path, run_worker):
    # A method using zero-argument super() cannot be recompiled on its own: the identity check must notice.
    src = "class A:\n    def f(self, x: int):\n        return x\n\nclass B(A):\n    def f(self, x: int):\n        return super().f(x) + 1\n"
    root = write_tree(tmp_path / "w", {"m.py": src})
    plan = mutants.generate("def f(self, x: int):\n    return super().f(x) + 1\n", 6, [[7, 7]])
    out = run_worker(root, [{"module": "m", "qualname": "B.f", "inputs": [call(1), call(2)], "mutants": plan}])["m:B.f"]
    assert out["mutation"]["applicable"] is False
