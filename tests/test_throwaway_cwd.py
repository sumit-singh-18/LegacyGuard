"""Throwaway working folders (Task 7.4): every process that runs project code for a check gets a clone, a copy, an
empty folder or (too large, OS sandbox active) the real tree read-only, never the real tree as a writable cwd."""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from guardlib import config as cfgmod
from guardlib import report
from runner import diff, os_sandbox, scratch

from conftest import TARGET_PY, write_tree

HAS_SANDBOX = os_sandbox.available(TARGET_PY)


def fake_clone(src, dst):
    """A clone that always works (a plain copy), so tests do not depend on this machine's file system."""
    shutil.copy2(src, dst, follow_symlinks=False)


def small_tree(root: Path) -> Path:
    return write_tree(root, {"pkg/__init__.py": "", "pkg/m.py": "X = 1\n", "data/table.txt": "a,b\n"})


@pytest.fixture
def fresh(monkeypatch):
    """No leftovers from other tests in the module registry or labels."""
    monkeypatch.setattr(scratch, "_REG", {})
    monkeypatch.setattr(scratch, "_LAST", {})
    yield


# ---- unit: strategy choice ----

def test_auto_on_a_small_tree_matches_this_machines_probe(tmp_path, make_cfg, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    ok, _ = scratch._probe(c, str(root), "pkg/m.py")
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.strategy == ("clone" if ok else "copy")
        assert cwd.label.startswith(cwd.strategy)
        assert "3 files" in cwd.label  # counts in the label
        assert (Path(cwd.path) / "data/table.txt").read_text() == "a,b\n"
        assert not scratch._inside(cwd.path, str(root))
    finally:
        scratch.release(cwd)
    assert not os.path.exists(cwd.folder)


@pytest.mark.parametrize("code, words", [(errno.EXDEV, "another file system"), (errno.ENOTSUP, "no copy-on-write"),
                                         (getattr(errno, "EOPNOTSUPP", errno.ENOTSUP), "no copy-on-write")])
def test_probe_failure_falls_back_to_copy(tmp_path, make_cfg, monkeypatch, fresh, code, words):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)

    def refuse(src, dst):
        raise OSError(code, os.strerror(code))
    monkeypatch.setattr(scratch, "_clone_one", refuse)
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.strategy == "copy" and words in cwd.label
        assert (Path(cwd.path) / "pkg/m.py").exists()
    finally:
        scratch.release(cwd)


@pytest.mark.parametrize("sandboxed, strategy", [(True, "off"), (False, "empty")])
def test_too_large_to_copy_without_clone(tmp_path, make_cfg, monkeypatch, fresh, sandboxed, strategy):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    monkeypatch.setattr(scratch, "MAX_COPY_FILES", 1)
    monkeypatch.setattr(scratch, "_probe", lambda cfg, tree, sample: (False, "no copy-on-write on this file system"))
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=sandboxed)
    try:
        assert cwd.strategy == strategy
        if strategy == "off":
            assert cwd.path == str(root) and "read-only under the OS sandbox" in cwd.label
        else:
            assert os.listdir(cwd.path) == [] and "no OS sandbox" in cwd.label
        assert "3 files" in cwd.label
    finally:
        scratch.release(cwd)


