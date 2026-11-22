"""Sprint 7.4 Part B: the baseline's working folder is built once per session and reused across checks; only the
working tree is cloned per check. Rebuilt when a stale key changes, when its fingerprint changes, or when it is gone;
built under a flock; a replaced folder is retired and deleted only at the next SessionStart."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from guardlib import ledger
from runner import scratch

from conftest import PLUGIN, write_tree


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(scratch, "_REG", {})
    monkeypatch.setattr(scratch, "_LAST", {})
    yield


FILES = {"pkg/__init__.py": "", "pkg/m.py": "X = 1\n", "data/table.txt": "a,b\n"}


def session(tmp_path, make_cfg, commit="c1", **bd):
    """A project whose session baseline (state_dir/baseline) is built, as after SessionStart."""
    root = write_tree(tmp_path / "proj", FILES)
    cfg = make_cfg(root, behavioral_diff={"os_sandbox": "off", **bd})
    base = write_tree(cfg.state_dir / "baseline", FILES)
    with ledger.locked(cfg.state_dir) as data:
        data.update(baseline_ok=True, baseline_commit=commit)
    return root, cfg, base


def builds(monkeypatch) -> list[str]:
    """Real paths of the trees prepare_cwd was asked to fill (a build, never a reuse)."""
    made: list[str] = []
    real = scratch.prepare_cwd
    monkeypatch.setattr(scratch, "prepare_cwd", lambda *a: made.append(os.path.realpath(a[1])) or real(*a))
    return made


def check(cfg, base, root) -> tuple[scratch.Cwd, scratch.Cwd]:
    """One post-edit check: (baseline folder, working-tree folder), both asked for inside one for_trees block."""
    with scratch.for_trees(cfg, str(base), str(root), session_cache=True):
        with scratch.current(cfg, str(base)) as b, scratch.current(cfg, str(root)) as w:
            assert os.path.isdir(b.path) and os.path.isdir(w.path)
    return b, w


def manifest(cfg) -> dict:
    return json.loads((cfg.state_dir / "cwd_cache.json").read_text())


# ---- reuse ----

def test_second_check_reuses_the_baseline_folder_and_clones_only_the_working_tree(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    made = builds(monkeypatch)
    b1, w1 = check(cfg, base, root)
    assert b1.strategy in scratch.CACHE_STRATEGIES and not b1.owned and not b1.reused
    assert b1.label.endswith("; built for this session, kept for later checks")
    assert os.path.isdir(b1.folder) and not os.path.exists(w1.folder)  # the work folder is per check
    assert os.path.basename(b1.folder).startswith(scratch.CACHE_PREFIX)
    b2, w2 = check(cfg, base, root)
    assert b2.folder == b1.folder and b2.reused and not b2.owned
    assert "reused from an earlier check this session" in b2.label and b2.label.startswith(b2.strategy)
    assert "reused from an earlier check this session" in scratch.describe()  # the Isolation line says so
    assert made == [os.path.realpath(base), os.path.realpath(root), os.path.realpath(root)]  # no second baseline build
    assert (Path(b2.path) / "data/table.txt").read_text() == "a,b\n"
    m = manifest(cfg)
    assert m["path"] == b1.folder and m["retired"] == [] and m["fingerprint"] == scratch.fingerprint(b1.folder)


def test_cwd_subfolder_is_kept_on_reuse(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg, cwd="pkg")
    b1, _ = check(cfg, base, root)
    b2, _ = check(cfg, base, root)
    assert b2.reused and b1.path == b2.path == os.path.join(b1.folder, "pkg")


# ---- staleness keys ----

def _bump_dev(monkeypatch, which):
    real = scratch._dev

    def dev(path):
        if os.path.realpath(path) == os.path.realpath(which()):
            return real(path) + 1000
        return real(path)
    monkeypatch.setattr(scratch, "_dev", dev)


STALE = {
    "baseline_commit": lambda cfg, base, mp: _commit(cfg),
    "generation": lambda cfg, base, mp: os.utime(base, ns=(time.time_ns(), time.time_ns() + 10**9)),
    "dev_tree": lambda cfg, base, mp: _bump_dev(mp, lambda: base),
    "dev_tmp": lambda cfg, base, mp: _bump_dev(mp, tempfile.gettempdir),
    "cwd_mode": lambda cfg, base, mp: cfg.raw["behavioral_diff"].update(cwd_mode="copy"),
    "cwd": lambda cfg, base, mp: cfg.raw["behavioral_diff"].update(cwd="data"),
    "list_version": lambda cfg, base, mp: mp.setattr(scratch, "LIST_VERSION", scratch.LIST_VERSION + 1),
}


def _commit(cfg):
    with ledger.locked(cfg.state_dir) as data:
        data["baseline_commit"] = "c2"


@pytest.mark.parametrize("key", sorted(STALE))
def test_each_stale_key_rebuilds_and_retires_the_old_folder(tmp_path, make_cfg, monkeypatch, fresh, key):
    root, cfg, base = session(tmp_path, make_cfg)
    b1, _ = check(cfg, base, root)
    before = scratch.stale_key(cfg, str(base))
    STALE[key](cfg, base, monkeypatch)
    after = scratch.stale_key(cfg, str(base))
    assert before[key] != after[key]
    made = builds(monkeypatch)
    b2, _ = check(cfg, base, root)
    assert made[0] == os.path.realpath(base) and not b2.reused and b2.folder != b1.folder
    assert os.path.isdir(b1.folder)  # retired, never deleted under another hook
    assert manifest(cfg)["retired"] == [b1.folder] and manifest(cfg)["stale_key"] == after


def test_realpath_of_the_baseline_is_a_stale_key(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, _ = session(tmp_path, make_cfg)
    shutil.rmtree(cfg.state_dir / "baseline")
    one, two = write_tree(tmp_path / "b1", FILES), write_tree(tmp_path / "b2", FILES)
    link = cfg.state_dir / "baseline"
    link.symlink_to(one)
    b1, _ = check(cfg, link, root)
    before = scratch.stale_key(cfg, str(link))
    link.unlink()
    link.symlink_to(two)
    assert scratch.stale_key(cfg, str(link))["tree"] != before["tree"]
    b2, _ = check(cfg, link, root)
    assert not b2.reused and b2.folder != b1.folder


def test_folder_gone_rebuilds(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    b1, _ = check(cfg, base, root)
    shutil.rmtree(b1.folder)
    b2, _ = check(cfg, base, root)
    assert not b2.reused and os.path.isdir(b2.folder) and b2.folder != b1.folder


# ---- fingerprint ----

def _keep_mtime(path: Path, change):
    st = os.lstat(path)
    change()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


FINGER = {
    # an entry added; the folder's own mtime put back, so only the count tells
    "count": (0, lambda f: _keep_mtime(f / "data", lambda: (f / "data/new.txt").write_text("")) or
              os.utime(f / "data/new.txt", ns=(0, 0))),
    # a file grown; its mtime put back, so only the size tells
    "size": (1, lambda f: _keep_mtime(f / "data/table.txt", lambda: (f / "data/table.txt").write_text("a,b,c\n"))),
    # same size, newer mtime
    "mtime": (2, lambda f: os.utime(f / "pkg/m.py", ns=(time.time_ns(), time.time_ns() + 5 * 10**9))),
}


@pytest.mark.parametrize("what", sorted(FINGER))
def test_a_changed_fingerprint_rebuilds(tmp_path, make_cfg, monkeypatch, fresh, what):
    root, cfg, base = session(tmp_path, make_cfg)
    b1, _ = check(cfg, base, root)
    index, change = FINGER[what]
    before = scratch.fingerprint(b1.folder)
    change(Path(b1.folder))
    after = scratch.fingerprint(b1.folder)
    assert [i for i in range(3) if before[i] != after[i]] == [index]
    b2, _ = check(cfg, base, root)
    assert not b2.reused and b2.folder != b1.folder and manifest(cfg)["retired"] == [b1.folder]
    assert (Path(b2.path) / "data/table.txt").read_text() == "a,b\n"  # a clean copy of the baseline again


def test_fingerprint_of_a_missing_folder_is_none(tmp_path):
    assert scratch.fingerprint(str(tmp_path / "gone")) is None


# ---- where the cache applies ----

def test_no_session_baseline_means_a_folder_per_check(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    with ledger.locked(cfg.state_dir) as data:
        data["baseline_ok"] = False
    b1, _ = check(cfg, base, root)
    assert b1.owned and not os.path.exists(b1.folder)
    other = write_tree(tmp_path / "elsewhere", FILES)  # a tree that is not the session baseline (ci.run's own base)
    with ledger.locked(cfg.state_dir) as data:
        data["baseline_ok"] = True
    b2, _ = check(cfg, other, root)
    assert b2.owned and not os.path.exists(b2.folder)
    assert not (cfg.state_dir / "cwd_cache.json").exists()


def test_without_session_cache_flag_it_is_part_a(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    with scratch.for_trees(cfg, str(base), str(root)):
        with scratch.current(cfg, str(base)) as b:
            pass
    assert b.owned and not os.path.exists(b.folder) and not (cfg.state_dir / "cwd_cache.json").exists()


@pytest.mark.parametrize("mode", ["empty", "off"])
def test_empty_and_off_are_not_cached(tmp_path, make_cfg, monkeypatch, fresh, mode):
    root, cfg, base = session(tmp_path, make_cfg, cwd_mode=mode)
    b, _ = check(cfg, base, root)
    assert b.strategy == mode and "built for this session" not in b.label
    if mode == "empty":
        assert b.owned and not os.path.exists(b.folder)
    assert "path" not in scratch._read_manifest(cfg)
    assert scratch._read_manifest(cfg).get("retired", []) == []  # the unused cache folder was deleted at once


def test_any_cache_error_falls_back_to_a_folder_per_check(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg)

    def broken(cfg):
        raise OSError("no lock")
    monkeypatch.setattr(scratch, "_cache_lock", broken)
    b, _ = check(cfg, base, root)
    assert b.owned and b.strategy in scratch.CACHE_STRATEGIES and not os.path.exists(b.folder)


def test_stop_audit_uses_the_cache_and_ci_run_does_not(tmp_path, make_cfg, monkeypatch, fresh):
    from guardlib import audit, ci
    root, cfg, base = session(tmp_path, make_cfg)
    seen = []
    real = scratch.for_trees

    def spy(*a, **kw):
        seen.append(kw.get("session_cache", False))
        return real(*a, **kw)
    monkeypatch.setattr(scratch, "for_trees", spy)
    audit._behavior_findings(cfg, ledger.load(cfg.state_dir), [])
    assert seen == [True]
    import inspect
    assert "session_cache" not in inspect.getsource(ci.run)  # a per-run base worktree: Part A


def test_sweep_leaves_the_session_folder(tmp_path, make_cfg, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    b, _ = check(cfg, base, root)
    scratch._sweep()
    assert os.path.isdir(b.folder)


# ---- overlapping hooks ----

HOOK = textwrap.dedent("""
    import copy, json, os, sys, time
    sys.path.insert(0, {plugin!r})
    from guardlib import config as cfgmod
    from runner import scratch
    raw = copy.deepcopy(cfgmod.DEFAULTS)
    raw["python"] = sys.executable
    raw["behavioral_diff"]["os_sandbox"] = "off"
    cfg = cfgmod.Config(root={root!r}, raw=raw)
    real = scratch._materialise
    def slow(*a, **kw):
        time.sleep(0.6)  # long enough for the other hook to arrive while this one builds
        return real(*a, **kw)
    scratch._materialise = slow
    while time.time() < {start}:
        time.sleep(0.005)
    cwd = scratch.session_baseline(cfg, {base!r}, False)
    print(json.dumps({{"folder": cwd.folder, "reused": cwd.reused, "owned": cwd.owned}}))
