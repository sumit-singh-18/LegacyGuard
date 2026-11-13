"""Warm worker servers (roadmap A3): same results as the cold worker, reused across checks, fresh code for edits."""
import os

import pytest

from runner import diff, warm

from conftest import write_tree

pytestmark = pytest.mark.skipif(not hasattr(os, "fork"), reason="warm workers need fork()")

SRC = '''
import json

def total(xs: list[int]) -> int:
    return sum(xs)
'''


def setup(tmp_path, make_cfg, new_src):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new_src})
    c = make_cfg(root, behavioral_diff={"warm": True})
    base = write_tree(c.state_dir / "baseline", {"pkg/__init__.py": "", "pkg/m.py": SRC})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return c, str(base), str(root), targets


def servers(c, roots):
    from guardlib import config as cfgmod
    py = cfgmod.target_python(c)
    return [warm.socket_path(c, py, r, m) for r, m in roots]


def stop(c, base, root):
    from guardlib import config as cfgmod
    warm.stop_all(c, cfgmod.target_python(c), [(base, "baseline"), (root, "work")])


def test_warm_matches_cold_and_is_reused(tmp_path, make_cfg):
    broken = SRC.replace("return sum(xs)", "return sum(xs[1:])")
    c, base, root, targets = setup(tmp_path, make_cfg, broken)
    try:
        first = diff.run(c, base, root, targets, harvest={})[0]
        assert first.status == diff.DIFFERENT
        paths = servers(c, [(base, "baseline"), (root, "work")])
        assert all(os.path.exists(p) for p in paths), "both servers should be running"
        pings = [warm._send(p, {"cmd": "ping"}, 2) for p in paths]
        assert pings[0]["runs"] >= 2 and "pkg.m" in pings[0]["warm"]  # baseline keeps the project's module warm
        assert "pkg.m" not in pings[1]["warm"]  # the working tree never does
        # Fix the edit: the working-tree server must see the new code, not a stale import.
        (tmp_path / "proj/pkg/m.py").write_text(SRC.replace("return sum(xs)", "return 0 + sum(xs)"))
        targets2 = diff.changed_targets("pkg/m.py", SRC.lstrip("\n"), (tmp_path / "proj/pkg/m.py").read_text())
        assert diff.run(c, base, root, targets2, harvest={})[0].status == diff.DONE
        cold = make_cfg(tmp_path / "proj", behavioral_diff={"warm": False})
        assert diff.run(cold, base, root, targets2, harvest={})[0].status == diff.DONE
    finally:
        stop(c, base, root)


def test_third_party_imports(tmp_path):
    root = write_tree(tmp_path, {"app/__init__.py": "", "app/a.py": "import os, json\nimport requests\nfrom app import b\nfrom yaml import safe_load\nfrom . import c\n", "app/b.py": ""})
    assert warm.third_party_imports(str(root), ["app/a.py"], []) == ["requests", "yaml"]