def test_over_the_clone_cap_copies_when_small(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    monkeypatch.setattr(scratch, "MAX_CLONE_FILES", 1)
    monkeypatch.setattr(scratch, "_clone_one", fake_clone)
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.strategy == "copy" and "more than 1 files" in cwd.label
    finally:
        scratch.release(cwd)


@pytest.mark.parametrize("sandboxed, strategy", [(True, "off"), (False, "empty")])
def test_clone_failing_on_a_large_file_after_a_good_probe_is_not_copied_in_full(tmp_path, make_cfg, monkeypatch, fresh,
                                                                                sandboxed, strategy):
    """No uncapped copy fallback: the per-file copy after a failed clone counts bytes and gives up at MAX_COPY_BYTES."""
    root = small_tree(tmp_path / "proj")
    (root / "data/big.bin").write_bytes(b"x" * 4096)
    c = make_cfg(root)
    monkeypatch.setattr(scratch, "MAX_COPY_BYTES", 1024)
    copies = []

    def clone(src, dst):
        if src.endswith("big.bin"):
            raise OSError(errno.EXDEV, "cross-device")
        fake_clone(src, dst)
    monkeypatch.setattr(scratch, "_clone_one", clone)
    monkeypatch.setattr(scratch, "_probe", lambda cfg, tree, sample: (True, ""))
    monkeypatch.setattr(scratch, "_copy_one", lambda s, d: copies.append(s) or fake_clone(s, d))
    before = set(os.listdir(tempfile.gettempdir()))
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=sandboxed)
    try:
        assert cwd.strategy == strategy, cwd.label
        assert "another file system" in cwd.label and "4 files" in cwd.label
        assert copies == []  # the big file was never copied
        leftover = {n for n in set(os.listdir(tempfile.gettempdir())) - before if n.startswith(scratch.PREFIX)}
        assert leftover == ({os.path.basename(cwd.folder)} if cwd.owned else set())  # the half-made clone is gone
    finally:
        scratch.release(cwd)


def test_clone_failing_on_a_small_file_copies_it_and_says_so(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)

    def clone(src, dst):
        if src.endswith("table.txt"):
            raise OSError(errno.EXDEV, "cross-device")
        fake_clone(src, dst)
    monkeypatch.setattr(scratch, "_clone_one", clone)
    monkeypatch.setattr(scratch, "_probe", lambda cfg, tree, sample: (True, ""))
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.strategy == "clone" and "1 copied" in cwd.label
        assert (Path(cwd.path) / "data/table.txt").read_text() == "a,b\n"
    finally:
        scratch.release(cwd)