""")


def test_two_hooks_at_once_build_one_folder(tmp_path, make_cfg, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    start = time.time() + 1.0
    script = HOOK.format(plugin=str(PLUGIN), root=str(root), base=str(base), start=start)
    procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
    assert outs[0]["folder"] == outs[1]["folder"] and sorted(o["reused"] for o in outs) == [False, True]
    assert not any(o["owned"] for o in outs)
    m = manifest(cfg)
    assert m["path"] == outs[0]["folder"] and m["retired"] == []
    assert [p for p in os.listdir(tempfile.gettempdir()) if p.startswith(scratch.CACHE_PREFIX)
            and os.path.join(tempfile.gettempdir(), p) == m["path"]]


def test_a_replacement_retires_the_folder_another_hook_is_using(tmp_path, make_cfg, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    with scratch.for_trees(cfg, str(base), str(root), session_cache=True):
        with scratch.current(cfg, str(base)) as in_use:  # hook A is running in this folder ...
            _commit(cfg)
            other = scratch.session_baseline(cfg, str(base), False)  # ... when hook B finds the cache stale
            assert other.folder != in_use.folder
            assert os.path.isfile(os.path.join(in_use.path, "pkg/m.py"))  # A's folder is untouched
    assert os.path.isdir(in_use.folder)  # still there after A's block: only SessionStart deletes it
    assert manifest(cfg)["path"] == other.folder and manifest(cfg)["retired"] == [in_use.folder]


def test_a_build_killed_midway_is_tracked_for_cleanup(tmp_path, make_cfg, monkeypatch, fresh):
    root, cfg, base = session(tmp_path, make_cfg)

    def killed(*a, **kw):
        raise KeyboardInterrupt  # not an Exception: like the hook being killed mid-build
    monkeypatch.setattr(scratch, "_materialise", killed)
    with pytest.raises(KeyboardInterrupt):
        scratch.session_baseline(cfg, str(base), False)
    (left,) = manifest(cfg)["retired"]
    assert os.path.isdir(left) and os.path.basename(left).startswith(scratch.CACHE_PREFIX)
    scratch.drop_session_cache(cfg)
    assert not os.path.exists(left)


# ---- cleanup at SessionStart ----

def test_drop_session_cache_deletes_the_folder_and_every_retired_one(tmp_path, make_cfg, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    b1, _ = check(cfg, base, root)
    _commit(cfg)
    b2, _ = check(cfg, base, root)
    stray = tmp_path / "not-ours"
    stray.mkdir()
    m = manifest(cfg)
    m["retired"].append(str(stray))  # a manifest entry that is not a cache folder is never deleted
    (cfg.state_dir / "cwd_cache.json").write_text(json.dumps(m))
    scratch.drop_session_cache(cfg)
    assert not os.path.exists(b1.folder) and not os.path.exists(b2.folder) and stray.is_dir()
    assert not (cfg.state_dir / "cwd_cache.json").exists()
    scratch.drop_session_cache(cfg)  # nothing cached: nothing to do, no error


def test_only_cache_folders_directly_in_the_temp_folder_are_deleted_or_reused(tmp_path, make_cfg, fresh):
    root, cfg, base = session(tmp_path, make_cfg)
    b1, _ = check(cfg, base, root)
    victim = tmp_path / "lg-cwd-base-victim"  # the right prefix, the wrong parent folder
    write_tree(victim, {"keep.txt": "x"})
    target = Path(tempfile.mkdtemp(prefix=scratch.CACHE_PREFIX))  # a symlink to a real cache-like folder
    link = Path(tempfile.gettempdir()) / f"{scratch.CACHE_PREFIX}link-{os.getpid()}"
    link.symlink_to(target)
    try:
        assert not scratch._is_cache_folder(cfg, str(victim)) and not scratch._is_cache_folder(cfg, str(link))
        assert scratch._is_cache_folder(cfg, b1.folder)
        m = manifest(cfg)
        m["retired"] += [m["path"], str(victim), str(link)]
        (cfg.state_dir / "cwd_cache.json").write_text(json.dumps({**m, "path": str(victim)}))
        b2, _ = check(cfg, base, root)  # a manifest naming a folder outside the temp folder is never reused
        assert not b2.reused and b2.folder != str(victim)
        scratch.drop_session_cache(cfg)
        assert (victim / "keep.txt").read_text() == "x" and link.is_symlink() and target.is_dir()
        assert not os.path.exists(b1.folder) and not os.path.exists(b2.folder)
    finally:
        link.unlink()
        shutil.rmtree(target, ignore_errors=True)


def _git_project(root: Path) -> None:
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init")


def _session_start():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lg_session_start_hook_b", PLUGIN / "scripts" / "session_start.py")
    sys.path.insert(0, str(PLUGIN / "scripts"))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(PLUGIN / "scripts"))
    return module


def test_session_start_drops_the_last_sessions_folders(tmp_path, make_cfg, monkeypatch, fresh):
    root = write_tree(tmp_path / "proj", {**FILES, "pkg/calc.py": "def f(n):\n    return n + 1\n"})
    cfg = make_cfg(root, behavioral_diff={"os_sandbox": "off"})
    _git_project(root)
    hook = _session_start()
    monkeypatch.setattr(hook, "snapshot_baseline_routes", lambda cfg: None)
    import indexer.__main__ as indexer_main
    monkeypatch.setattr(indexer_main, "refresh", lambda cfg: None)
    hook.start_session(cfg, {"session_id": "s1", "source": "startup"})
    base = cfg.state_dir / "baseline"
    b1, _ = check(cfg, base, root)
    assert b1.strategy in scratch.CACHE_STRATEGIES and (Path(b1.path) / "pkg/calc.py").exists()
    hook.start_session(cfg, {"session_id": "s1", "source": "resume"})  # the same session resumed: kept
    assert check(cfg, base, root)[0].reused
    hook.start_session(cfg, {"session_id": "s2", "source": "startup"})
    assert not os.path.exists(b1.folder) and not (cfg.state_dir / "cwd_cache.json").exists()
    b3, _ = check(cfg, base, root)
    assert not b3.reused and os.path.isdir(b3.folder)


def test_a_folder_built_while_the_baseline_is_half_rebuilt_is_not_reused(tmp_path, make_cfg, monkeypatch, fresh):
    """An old-session hook runs between `git worktree add` and the copy of the dirty files: same commit, final
    generation, old ledger still baseline_ok. The second drop after the ledger reset must delete what it built."""
    root = write_tree(tmp_path / "proj", {**FILES, "pkg/calc.py": "def f(n):\n    return n + 1\n"})
    cfg = make_cfg(root, behavioral_diff={"os_sandbox": "off"})
    _git_project(root)
    (root / "pkg/calc.py").write_text("def f(n):\n    return n + 2\n")  # a dirty file the baseline copies in
    hook = _session_start()
    monkeypatch.setattr(hook, "snapshot_baseline_routes", lambda cfg: None)
    import indexer.__main__ as indexer_main
    monkeypatch.setattr(indexer_main, "refresh", lambda cfg: None)
    hook.start_session(cfg, {"session_id": "s1", "source": "startup"})
    base = cfg.state_dir / "baseline"
    real_copy, mid = shutil.copy2, []

    def copy_after_an_old_hook(src, dst, *a, **kw):
        if not mid and str(dst).startswith(str(base)):
            got = scratch.session_baseline(cfg, str(base), False)
            mid.append((got, (Path(got.path) / "pkg/calc.py").read_text()))  # the dirty file is not copied yet
        return real_copy(src, dst, *a, **kw)
    monkeypatch.setattr(shutil, "copy2", copy_after_an_old_hook)
    hook.start_session(cfg, {"session_id": "s2", "source": "startup"})
    monkeypatch.setattr(shutil, "copy2", real_copy)
    ((early, text),) = mid
    assert early.strategy in scratch.CACHE_STRATEGIES and "n + 1" in text and not early.owned
    assert not os.path.exists(early.folder)  # deleted by the drop after the ledger reset
    b, _ = check(cfg, base, root)
    assert not b.reused and b.folder != early.folder
    assert "n + 2" in (Path(b.path) / "pkg/calc.py").read_text()  # the finished baseline, dirty file included
