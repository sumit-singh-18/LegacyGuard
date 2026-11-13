"""The project's own tests that reach the changed code, run on the baseline and on the edit (roadmap C4)."""
import shutil

from guardlib import engine, ledger
from indexer import db
from runner import affected_tests as at
from runner import diff

from conftest import write_tree

BANK = '''
def withdraw(balance, amount):
    if amount > balance:
        raise ValueError("insufficient funds")
    return balance - amount


def fee(amount):
    return 1
'''

TESTS = '''
import pytest
from bank.core import withdraw, fee


def test_withdraw():
    assert withdraw(100, 30) == 70


@pytest.mark.parametrize("amount", [100, 101])
def test_limits(amount):
    if amount > 100:
        with pytest.raises(ValueError):
            withdraw(100, amount)
    else:
        assert withdraw(100, amount) == 0


class TestFee:
    def test_flat(self):
        assert fee(5) == 1


def test_already_broken():
    assert withdraw(1, 1) == 99
'''


def project(tmp_path, make_cfg, new_bank):
    files = {"bank/__init__.py": "", "tests/__init__.py": "", "tests/test_core.py": TESTS}
    root = write_tree(tmp_path / "proj", {**files, "bank/core.py": new_bank})
    cfg = make_cfg(root)
    base = cfg.state_dir / "baseline"
    write_tree(base, {**files, "bank/core.py": BANK})
    conn = db.connect(str(root))
    conn.executemany("INSERT INTO test_cover(test_id, file, qualname) VALUES (?,?,?)", [
        ("tests.test_core.test_withdraw", "bank/core.py", "withdraw"),
        ("tests.test_core.test_limits", "bank/core.py", "withdraw"),
        ("tests.test_core.test_already_broken", "bank/core.py", "withdraw"),
        ("tests.test_core.TestFee.test_flat", "bank/core.py", "fee"),
    ])
    conn.commit()
    conn.close()
    return root, cfg


def test_to_nodeid(tmp_path):
    write_tree(tmp_path, {"tests/test_a.py": "", "pkg/tests/test_b.py": ""})
    assert at.to_nodeid(str(tmp_path), "tests.test_a.test_x") == "tests/test_a.py::test_x"
    assert at.to_nodeid(str(tmp_path), "pkg.tests.test_b.TestK.test_y") == "pkg/tests/test_b.py::TestK::test_y"
    assert at.to_nodeid(str(tmp_path), "nowhere.test_z") is None


def targets_for(root, name):
    return [diff.Target("bank/core.py", name, "function", [], "h")]


def test_selection_and_regression(tmp_path, make_cfg):
    broken = BANK.replace("if amount > balance:", "if amount >= balance:")
    root, cfg = project(tmp_path, make_cfg, broken)
    wakes = engine.affected_tests(cfg, "bank/core.py", targets_for(root, "withdraw"), "hash1")
    data = ledger.load(cfg.state_dir)
    regs = {f["item"]: f for f in data["findings"] if f["kind"] == "test_regression"}
    # amount == balance now raises: the parametrized limit test fails; the test that already failed is left out.
    assert set(regs) == {"tests/test_core.py::test_limits"}
    assert len(wakes) == 1 and "passed on the session baseline and fails after this edit" in wakes[0]
    assert data["old_test_results"]["tests/test_core.py::test_already_broken"]["outcome"] == "failed"


def test_fix_resolves_and_baseline_is_cached(tmp_path, make_cfg):
    broken = BANK.replace("if amount > balance:", "if amount >= balance:")
    root, cfg = project(tmp_path, make_cfg, broken)
    engine.affected_tests(cfg, "bank/core.py", targets_for(root, "withdraw"), "hash1")
    (root / "bank/core.py").write_text((cfg.state_dir / "baseline/bank/core.py").read_text())
    shutil.rmtree(cfg.state_dir / "baseline" / "tests")  # the baseline must not be run again: its results are cached
    wakes = engine.affected_tests(cfg, "bank/core.py", targets_for(root, "withdraw"), "hash2")
    states = {f["state"] for f in ledger.load(cfg.state_dir)["findings"] if f["kind"] == "test_regression"}
    assert wakes == [] and states == {"resolved"}


def test_class_based_test_selected(tmp_path, make_cfg):
    root, cfg = project(tmp_path, make_cfg, BANK.replace("return 1", "return 2"))
    wakes = engine.affected_tests(cfg, "bank/core.py", targets_for(root, "fee"), "h")
    assert len(wakes) == 1 and "TestFee::test_flat" in wakes[0]


def test_disabled_or_no_index(tmp_path, make_cfg):
    root, cfg = project(tmp_path, make_cfg, BANK)
    cfg.raw["affected_tests"] = {"enabled": False}
    assert engine.affected_tests(cfg, "bank/core.py", targets_for(root, "withdraw"), "h") == []
    other = write_tree(tmp_path / "noindex", {"a.py": ""})
    assert engine.affected_tests(make_cfg(other), "a.py", targets_for(other, "x"), "h") == []