def test_modes_from_config(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    monkeypatch.setattr(scratch, "_clone_one", fake_clone)
    got = {}
    for mode in ("off", "empty", "clone", "copy", "weird"):
        c = make_cfg(root, behavioral_diff={"cwd_mode": mode})
        cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
        got[mode] = cwd
        scratch.release(cwd)
    assert got["off"].strategy == "off" and got["off"].path == str(root) and "switched off" in got["off"].label
    assert got["empty"].strategy == "empty"
    assert got["clone"].strategy == "clone"
    assert got["copy"].strategy == "copy"
    assert got["weird"].strategy == "clone" and "unknown cwd_mode 'weird', used auto" in got["weird"].label


def test_cwd_subfolder(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    monkeypatch.setattr(scratch, "_clone_one", fake_clone)
    c = make_cfg(root, behavioral_diff={"cwd": "data"})
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.path == os.path.join(cwd.folder, "data") and os.path.exists(os.path.join(cwd.path, "table.txt"))
    finally:
        scratch.release(cwd)
    c = make_cfg(root, behavioral_diff={"cwd": "nowhere"})
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.path == cwd.folder and "cwd 'nowhere' not found" in cwd.label
    finally:
        scratch.release(cwd)
    c = make_cfg(root, behavioral_diff={"cwd": "data", "cwd_mode": "empty"})
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    try:
        assert cwd.path == os.path.join(cwd.folder, "data") and os.path.isdir(cwd.path)
    finally:
        scratch.release(cwd)


def test_temp_folder_inside_the_project_means_off(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    inner = root / "tmpdir"
    inner.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(inner))
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    assert cwd.strategy == "off" and cwd.path == str(root) and "temp folder is inside the project" in cwd.label
    assert [n for n in os.listdir(inner) if n.startswith(scratch.PREFIX)] == []


def test_only_the_probe_is_cached(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    calls = []
    monkeypatch.setattr(scratch, "_clone_one", fake_clone)
    real_probe = scratch._probe
    monkeypatch.setattr(scratch, "_probe", lambda *a: calls.append(1) or real_probe(*a))
    for _ in range(2):
        scratch.release(scratch.prepare_cwd(c, str(root), sandboxed=False))
    assert len(calls) == 1  # the second check reads the cache
    # Thresholds are recomputed each call: more files than the copy cap and no clone change the strategy, no new probe.
    monkeypatch.setattr(scratch, "MAX_CLONE_FILES", 1)
    monkeypatch.setattr(scratch, "MAX_COPY_FILES", 1)
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=True)
    assert cwd.strategy == "off" and len(calls) == 1
    # Another mode, or another device for the temp folder, probes again.
    c2 = make_cfg(root, behavioral_diff={"cwd_mode": "clone"})
    scratch.release(scratch.prepare_cwd(c2, str(root), sandboxed=False))
    assert len(calls) == 2
    real_dev = scratch._dev
    tmp = os.path.realpath(tempfile.gettempdir())
    monkeypatch.setattr(scratch, "_dev", lambda p: real_dev(p) + (1 if p == tmp else 0))
    monkeypatch.setattr(scratch, "MAX_CLONE_FILES", 20_000)
    scratch.release(scratch.prepare_cwd(c, str(root), sandboxed=False))
    assert len(calls) == 3


def test_listing_rule(tmp_path, monkeypatch):
    root = write_tree(tmp_path / "proj", {
        "pkg/__init__.py": "", ".env": "K=1\n", "data/local.json": "{}", ".gitignore": ".env\ndata/local.json\n",
        ".git/HEAD": "ref", "pkg/__pycache__/m.pyc": "", "venv/pyvenv.cfg": "", "venv/lib/x.py": "",
        "node_modules/a.js": "", "vendor/sub/.git": "gitdir: x", "vendor/sub/y.py": "", "build/lib/z.py": "",
        "dist/p.whl": "", ".eggs/e.py": "", "pkg.egg-info/PKG-INFO": "", "htmlcov/index.html": "",
        "_build/i.html": "", "pkg/build/b.py": "", "pkg/dist/d.py": "",
        "docs/_build/k.html": "", "env/conda-meta/history": "", "env/lib/c.py": "", "mnt/far.py": "",
    })
    os.symlink("pkg", root / "link_to_pkg")
    wt = write_tree(tmp_path / "wt", {".git": "gitdir: /elsewhere\n", "m.py": ""})  # a git worktree's pointer file
    assert {r for r, _, _ in scratch._list_tree(str(wt), None)[0]} == {"m.py"}
    real_dev = scratch._dev
    monkeypatch.setattr(scratch, "_dev", lambda p: real_dev(p) + (1 if os.path.basename(p) == "mnt" else 0))
    entries, files, nbytes, over = scratch._list_tree(str(root), None)
    names = {r for r, kind, _ in entries if kind != "dir"}
    # build, dist and _build are pruned only at the top level: pkg/build/ and docs/_build/ are kept.
    assert names == {"pkg/__init__.py", ".env", "data/local.json", ".gitignore", "link_to_pkg", "pkg/build/b.py",
                     "pkg/dist/d.py", "docs/_build/k.html"}
    assert dict((r, k) for r, k, _ in entries)["link_to_pkg"] == "link"
    assert files == 8 and not over


def test_working_folder_location_release_and_sweep(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    folder = scratch._location(c, str(root))
    assert folder and not scratch._inside(folder, str(root)) and not scratch._inside(folder, c.root)
    shutil.rmtree(folder)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    gone = tempfile.mkdtemp(prefix=f"{scratch.PREFIX}{scratch._b36(dead.pid)}-")
    alive = tempfile.mkdtemp(prefix=f"{scratch.PREFIX}{scratch._b36(os.getppid())}-")
    try:
        scratch._sweep()
        assert not os.path.exists(gone) and os.path.exists(alive)
    finally:
        shutil.rmtree(alive, ignore_errors=True)
        shutil.rmtree(gone, ignore_errors=True)


def test_ficlone_number_is_not_guessed_on_other_architectures(monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.delattr(fcntl, "FICLONE", raising=False)
    monkeypatch.setattr(scratch.platform, "machine", lambda: "ppc64le")
    assert scratch._ficlone_request() is None
    monkeypatch.setattr(scratch.platform, "machine", lambda: "x86_64")
    assert scratch._ficlone_request() == scratch.FICLONE_GENERIC


def test_for_trees_prepares_lazily_once_and_releases(tmp_path, make_cfg, monkeypatch, fresh):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    base = small_tree(c.state_dir / "baseline")
    made = []
    real = scratch.prepare_cwd
    monkeypatch.setattr(scratch, "prepare_cwd", lambda *a: made.append(a[1]) or real(*a))
    with scratch.for_trees(c, str(base), str(root)):
        assert made == []  # nothing ran yet: nothing prepared
        with scratch.for_trees(c, str(base), str(root)):  # an inner block (diff.run) reuses the outer one's
            with scratch.current(c, str(root)) as w1:
                pass
        with scratch.current(c, str(root)) as w2:
            pass
        assert w1 is w2 and os.path.exists(w1.path)
    assert made == [str(root)]
    assert not os.path.exists(w1.folder) and scratch._REG == {}
    assert scratch.describe().startswith("work: ")


# ---- end to end through diff.run ----

FILES = '''
import os


def read_table(n: int) -> int:
    with open("data/table.txt") as fh:
        return len(fh.read()) + n


def write_out(n: int) -> int:
    with open("out.txt", "w") as fh:
        fh.write(str(n))
    return n


def read_local(n: int) -> int:
    with open("data/local.json") as fh:
        return len(fh.read()) + n


def where(n: int) -> str:
    return os.path.basename(os.getcwd()) + str(n)
'''


def snapshot(root: Path) -> dict:
    """Names, sizes and mtimes of the project's files; the guard's own state folder is left out, except the baseline."""
    base = root / ".legacyguard" / "baseline"
    out = {f"baseline:{k}": v for k, v in snapshot_dir(base).items()} if base.exists() else {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".legacyguard"]
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


def snapshot_dir(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for f in filenames:
            p = os.path.join(dirpath, f)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


def project(tmp_path, make_cfg, edited: str, **bd):
    files = {"pkg/__init__.py": "", "data/table.txt": "a,b\n", ".gitignore": "data/local.json\n"}
    root = write_tree(tmp_path / "proj", {**files, "pkg/f.py": edited, "data/local.json": "{}"})
    c = make_cfg(root, behavioral_diff={"os_sandbox": "off", **bd})
    base = write_tree(c.state_dir / "baseline", {**files, "pkg/f.py": FILES})
    return c, str(base), str(root)


def targets(base, root, names):
    ts = diff.changed_targets("pkg/f.py", (Path(base) / "pkg/f.py").read_text(), (Path(root) / "pkg/f.py").read_text())
    return [t for t in ts if t.qualname in names]


def edited_all():
    return (FILES.replace("+ n\n", "+ n + 0\n").replace("return n\n", "return n + 0\n")
            .replace("str(n)\n", "str(n + 0)\n"))


@pytest.mark.parametrize("mode", ["auto", "copy"])
def test_acceptance_relative_read_and_write_and_trees_unchanged(tmp_path, make_cfg, fresh, mode):
    c, base, root = project(tmp_path, make_cfg, edited_all(), cwd_mode=mode)
    before = (snapshot(Path(base)), snapshot(Path(root)))
    res = {r.qualname: r for r in diff.run(c, base, root, targets(base, root, {"read_table", "write_out"}), harvest={})}
    assert res["read_table"].status in (diff.DONE, diff.UNTESTED) and "could not run" not in res["read_table"].reason
    assert res["read_table"].status == diff.DONE, res["read_table"].reason
    assert res["write_out"].status == diff.UNTESTED and "writ" in res["write_out"].reason
    off = make_cfg(Path(root), behavioral_diff={"os_sandbox": "off", "cwd_mode": "off"})
    res_off = {r.qualname: r for r in diff.run(off, base, root, targets(base, root, {"write_out"}), harvest={})}
    assert res_off["write_out"].reason == res["write_out"].reason
    assert (snapshot(Path(base)), snapshot(Path(root))) == before
    assert not os.path.exists(os.path.join(root, "out.txt"))
    assert scratch.describe().split(": ", 1)[0] in ("work", "baseline")


def test_ignored_file_read_behaves_as_with_cwd_off(tmp_path, make_cfg, fresh):
    c, base, root = project(tmp_path, make_cfg, edited_all())
    got = diff.run(c, base, root, targets(base, root, {"read_local"}), harvest={})[0]
    off = make_cfg(Path(root), behavioral_diff={"os_sandbox": "off", "cwd_mode": "off"})
    want = diff.run(off, base, root, targets(base, root, {"read_local"}), harvest={})[0]
    assert (got.status, got.reason) == (want.status, want.reason)


def test_old_a_and_old_b_share_one_working_folder(tmp_path, make_cfg, fresh):
    c, base, root = project(tmp_path, make_cfg, edited_all())
    r = diff.run(c, base, root, targets(base, root, {"where"}), harvest={})[0]
    assert "non-deterministic" not in r.reason
    assert r.status == diff.DIFFERENT  # the baseline's folder and the working tree's are two folders


FMTLIB = '''
import os, pickle

CACHE = os.path.join(os.path.expanduser("~"), ".cache", "fmtlib", "1.0")
os.makedirs(CACHE, exist_ok=True)
with open(os.path.join(CACHE, "grammar.pickle"), "wb") as fh:
    pickle.dump([1, 2, 3], fh)


def width(n: int) -> int:
    return n * 2
'''


@pytest.mark.parametrize("sandbox", ["auto", "off"])
def test_import_writing_an_absolute_cache_path_runs_with_a_clone_cwd(tmp_path, make_cfg, monkeypatch, fresh, sandbox):
    files = {"fmtlib/__init__.py": ""}
    root = write_tree(tmp_path / "proj", {**files, "fmtlib/core.py": FMTLIB.replace("n * 2", "n + n")})
    c = make_cfg(root, behavioral_diff={"os_sandbox": sandbox})
    base = write_tree(c.state_dir / "baseline", {**files, "fmtlib/core.py": FMTLIB})
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ts = diff.changed_targets("fmtlib/core.py", FMTLIB.lstrip("\n"), (root / "fmtlib/core.py").read_text())
    r = diff.run(c, str(base), str(root), ts, harvest={})[0]
    assert r.status == diff.DONE, r.reason
    assert not (home / ".cache").exists()
    assert scratch.describe().split("work: ", 1)[1].split(" ", 1)[0] in ("clone", "copy")
    if sandbox == "auto" and HAS_SANDBOX:
        assert os_sandbox.describe() in ("sandbox-exec", "bubblewrap")


def test_report_names_the_working_folder_and_the_missing_os_sandbox(tmp_path, make_cfg, fresh):
    from guardlib import engine
    c, base, root = project(tmp_path, make_cfg, edited_all())
    diff.run(c, base, root, targets(base, root, {"read_table"}), harvest={})
    data = {"session_id": "s", "prompts_used": 0, "wakeups": 0, "findings": []}
    engine.record_isolation(data)
    text = report.render(data, c)
    label = scratch.describe()
    assert f"working folder **{label}**" in text
    assert label.split("work: ", 1)[1].split(" ", 1)[0] in ("clone", "copy") and " files" in label
    assert "No OS sandbox: absolute-path writes are stopped only by the in-process guards." in text
    if HAS_SANDBOX:
        c2 = make_cfg(Path(root), behavioral_diff={"os_sandbox": "auto"})
        diff.run(c2, base, root, targets(base, root, {"read_table"}), harvest={})
        engine.record_isolation(data)
        text = report.render(data, c2)
        assert "OS sandbox **sandbox-exec**" in text or "OS sandbox **bubblewrap**" in text
        assert "No OS sandbox" not in text


BYPASS = '''
import os, sys

TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def escape(n: int) -> int:
    (sys.modules.get("lg_worker") or sys.modules["__main__"]).GUARD_WRITES["allowed"] = True
    for path in ("rel_escape.txt", os.path.join(TREE, "abs_escape.txt")):
        try:
            with open(path, "w") as fh:
                fh.write("x")
        except OSError:
            pass
    return n
'''


@pytest.mark.skipif(not HAS_SANDBOX, reason="needs an OS sandbox")
def test_writes_past_the_in_process_layers_leave_the_tree_unchanged(tmp_path, make_cfg, fresh):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/f.py": BYPASS.replace("return n", "return n + 0")})
    c = make_cfg(root, behavioral_diff={"os_sandbox": "auto", "self_diff": False})
    base = write_tree(c.state_dir / "baseline", {"pkg/__init__.py": "", "pkg/f.py": BYPASS})
    before = snapshot(root)
    ts = diff.changed_targets("pkg/f.py", BYPASS.lstrip("\n"), (root / "pkg/f.py").read_text())
    seen = {}
    real = scratch.release

    def keep(cwd):  # look inside the working folder before it is deleted
        if cwd is not None and cwd.owned:
            seen[cwd.tree] = os.listdir(cwd.path)
        real(cwd)
    scratch.release = keep
    try:
        diff.run(c, str(base), str(root), ts, harvest={})
    finally:
        scratch.release = real
    assert snapshot(root) == before
    assert not (base / "abs_escape.txt").exists() and not (base / "rel_escape.txt").exists()
    assert "rel_escape.txt" in seen.get(str(root), [])  # the relative write landed in the throwaway folder


# ---- timing (recorded in docs/PROGRESS.md) ----

def test_timing_2000_files(tmp_path, make_cfg, monkeypatch, fresh):
    root = tmp_path / "big"
    n = 0
    for p in range(40):
        for s in range(7):
            d = root / f"pkg{p}" / f"sub{s}"
            d.mkdir(parents=True)
            for f in range(7 if (p * 7 + s) < 160 else 8):
                if n >= 2000:
                    break
                (d / f"m{f}.py").write_text("x = 1\n" * (50 + f * 20))
                n += 1
    c = make_cfg(root)
    cwd = scratch.prepare_cwd(c, str(root), sandboxed=False)
    scratch.release(cwd)
    monkeypatch.setattr(scratch, "_probe", lambda *a: (False, "forced copy"))
    cp = scratch.prepare_cwd(make_cfg(root, behavioral_diff={"cwd_mode": "copy"}), str(root), sandboxed=False)
    scratch.release(cp)
    print(f"\n2k files: {cwd.label} | {cp.label}")
    assert cwd.files == cp.files and cwd.seconds < 10 and cp.seconds < 10


# ---- one working folder per tree per check, across the diff and the affected tests ----

def test_post_edit_prepares_one_folder_per_tree_for_diff_and_affected_tests(tmp_path, make_cfg, monkeypatch, fresh):
    from conftest import PLUGIN
    from guardlib import ledger
    from test_affected_tests import BANK, project as bank_project
    sys.path.insert(0, str(PLUGIN / "scripts"))
    try:
        import _common
        import post_edit
    finally:
        sys.path.remove(str(PLUGIN / "scripts"))
    root, cfg = bank_project(tmp_path, make_cfg, BANK.replace("if amount > balance:", "if amount >= balance:"))
    cfg.raw["behavioral_diff"]["os_sandbox"] = "off"
    with ledger.locked(cfg.state_dir) as data:
        data["baseline_ok"] = True
    monkeypatch.setattr(post_edit, "load_guard", lambda event, fp=None: (str(root), cfg))
    made, used = [], []
    real_prepare = scratch.prepare_cwd
    monkeypatch.setattr(scratch, "prepare_cwd", lambda *a: made.append(os.path.realpath(a[1])) or real_prepare(*a))
    real_current = scratch.current

    @__import__("contextlib").contextmanager
    def spy(c, r):
        with real_current(c, r) as cwd:
            used.append(cwd.path)
            yield cwd
    monkeypatch.setattr(scratch, "current", spy)
    with pytest.raises(_common.Wake) as woke:
        post_edit.handle({"tool_input": {"file_path": str(root / "bank/core.py")}, "cwd": str(root)})
    assert "test regression" in str(woke.value)  # the affected tests ran too
    assert sorted(made) == sorted({os.path.realpath(root), os.path.realpath(cfg.state_dir / "baseline")})
    assert len(set(used)) == 2 and len(used) > 2  # the diff's workers and both pytest runs shared the two folders
    iso = ledger.load(cfg.state_dir)["isolation"]
    assert iso["os_sandbox"] == "switched off in config" and "work: " in iso["cwd"] and "baseline: " in iso["cwd"]
    # Part B: the working tree's folder is gone; the baseline's is the session's cached one and stays for later checks.
    kept = scratch._read_manifest(cfg)["path"]
    assert "built for this session" in iso["cwd"]
    assert scratch._REG == {} and [p for p in set(used) if os.path.exists(p)] == [kept]


def test_folder_name_pid_is_base36_and_sandboxed_fails_open(tmp_path, make_cfg, monkeypatch):
    root = small_tree(tmp_path / "proj")
    c = make_cfg(root)
    folder = scratch._location(c, str(root))
    try:
        assert os.path.basename(folder).startswith(f"{scratch.PREFIX}{scratch._b36(os.getpid())}-")
        assert str(os.getpid()) not in os.path.basename(folder)
    finally:
        shutil.rmtree(folder)
    assert int(scratch._b36(123456), 36) == 123456 and scratch._b36(0) == "0"
    monkeypatch.setattr(cfgmod, "target_python", lambda cfg: None)
    assert scratch.sandboxed(c) is False


SUB_ESCAPE = """
def up(n: int) -> int:
    with open("../x.txt", "w") as fh:
        fh.write("x")
    return n
"""


@pytest.mark.parametrize("is_warm", [False, True])
def test_cwd_subfolder_write_to_its_parent_is_refused(tmp_path, make_cfg, fresh, is_warm):
    """With `cwd: data` the cwd is <folder>/data; `../x.txt` lands in <folder>, which must be protected as the tree
    was before 7.4 (then it landed in the tree and was refused)."""
    if is_warm and not hasattr(os, "fork"):
        pytest.skip("warm workers need fork()")
    from runner import warm
    files = {"pkg/__init__.py": "", "data/table.txt": "a\n"}
    root = write_tree(tmp_path / "proj", {**files, "pkg/f.py": SUB_ESCAPE.replace("return n", "return n + 0")})
    c = make_cfg(root, behavioral_diff={"os_sandbox": "off", "cwd": "data", "warm": is_warm})
    base = write_tree(c.state_dir / "baseline", {**files, "pkg/f.py": SUB_ESCAPE})
    ts = diff.changed_targets("pkg/f.py", SUB_ESCAPE.lstrip("\n"), (root / "pkg/f.py").read_text())
    seen = {}
    real = scratch.release

    def keep(cwd):
        if cwd is not None and cwd.owned:
            seen[cwd.tree] = (cwd.path, sorted(os.listdir(cwd.folder)))
        real(cwd)
    scratch.release = keep
    try:
        r = diff.run(c, str(base), str(root), ts, harvest={})[0]
        off = make_cfg(root, behavioral_diff={"os_sandbox": "off", "cwd": "data", "cwd_mode": "off", "warm": is_warm})
        want = diff.run(off, str(base), str(root), ts, harvest={})[0]
    finally:
        scratch.release = real
        if is_warm:
            py = cfgmod.target_python(c)
            warm.stop_all(c, py, [(str(base), "baseline"), (str(root), "work")])
    assert seen[str(root)][0].endswith(os.sep + "data")
    assert "x.txt" not in seen[str(root)][1]
    assert r.status == diff.UNTESTED and "writ" in r.reason
    assert (r.status, r.reason) == (want.status, want.reason)
    assert not (root / "x.txt").exists()
