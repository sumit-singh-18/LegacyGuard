"""Writes during import and input setup (Task 6.8a): cache-like writes are redirected to the run's scratch folder,
everything else stays refused, and the compared call itself still cannot write at all."""
from __future__ import annotations

import glob
import json
import os
import subprocess
import tempfile

import pytest

from runner import diff, warm

from conftest import TARGET_PY, WORKER, call, write_tree

# A module that builds a lookup table at import and caches it as a pickle next to itself, then reads it back.
PICKLE_NEXT_TO_MODULE = '''
import os, pickle

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "table.pickle")

def _build():
    return {i: i * i for i in range(10)}

with open(CACHE, "wb") as fh:
    pickle.dump(_build(), fh)
with open(CACHE, "rb") as fh:
    TABLE = pickle.load(fh)

def lookup(k):
    return TABLE.get(k, -1)
'''

# A library that keeps its cache under $XDG_CACHE_HOME or ~/.cache, written atomically (temp file in the same
# folder, then os.replace), the way many libraries do it.
ENV_CACHE = '''
import os, pickle, tempfile

DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "fixturelib")
os.makedirs(DIR, exist_ok=True)
os.makedirs(DIR, exist_ok=True)
PATH = os.path.join(DIR, "grammar.bin")
with tempfile.NamedTemporaryFile(dir=DIR, delete=False) as tmp:
    pickle.dump(["a", "b", "c"], tmp)
os.replace(tmp.name, PATH)
with open(PATH, "rb") as fh:
    WORDS = pickle.load(fh)

def word(i):
    return WORDS[i % len(WORDS)]
'''

# A class whose constructor writes an index file into a cache folder next to the module, and a temp file.
INIT_WRITES = '''
import os, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

class Store:
    def __init__(self):
        folder = os.path.join(HERE, ".cache")
        os.makedirs(folder, exist_ok=True)
        self.index = os.path.join(folder, "store.idx")
        with open(self.index, "w") as fh:
            fh.write("3")
        fd, self.tmp = tempfile.mkstemp()
        os.close(fd)

    def get(self, k):
        with open(self.index) as fh:
            return int(fh.read()) * k
'''

# Negative shapes: a write during the compared call, and import-time writes outside any cache.
WRITES_IN_CALL = '''
import os

HERE = os.path.dirname(os.path.abspath(__file__))

def save(k):
    with open(os.path.join(HERE, "cache.pickle"), "wb") as fh:
        fh.write(bytes([k]))
    return k
'''

IMPORT_WRITES_PROJECT = '''
import os

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "output.txt"), "w") as fh:
    fh.write("x")

def f(x):
    return x
'''

IMPORT_WRITES_HOME = '''
import os

with open(os.path.expanduser("~/.fixturerc"), "w") as fh:
    fh.write("x")

def f(x):
    return x
'''


def run(tmp_path, root, module, qualname, items, env_home=None, **extra):
    """lg_worker.py on root, with HOME pointed at a protected folder so nothing reaches the real home."""
    home = env_home or (tmp_path / "home")
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(home)
    req, res = tmp_path / "req.json", tmp_path / "res.json"
    req.write_text(json.dumps({"root": str(root), "sys_path": [str(root)], "protect": [str(root), str(home)],
                               "functions": [{"module": module, "qualname": qualname, "inputs": items}], **extra}))
    done = subprocess.run([TARGET_PY, "-B", str(WORKER), str(req), str(res)], cwd=root, env=env,
                          capture_output=True, text=True, timeout=60)
    assert res.exists(), done.stderr[-2000:]
    return json.loads(res.read_text())[f"{module}:{qualname}"]


def test_pickle_cache_next_to_module_is_redirected(tmp_path):
    root = write_tree(tmp_path / "proj", {"tab.py": PICKLE_NEXT_TO_MODULE})
    scratch = tmp_path / "scr"
    r = run(tmp_path, root, "tab", "lookup", [call(3), call(11)], scratch=str(scratch))
    assert r["status"] == "ran", r
    assert [x["value"] for x in r["results"]] == [{"t": "v", "v": 9}, {"t": "v", "v": -1}]
    assert not (root / "table.pickle").exists()  # the real tree is untouched
    shadow = scratch / "shadow" / str(os.path.realpath(root / "table.pickle")).lstrip(os.sep)
    assert shadow.is_file()  # the write went to the run's scratch folder


def test_env_based_cache_dir_is_redirected(tmp_path):
    root = write_tree(tmp_path / "proj", {"lib.py": ENV_CACHE})
    r = run(tmp_path, root, "lib", "word", [call(0), call(4)])
    assert r["status"] == "ran", r
    assert [x["value"]["v"] for x in r["results"]] == ["a", "b"]
    assert not (tmp_path / "home" / ".cache").exists()


def test_constructor_writes_are_redirected(tmp_path):
    root = write_tree(tmp_path / "proj", {"store.py": INIT_WRITES})
    r = run(tmp_path, root, "store", "Store.get", [call(2), call(5)])
    assert r["status"] == "ran", r
    assert [x["outcome"] for x in r["results"]] == ["return", "return"], r
    assert [x["value"]["v"] for x in r["results"]] == [6, 15]
    assert not (root / ".cache").exists()


def test_write_during_the_call_is_still_blocked(tmp_path):
    root = write_tree(tmp_path / "proj", {"sv.py": WRITES_IN_CALL})
    r = run(tmp_path, root, "sv", "save", [call(1)])
    assert r["status"] == "ran"
    out = r["results"][0]
    assert out["outcome"] == "blocked" and "file write" in out["blocked_kinds"], out
    assert not (root / "cache.pickle").exists()


