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


# ---- Task 7.4: pytest runs in a throwaway working folder, imports from the real tree ----

def test_no_ini_file_keys_stay_relative_and_equal_across_trees(tmp_path, make_cfg):
    """No pytest.ini, pyproject or setup.cfg, and tests/ is not a package: without --rootdir the rootdir would be
    tests/ (the common ancestor of the args), junit classnames would lose the "tests." prefix and results would drop."""
    broken = BANK.replace("if amount > balance:", "if amount >= balance:")
    root, cfg = project(tmp_path, make_cfg, broken)
    for tree in (root, cfg.state_dir / "baseline"):
        (tree / "tests/__init__.py").unlink()
    nodeids = ["tests/test_core.py::test_withdraw", "tests/test_core.py::test_limits"]
    regs, old, new = at.compare(cfg, str(cfg.state_dir / "baseline"), str(root), nodeids, {})
    assert set(old) == set(new) == set(nodeids)
    assert [r["nodeid"] for r in regs] == ["tests/test_core.py::test_limits"]


def test_relative_read_from_the_tree_root_passes_on_both_trees(tmp_path, make_cfg):
    reader = "import json\n\ndef test_reads():\n    assert json.load(open('tests/data/x.json')) == {'a': 1}\n"
    root, cfg = project(tmp_path, make_cfg, BANK)
    for tree in (root, cfg.state_dir / "baseline"):
        write_tree(tree, {"tests/test_read.py": reader, "tests/data/x.json": '{"a": 1}'})
    _, old, new = at.compare(cfg, str(cfg.state_dir / "baseline"), str(root), ["tests/test_read.py::test_reads"], {})
    assert old["tests/test_read.py::test_reads"]["outcome"] == new["tests/test_read.py::test_reads"]["outcome"] == "passed"


def test_ignored_in_place_module_imports_from_the_real_tree(tmp_path, make_cfg, monkeypatch):
    """pkg/_gen.py stands for a git-ignored in-place build or generated module. It is left out of the working
    folder, so the folder and the real tree differ. The test must still pass: imports come from the real tree. With
    `-m pytest` instead of the launcher, '' (the working folder) would come first on sys.path, bank would import from
    the folder, and the import of bank._gen would fail."""
    from runner import scratch
    root, cfg = project(tmp_path, make_cfg, BANK)
    (root / "tests/__init__.py").unlink()  # else pytest's rootdir insertion puts the real tree first and hides the issue
    write_tree(root, {"bank/_gen.py": "VALUE = 3\n", "tests/test_gen.py": "from bank._gen import VALUE\n\ndef test_gen():\n    assert VALUE == 3\n"})
    real = scratch._list_tree
    monkeypatch.setattr(scratch, "_list_tree", lambda tree, limit: (lambda e: ([x for x in e[0] if x[0] != "bank/_gen.py"], *e[1:]))(real(tree, limit)))
    out = at.run(cfg, str(root), ["tests/test_gen.py::test_gen"], 60)
    assert out["tests/test_gen.py::test_gen"]["outcome"] == "passed", out


def test_cwd_mode_off_still_imports_the_project(tmp_path, make_cfg):
    """With cwd_mode off the cwd is the tree root, which PYTHONPATH carries: the launcher must not drop it."""
    root, cfg = project(tmp_path, make_cfg, BANK)
    (root / "tests/__init__.py").unlink()  # else pytest's rootdir insertion would put the tree on sys.path anyway
    cfg.raw["behavioral_diff"]["cwd_mode"] = "off"
    out = at.run(cfg, str(root), ["tests/test_core.py::test_withdraw"], 60)
    assert out["tests/test_core.py::test_withdraw"]["outcome"] == "passed", out


def test_launcher_drops_only_the_current_folder_markers(tmp_path):
    import os
    import subprocess
    import sys
    code = at.LAUNCH.replace("import pytest; sys.exit(pytest.main(sys.argv[1:]))", "print(repr(sys.path))")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, check=True).stdout
    path = eval(out)  # noqa: S307 - our own repr of a list of strings
    assert "" not in path and "." not in path and str(tmp_path) in path
