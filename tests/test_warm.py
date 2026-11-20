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


PID_SRC = '''
import os

AT_IMPORT = os.getpid()


def who(x: int):
    n = x
    return AT_IMPORT, os.getpid(), f"worker-{os.getpid()}", n
'''


def test_process_ids_agree_across_warm_children(tmp_path, make_cfg):
    # The baseline server imports the module before forking, so its children read the server's pid at import and their
    # own pid in the call; the working-tree children read their own pid for both. All of these are "<pid>" when
    # compared, so a refactor next to them is verified, not reported as non-deterministic or different.
    new = PID_SRC.replace("    n = x\n", "    n = x + 0\n")
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new})
    c = make_cfg(root, behavioral_diff={"warm": True, "mutants": 0})
    base = write_tree(c.state_dir / "baseline", {"pkg/__init__.py": "", "pkg/m.py": PID_SRC})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    try:
        for _ in range(2):  # the second round runs on servers that already hold the module
            got = diff.run(c, str(base), str(root), targets, harvest={})[0]
            assert got.status == diff.DONE, (got.status, got.reason, got.differences[:1])
    finally:
        stop(c, str(base), str(root))


PPID_SRC = '''
import os


def mine(x: int):
    n = x
    return os.getpid(), n
'''


def test_known_limit_warm_child_ppid_reads_as_pid(tmp_path, make_cfg):
    # KNOWN LIMIT, pinned on purpose (see lg_codec.set_pids): a warm child masks the server's pid as "<pid>" for modules
    # imported before the fork, and the server is also the child's real parent, so os.getppid() reads "<pid>" as well.
    # An edit from getpid to getppid is therefore verified under warm workers and a difference under cold ones. If this
    # test starts failing, the limit changed: update the docs (CLAUDE.md 6.8d, HANDOFF follow-ups) with it.
    new = PPID_SRC.replace("return os.getpid(), n", "return os.getppid(), n")
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new})
    c = make_cfg(root, behavioral_diff={"warm": True, "mutants": 0})
    base = write_tree(c.state_dir / "baseline", {"pkg/__init__.py": "", "pkg/m.py": PPID_SRC})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    try:
        warm_got = diff.run(c, str(base), str(root), targets, harvest={})[0]
        assert warm_got.status == diff.DONE, (warm_got.status, warm_got.reason, warm_got.differences[:1])
        cold = make_cfg(root, behavioral_diff={"warm": False, "mutants": 0})
        cold_got = diff.run(cold, str(base), str(root), targets, harvest={})[0]
        assert cold_got.status == diff.DIFFERENT, (cold_got.status, cold_got.reason)
        assert "<pid>" in cold_got.differences[0]["before"] and "<parent pid>" in cold_got.differences[0]["after"]
    finally:
        stop(c, str(base), str(root))


CWD_SRC = '''
import os

def where(n: int) -> str:
    return os.path.realpath(os.getcwd())

def scribble(n: int) -> int:
    with open("scribble.txt", "w") as fh:
        fh.write("x")
    return n
'''


def test_warm_child_runs_in_the_checks_working_folder_and_refuses_relative_writes(tmp_path, make_cfg):
    """Task 7.4: the warm child chdirs into the folder the hook made, and protects it like the cold worker does."""
    from conftest import call
    from runner import scratch
    from guardlib import config as cfgmod
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": CWD_SRC})
    spec = [{"module": "pkg.m", "qualname": "where", "inputs": [call(1)]},
            {"module": "pkg.m", "qualname": "scribble", "inputs": [call(1)]}]
    got = {}
    for is_warm in (True, False):
        c = make_cfg(root, behavioral_diff={"warm": is_warm, "os_sandbox": "off"})
        py = cfgmod.target_python(c)
        with scratch.for_trees(c, str(tmp_path / "nothing"), str(root)):
            with scratch.current(c, str(root)) as cwd:
                out = diff._run_worker(c, py, str(root), [str(root)], spec, 60)
        got[is_warm] = (out, cwd)
        if is_warm:  # the warm server really ran the request
            assert (warm._send(warm.socket_path(c, py, str(root), "work"), {"cmd": "ping"}, 2) or {}).get("runs", 0) >= 1
    try:
        for is_warm, (out, cwd) in got.items():
            assert cwd.strategy in ("clone", "copy")
            # The hook's pid in the folder name may read as <parent pid> (lg_codec.canon masks it in the cold worker).
            value = out["pkg.m:where"]["results"][0]["value"]["v"]
            real = os.path.realpath(cwd.path)
            assert value.split(scratch.PREFIX)[0] == real.split(scratch.PREFIX)[0]
            assert value.rsplit("-", 1)[1] == real.rsplit("-", 1)[1]
            assert out["pkg.m:scribble"]["results"][0]["outcome"] != "return"
        assert got[True][0]["pkg.m:scribble"]["results"][0] == got[False][0]["pkg.m:scribble"]["results"][0]
        assert not (root / "scribble.txt").exists()
    finally:
        warm.stop_all(make_cfg(root), cfgmod.target_python(make_cfg(root)), [(str(root), "work")])