def test_import_write_into_the_project_is_still_blocked(tmp_path):
    root = write_tree(tmp_path / "proj", {"bad.py": IMPORT_WRITES_PROJECT})
    r = run(tmp_path, root, "bad", "f", [call(1)])
    assert r["status"] == "import_blocked" and "file write" in r["reason"], r
    assert not (root / "output.txt").exists()


def test_import_write_into_home_outside_a_cache_is_still_blocked(tmp_path):
    root = write_tree(tmp_path / "proj", {"badhome.py": IMPORT_WRITES_HOME})
    r = run(tmp_path, root, "badhome", "f", [call(1)])
    assert r["status"] == "import_blocked" and "file write" in r["reason"], r
    assert not (tmp_path / "home" / ".fixturerc").exists()


def test_own_scratch_folder_is_deleted_after_the_run(tmp_path):
    root = write_tree(tmp_path / "proj", {"tab.py": PICKLE_NEXT_TO_MODULE})
    pattern = os.path.join(tempfile.gettempdir(), "lg-scratch-*")
    before = set(glob.glob(pattern))
    r = run(tmp_path, root, "tab", "lookup", [call(2)])  # no "scratch" in the request: the worker makes its own
    assert r["status"] == "ran"
    assert set(glob.glob(pattern)) <= before


# ---- end to end: baseline and working tree, old run twice and new once ----

COUNTS_RUNS = '''
import os, pickle

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__lgcache__", "runs.pickle")
os.makedirs(os.path.dirname(CACHE), exist_ok=True)
try:
    with open(CACHE, "rb") as fh:
        RUNS = pickle.load(fh) + 1
except FileNotFoundError:
    RUNS = 1
with open(CACHE, "wb") as fh:
    pickle.dump(RUNS, fh)

def runs(k: int) -> int:
    return RUNS * k
'''


def e2e(tmp_path, make_cfg, old_src, new_src, **cfg):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new_src})
    c = make_cfg(root, **cfg)
    base = write_tree(c.state_dir / "baseline", {"pkg/__init__.py": "", "pkg/m.py": old_src})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return c, str(base), str(root), targets


def test_e2e_import_cache_is_checked_and_runs_do_not_share_it(tmp_path, make_cfg):
    new = COUNTS_RUNS.replace("return RUNS * k", "return k * RUNS")
    c, base, root, targets = e2e(tmp_path, make_cfg, COUNTS_RUNS, new)
    r = diff.run(c, base, root, targets, harvest={})[0]
    assert r.status == diff.DONE, r.reason  # old twice and new once agree: each run started from no cache
    assert not (tmp_path / "proj/pkg/__lgcache__").exists() and not (tmp_path / "proj/.legacyguard/baseline/pkg/__lgcache__").exists()
    broken = COUNTS_RUNS.replace("return RUNS * k", "return RUNS * k + 1")
    (tmp_path / "proj/pkg/m.py").write_text(broken.lstrip("\n"))
    t2 = diff.changed_targets("pkg/m.py", COUNTS_RUNS.lstrip("\n"), broken.lstrip("\n"))
    assert diff.run(c, base, root, t2, harvest={})[0].status == diff.DIFFERENT


def test_e2e_import_write_into_project_stays_untested(tmp_path, make_cfg):
    new = IMPORT_WRITES_PROJECT.replace("return x", "return x + 0")
    c, base, root, targets = e2e(tmp_path, make_cfg, IMPORT_WRITES_PROJECT, new)
    r = diff.run(c, base, root, targets, harvest={})[0]
    assert r.status == diff.UNTESTED and r.reason.startswith("importing the module file write is blocked"), r.reason
    assert not (tmp_path / "proj/pkg/output.txt").exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="warm workers need fork()")
def test_e2e_warm_servers_match_cold_and_do_not_leak(tmp_path, make_cfg):
    new = COUNTS_RUNS.replace("return RUNS * k", "return k * RUNS")
    c, base, root, targets = e2e(tmp_path, make_cfg, COUNTS_RUNS, new, behavioral_diff={"warm": True})
    from guardlib import config as cfgmod
    py = cfgmod.target_python(c)
    try:
        for _ in range(3):  # later checks reuse both servers: a cache written by one check must not reach the next
            r = diff.run(c, base, root, targets, harvest={})[0]
            assert r.status == diff.DONE, r.reason
        ping = warm._send(warm.socket_path(c, py, base, "baseline"), {"cmd": "ping"}, 2)
        assert ping and ping["runs"] >= 6 and "pkg.m" in ping["warm"]
        assert not (tmp_path / "proj/pkg/__lgcache__").exists() and not (tmp_path / "proj/.legacyguard/baseline/pkg/__lgcache__").exists()
    finally:
        warm.stop_all(c, py, [(base, "baseline"), (root, "work")])


def test_e2e_redirect_works_inside_the_os_sandbox(tmp_path, make_cfg):
    """The shadow folder lives in the run's temp folder, which the OS sandbox already leaves writable."""
    from runner import os_sandbox
    new = COUNTS_RUNS.replace("return RUNS * k", "return k * RUNS")
    c, base, root, targets = e2e(tmp_path, make_cfg, COUNTS_RUNS, new, behavioral_diff={"os_sandbox": "auto"})
    r = diff.run(c, base, root, targets, harvest={})[0]
    assert r.status == diff.DONE, r.reason
    if os_sandbox.describe() not in ("sandbox-exec", "bubblewrap"):
        pytest.skip(f"no OS sandbox here: {os_sandbox.describe()}")
