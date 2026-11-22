"""Throwaway working folders for the processes that run project code (roadmap A2, Sprint 7.4 phase 1).

Every process that runs project code for a check (cold worker, warm-server child, the affected-tests pytest run) gets
a working folder in the system temp folder instead of the real tree, so a relative write that gets past the
in-process guards lands in a folder that is deleted after the check:

- clone: a copy-on-write clone of the tree, file by file (clonefile(2) on macOS, the FICLONE ioctl on Linux), when a
  probe clone works and the tree has at most MAX_CLONE_FILES files;
- copy: a plain copy when the tree is small (MAX_COPY_FILES files and MAX_COPY_BYTES bytes);
- off: the real tree, read-only under the OS sandbox, when the tree is too large and an OS sandbox is active;
- empty: an empty folder when the tree is too large and there is no OS sandbox (relative reads of project files fail).

Phase 1 is safety only. Imports still come from the real tree (absolute sys.path entries), and the worker adds the
working folder to its protected paths, so writes there during a call are refused exactly as writes into the tree are.
A working folder does nothing for absolute-path writes: the in-process guards and the OS sandbox handle those.

Listing rule (one rule for both trees): walk the tree itself, not `git ls-files`. Pruned: PRUNE_DIRS, `*.egg-info`,
PRUNE_TOP (build, dist, _build) at the tree's top level only,
any folder holding `pyvenv.cfg` (a virtualenv) or `conda-meta` (a conda environment), any sub-folder holding a `.git`
entry (nested worktree or submodule), and any sub-folder on another device (a mount point). Folder symlinks are kept
as symlinks, never followed; sockets and fifos are skipped. So the working tree's clone has its tracked, untracked
and git-ignored files (`.env`, local settings, data files), while the baseline never had ignored files (it is built
from HEAD plus dirty files): relative reads of those fail on the old side, as they did before 7.4.

Known limits: OLD-A and OLD-B share one working folder per tree, so a write that gets past the in-process layers in
OLD-A can be seen by OLD-B (weakens the old-vs-old noise filter for that case, never hides an old-vs-new difference);
lg_codec.canon does not mask the working-folder path; the warm server itself keeps the tree as its cwd (a module that
stores os.getcwd() at import reads the tree on a warm baseline and the clone on a cold one, which gives untested
through old-vs-old disagreement or a difference, never a hidden one).

Part B (session cache of the baseline's folder): the baseline does not change during a session, so the post-edit
hook and the Stop audit reuse one clone or copy of it across checks (`session_baseline`); only the working tree is
cloned per check. The folder is `lg-cwd-base-*` in the temp folder (never swept by pid), described by
`<state>/cwd_cache.json` {path, strategy, label, files, nbytes, stale_key, fingerprint, retired}. It is rebuilt when
the stale key changes (baseline commit, baseline generation, real path and device of the baseline, device of the temp
folder, cwd_mode, cwd sub-folder, LIST_VERSION) or the folder is gone, and when its fingerprint (entry count, total
size, newest mtime) no longer matches, which catches a write that got past the in-process layers. Builds and
replacements happen under an exclusive flock on `<state>/cwd_cache.lock`; a replaced folder goes into `retired` and is
deleted only by `drop_session_cache` at SessionStart, never while another hook may still use it. ci.run (so
tools/validate.py) does not use it: its base worktree is rebuilt per run and one folder per tree already serves the
whole run. Any cache error falls back to a per-check folder (Part A). Limit: two overlapping post-edit hooks can
use the cached folder at the same time, so a relative write by one check's baseline affected-tests pytest can be seen
by the other's baseline pytest (diff workers cannot write there during calls; the fingerprint catches the write only
at the start of the next check). Only lg-cwd-base-* folders directly in the temp folder, not symlinks, are reused or
deleted.
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_CLONE_FILES = 20_000
MAX_COPY_FILES = 2_000
MAX_COPY_BYTES = 50 * 2**20
PRUNE_DIRS = {".git", ".hg", ".svn", ".legacyguard", "node_modules", "__pycache__", ".tox", ".nox", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".hypothesis", ".eggs", "htmlcov"}
PRUNE_TOP = {"build", "dist", "_build"}  # build output only at the tree's top level; pkg/build/ is source
PRUNE_SUFFIXES = (".egg-info",)
PRUNE_MARKERS = ("pyvenv.cfg", "conda-meta")
LIST_VERSION = 4  # bump when the listing rule changes
PREFIX = "lg-cwd-"
CACHE_PREFIX = "lg-cwd-base-"  # the session's baseline folder (Part B): not pid-named, so _sweep leaves it
CACHE_STRATEGIES = ("clone", "copy")
MODES = ("auto", "clone", "copy", "empty", "off")
FICLONE_GENERIC = 0x40049409  # _IOW(0x94, 9, int) on the generic ioctl encoding (x86_64, aarch64)
CLONE_NOFOLLOW = 0x0001


@dataclass
class Cwd:
    path: str              # the working folder handed to the process (folder or folder/subdir)
    strategy: str          # clone | copy | empty | off
    label: str
    seconds: float = 0.0
    files: int = 0
    nbytes: int = 0
    owned: bool = False    # deleted on release
    tree: str = ""
    folder: str = ""       # the folder that release() deletes
    reused: bool = False   # the session's cached baseline folder, built by an earlier check (Part B)


class _TooBig(Exception):
    pass


def _real(p: str) -> str:
    return os.path.realpath(p)


def _inside(path: str, parent: str) -> bool:
    path, parent = _real(path), _real(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def _size_text(n: int) -> str:
    if n >= 2**20:
        return f"{n / 2**20:.1f} MB"
    return f"{max(n, 0) / 1024:.0f} KB"


# ---- listing ----

def _dev(path: str) -> int:
    """The device a path is on (lstat), the seam tests use to fake a mount point."""
    return os.lstat(path).st_dev


def _pruned(entry: os.DirEntry, tree_dev: int, top: bool = False) -> bool:
    name = entry.name
    if name in PRUNE_DIRS or name.endswith(PRUNE_SUFFIXES) or (top and name in PRUNE_TOP):
        return True
    try:
        if _dev(entry.path) != tree_dev:
            return True  # a mount point: another device is never walked into
        return any(os.path.lexists(os.path.join(entry.path, m)) for m in (*PRUNE_MARKERS, ".git"))
    except OSError:
        return True


def _list_tree(tree: str, limit: int | None) -> tuple[list[tuple[str, str, int]], int, int, bool]:
    """([(relative path, kind, size)], files, bytes, over): kind is "dir", "file" or "link". Stops once more than
    limit files were seen (over=True). See the module docstring for the rule."""
    out: list[tuple[str, str, int]] = []
    files = nbytes = 0
    try:
        tree_dev = _dev(os.path.realpath(tree))
    except OSError:
        return out, 0, 0, False
    stack = [""]
    while stack:
        rel = stack.pop()
        try:
            with os.scandir(os.path.join(tree, rel) if rel else tree) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                if _pruned(e, tree_dev, top=not rel):
                    continue
                out.append((r, "dir", 0))
                stack.append(r)
            elif e.name == ".git":
                continue  # a worktree's gitdir pointer file: the clone must not lead git back into the real repo
            elif stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
                files += 1
                size = 0 if stat.S_ISLNK(st.st_mode) else st.st_size
                nbytes += size
                out.append((r, "link" if stat.S_ISLNK(st.st_mode) else "file", size))
                if limit is not None and files > limit:
                    return out, files, nbytes, True
    return out, files, nbytes, False


# ---- one file ----

_LIBC = {}


def _libc():
    if "c" not in _LIBC:
        _LIBC["c"] = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    return _LIBC["c"]


def _ficlone_request() -> int | None:
    """The FICLONE ioctl number, or None on an architecture whose _IOW encoding differs (powerpc, mips, sparc, alpha)."""
    import fcntl
    got = getattr(fcntl, "FICLONE", None)
    if got is not None:
        return got
    if platform.machine().lower() in ("x86_64", "amd64", "aarch64", "arm64"):
        return FICLONE_GENERIC
    return None


def _clone_one(src: str, dst: str) -> None:
    """Copy-on-write clone of one file or symlink. Raises OSError when the file system cannot (EXDEV, ENOTSUP, ...).
    Never `cp -c`, whose fallback to a real copy is silent."""
    if sys.platform == "darwin":
        lib = _libc()
        if lib.clonefile(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), src)
        return
    if sys.platform.startswith("linux"):
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
            return
        req = _ficlone_request()
        if req is None:
            raise OSError(errno.EOPNOTSUPP, "no FICLONE ioctl number for this architecture", src)
        import fcntl
        with open(src, "rb") as fs, open(dst, "wb") as fd:
            try:
                fcntl.ioctl(fd.fileno(), req, fs.fileno())
            except OSError:
                fd.close()
                with contextlib.suppress(OSError):
                    os.unlink(dst)
                raise
        shutil.copystat(src, dst, follow_symlinks=False)
        return
    raise OSError(errno.ENOTSUP, "no copy-on-write clone on this platform", src)


def _copy_one(src: str, dst: str) -> None:
    shutil.copy2(src, dst, follow_symlinks=False)


def _clone_name() -> str:
    return "clonefile" if sys.platform == "darwin" else "FICLONE"


def _errno_reason(exc: OSError) -> str:
    if exc.errno == errno.EXDEV:
        return "the temp folder is on another file system"
    if exc.errno in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
        return "no copy-on-write on this file system"
    return f"clone failed ({errno.errorcode.get(exc.errno or 0, exc.errno)})"


# ---- probe and its cache ----

def _probe(cfg, tree: str, sample: str | None) -> tuple[bool, str]:
    """Clone one listed file into a fresh temp folder: (ok, reason). Any OSError means no clone."""
    if sample is None:
        return False, "nothing to clone"
    tmp = tempfile.mkdtemp(prefix="lg-probe-")
    try:
        _clone_one(os.path.join(tree, sample), os.path.join(tmp, "probe"))
        return True, ""
    except OSError as exc:
        return False, _errno_reason(exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cache_key(cfg, tree: str, mode: str) -> str:
    try:
        dev_tree = _dev(_real(tree))
        dev_tmp = _dev(_real(tempfile.gettempdir()))
    except OSError:
        dev_tree = dev_tmp = -1
    return "|".join(map(str, (_real(tree), dev_tree, dev_tmp, sys.platform, mode, LIST_VERSION)))


def _cached_probe(cfg, tree: str, mode: str, sample: str | None) -> tuple[bool, str]:
    """Only the probe result is cached (cfg.state_dir/cwd_strategy.json); counts and sizes are recomputed every check."""
    path = Path(cfg.state_dir) / "cwd_strategy.json"
    key = _cache_key(cfg, tree, mode)
    try:
        cache = json.loads(path.read_text())
        hit = cache.get(key)
        if isinstance(hit, dict) and "ok" in hit:
            return bool(hit["ok"]), str(hit.get("reason") or "")
    except (OSError, ValueError, AttributeError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}
    ok, reason = _probe(cfg, tree, sample)
    if sample is not None:
        cache[key] = {"ok": ok, "reason": reason}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".cwd_strategy.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(cache))
            os.replace(tmp, path)
        except OSError:
            pass
    return ok, reason


# ---- folders ----

def _location(cfg, tree: str) -> str | None:
    """A fresh folder in the temp folder, or None when the temp folder is inside the project or the tree (the OS
    sandbox keeps those read-only, so a working folder there would be read-only too)."""
    folder = tempfile.mkdtemp(prefix=f"{PREFIX}{_b36(os.getpid())}-", dir=tempfile.gettempdir())
    if _inside(folder, str(cfg.root)) or _inside(folder, tree):
        shutil.rmtree(folder, ignore_errors=True)
        return None
    return folder


def _b36(n: int) -> str:
    """The hook pid in the folder name, in base 36: lg_codec.canon masks decimal pids (as `<parent pid>` in a cold
    worker), and a base-36 name keeps the folder path the same text the process sees."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def _sweep() -> None:
    """Delete lg-cwd-<pid>-* folders left by hook processes that are gone (killed mid-check). A pid is gone when
    os.kill(pid, 0) raises ProcessLookupError; a PermissionError (the pid belongs to another user) counts as alive.
    The pid is written in base 36. Known limit: containers that share /tmp but have separate pid namespaces can
    misjudge each other's pids (a live hook in another namespace can look dead, or a dead one alive)."""
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        if not name.startswith(PREFIX) or name.startswith(CACHE_PREFIX):
            continue  # lg-cwd-base-* belongs to the session cache ("base" would also parse as a base-36 pid)
        pid_text = name[len(PREFIX):].split("-", 1)[0]
        try:
            pid = int(pid_text, 36)
        except ValueError:
            continue
        if pid == os.getpid() or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
        except (PermissionError, OSError, OverflowError):
            pass


def _materialise(tree: str, dest: str, entries, use_clone: bool) -> tuple[int, int, int, str]:
    """Fill dest from the listing: (cloned, copied, copied bytes, first clone failure reason). The per-file copy
    fallback after a failed clone is capped: it raises _TooBig once the copied bytes pass MAX_COPY_BYTES."""
    cloned = copied = copied_bytes = 0
    first_fail = ""
    for rel, kind, size in entries:
        src, dst = os.path.join(tree, rel), os.path.join(dest, rel)
        if kind == "dir":
            os.makedirs(dst, exist_ok=True)
            continue
        if use_clone:
            try:
                _clone_one(src, dst)
                cloned += 1
                continue
            except FileNotFoundError:
                continue  # removed since the listing
            except OSError as exc:
                first_fail = first_fail or _errno_reason(exc)
        if copied_bytes + size > MAX_COPY_BYTES:
            raise _TooBig(f"copying the files the clone could not take would pass {_size_text(MAX_COPY_BYTES)} ({first_fail or 'clone failed'})")
        try:
            _copy_one(src, dst)
        except FileNotFoundError:
            continue
        copied += 1
        copied_bytes += size
    return cloned, copied, copied_bytes, first_fail


def _subdir(base: str, sub: str | None, create: bool) -> tuple[str, str]:
    """(path, note) for the configured `behavioral_diff.cwd` sub-folder."""
    if not sub:
        return base, ""
    target = os.path.normpath(os.path.join(base, sub))
    if not _inside(target, base):
        return base, f"; cwd '{sub}' is outside the tree, used the folder root"
    if create:
        os.makedirs(target, exist_ok=True)
    if os.path.isdir(target):
        return target, ""
    return base, f"; cwd '{sub}' not found, used the folder root"


def _off(tree: str, sub: str | None, label: str, seconds: float = 0.0) -> Cwd:
    path, note = _subdir(tree, sub, create=False)
    return Cwd(path=path, strategy="off", label=label + note, seconds=seconds, tree=tree)


def _fallback(cfg, tree: str, sandboxed: bool, why: str, sub: str | None, started: float, folder: str | None = None) -> Cwd:
    """Too large to clone or copy: the real tree read-only under the OS sandbox (off), else an empty folder."""
    if sandboxed:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        return _off(tree, sub, f"off: {why}; the real tree, read-only under the OS sandbox", time.monotonic() - started)
    if folder:
        shutil.rmtree(folder, ignore_errors=True)
    return _empty(cfg, tree, sub, f"empty: {why}; no OS sandbox, so relative reads of project files fail in this run", started)


def _empty(cfg, tree: str, sub: str | None, label: str, started: float) -> Cwd:
    folder = _location(cfg, tree)
    if folder is None:
        return _off(tree, sub, "off: the temp folder is inside the project", time.monotonic() - started)
    path, note = _subdir(folder, sub, create=True)
    return Cwd(path=path, strategy="empty", label=label + note, seconds=time.monotonic() - started, owned=True,
               tree=tree, folder=folder)


def _build(cfg, tree: str, sub: str | None, entries, files: int, nbytes: int, use_clone: bool, why_not_clone: str,
           sandboxed: bool, started: float, folder: str | None = None) -> Cwd:
    folder = folder or _location(cfg, tree)
    if folder is None:
        return _off(tree, sub, "off: the temp folder is inside the project", time.monotonic() - started)
    try:
        cloned, copied, copied_bytes, fail = _materialise(tree, folder, entries, use_clone)
    except _TooBig as exc:
        return _fallback(cfg, tree, sandboxed, f"{files:,} files, {_size_text(nbytes)}: {exc}", sub, started, folder)
    except OSError as exc:
        return _fallback(cfg, tree, sandboxed, f"copy failed ({errno.errorcode.get(exc.errno or 0, exc.errno)})", sub, started, folder)
    seconds = time.monotonic() - started
    if use_clone:
        label = f"clone ({_clone_name()}) of {files:,} files, {_size_text(nbytes)}, in {seconds:.2f} s"
        if copied:
            label += f"; {copied:,} copied ({_size_text(copied_bytes)}) after the clone failed: {fail}"
        strategy = "clone"
    else:
        label = f"copy of {files:,} files, {_size_text(nbytes)}, in {seconds:.2f} s"
        if why_not_clone:
            label += f" (no clone: {why_not_clone})"
        strategy = "copy"
    path, note = _subdir(folder, sub, create=False)
    return Cwd(path=path, strategy=strategy, label=label + note, seconds=seconds, files=files, nbytes=nbytes, owned=True,
               tree=tree, folder=folder)


def prepare_cwd(cfg, tree: str, sandboxed: bool, folder: str | None = None) -> Cwd:
    """The working folder for processes running project code from this tree, by `behavioral_diff.cwd_mode`.
    Fails open: any error gives the tree itself (as before 7.4) with the reason in the label. `folder`: an empty
    folder to clone or copy into (the session cache's); the caller deletes it when the result is not in it."""
    started = time.monotonic()
    bd = cfg.behavioral_diff
    sub = bd.get("cwd") or None
    try:
        _sweep()
        mode = str(bd.get("cwd_mode") or "auto")
        note = ""
        if mode not in MODES:
            note, mode = f" (unknown cwd_mode '{mode}', used auto)", "auto"
        if mode == "off":
            return _off(tree, sub, "off (switched off in config)")
        if mode == "empty":
            return _empty(cfg, tree, sub, "empty (set in config)", started)
        limit = None if mode == "clone" else max(MAX_CLONE_FILES, MAX_COPY_FILES) if mode == "auto" else MAX_COPY_FILES
        entries, files, nbytes, over = _list_tree(tree, limit)
        sample = next((r for r, kind, _ in entries if kind == "file"), None)
        copy_fits = files <= MAX_COPY_FILES and nbytes <= MAX_COPY_BYTES and not over
        if mode == "copy":
            if copy_fits:
                cwd = _build(cfg, tree, sub, entries, files, nbytes, False, "", sandboxed, started, folder)
            else:
                cwd = _fallback(cfg, tree, sandboxed, _too_many(files, nbytes, over, MAX_COPY_FILES) + " to copy", sub, started)
            return _noted(cwd, note)
        ok, reason = _cached_probe(cfg, tree, mode, sample)
        if ok and not over and (mode == "clone" or files <= MAX_CLONE_FILES):
            return _noted(_build(cfg, tree, sub, entries, files, nbytes, True, "", sandboxed, started, folder), note)
        why = reason if not ok else f"more than {MAX_CLONE_FILES:,} files"
        if copy_fits:
            return _noted(_build(cfg, tree, sub, entries, files, nbytes, False, why, sandboxed, started, folder), note)
        what = _too_many(files, nbytes, over, limit or MAX_CLONE_FILES)  # limit is None only in clone mode, never over
        return _noted(_fallback(cfg, tree, sandboxed, f"{what} to clone or copy" + (f" (no clone: {reason})" if not ok and reason else ""),
                                sub, started), note)
    except Exception as exc:  # noqa: BLE001 - fail open: the tree, as before 7.4
        return _off(tree, sub, f"off: the working folder could not be prepared ({type(exc).__name__}: {exc})"[:300],
                    time.monotonic() - started)


def _too_many(files: int, nbytes: int, over: bool, limit: int) -> str:
    if over:
        return f"more than {limit:,} files"
    return f"{files:,} files, {_size_text(nbytes)}, too many"


def _noted(cwd: Cwd, note: str) -> Cwd:
    if note:
        cwd.label += note
    return cwd


def release(cwd: Cwd | None) -> None:
    if cwd is not None and cwd.owned and cwd.folder:
        shutil.rmtree(cwd.folder, ignore_errors=True)


# ---- one folder per tree per check ----

class _Entry:
    def __init__(self, cfg, tree: str, role: str, cached: bool = False):
        self.cfg, self.tree, self.role, self.cached = cfg, tree, role, cached
        self.lock = threading.Lock()
        self.cwd: Cwd | None = None
        self.depth = 0

    def get(self) -> Cwd:
        with self.lock:  # OLD-A, OLD-B and NEW ask at the same moment: prepare once
            if self.cwd is None:
                if self.cached:
                    self.cwd = session_baseline(self.cfg, self.tree, sandboxed(self.cfg))
                else:
                    self.cwd = prepare_cwd(self.cfg, self.tree, sandboxed(self.cfg))
                with _REG_LOCK:
                    _LAST[self.role] = self.cwd.label
            return self.cwd


_REG: dict[str, _Entry] = {}
_REG_LOCK = threading.Lock()
_LAST: dict[str, str] = {}


def sandboxed(cfg) -> bool:
    """Whether an OS sandbox will wrap this config's processes. Any error means not sandboxed (fails open: the
    too-large fallback is then an empty folder, never the real tree)."""
    try:
        from guardlib import config as cfgmod
        from runner import os_sandbox
        return bool(os_sandbox.active(cfgmod.target_python(cfg), str(cfg.behavioral_diff.get("os_sandbox", "auto"))))
    except Exception:  # noqa: BLE001
        return False


@contextlib.contextmanager
def for_trees(cfg, baseline_root: str, work_root: str, session_cache: bool = False):
    """One working folder per tree for everything inside the block. Folders are prepared on first use (a check that
    runs nothing clones nothing) and released when the outermost block for that tree ends. Re-entrant: an inner
    block (diff.run inside the post_edit hook's block) reuses the outer one's folders. session_cache (the post-edit
    hook and the Stop audit): the baseline's folder comes from the session cache (session_baseline) and outlives
    the block."""
    mine = []
    with _REG_LOCK:
        if not any(e.depth for e in _REG.values()):
            _LAST.clear()
        for role, root in (("baseline", baseline_root), ("work", work_root)):
            key = _real(root)
            entry = _REG.get(key)
            if entry is None:
                entry = _REG[key] = _Entry(cfg, root, role, cached=session_cache and role == "baseline")
            entry.depth += 1
            mine.append(key)
    try:
        yield
    finally:
        done = []
        with _REG_LOCK:
            for key in mine:
                entry = _REG.get(key)
                if entry is None:
                    continue
                entry.depth -= 1
                if entry.depth <= 0:
                    _REG.pop(key, None)
                    done.append(entry)
        for entry in done:
            release(entry.cwd)


@contextlib.contextmanager
def current(cfg, root: str):
    """The working folder for this tree: the one registered by an enclosing for_trees, else one prepared for this
    block alone (direct callers such as affected_tests.run) and released after it."""
    with _REG_LOCK:
        entry = _REG.get(_real(root))
    if entry is not None:
        yield entry.get()
        return
    cwd = prepare_cwd(cfg, root, sandboxed(cfg))
    with _REG_LOCK:
        _LAST["tree"] = cwd.label
    try:
        yield cwd
    finally:
        release(cwd)


def describe() -> str:
    """Labels of the working folders prepared by the last for_trees block (or direct call), for the report."""
    with _REG_LOCK:
        if not _LAST:
            return "not used"
        order = [r for r in ("work", "baseline", "tree") if r in _LAST]
        return "; ".join(f"{r}: {_LAST[r]}" for r in order)


# ---- Part B: the baseline's folder, once per session ----

def fingerprint(folder: str) -> list[int] | None:
    """[entries, total file bytes, newest st_mtime_ns over files and folders] from one os.walk and lstat, or None
    when the folder is gone. A write that restores the mtime with os.utime would not show (the audit hook refuses
    utime during calls)."""
    try:
        newest = os.lstat(folder).st_mtime_ns
    except OSError:
        return None
    count = size = 0
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in (*dirnames, *filenames):
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            count += 1
            if stat.S_ISREG(st.st_mode):
                size += st.st_size
            newest = max(newest, st.st_mtime_ns)
    return [count, size, newest]


def _manifest_path(cfg) -> Path:
    return Path(cfg.state_dir) / "cwd_cache.json"


def _read_manifest(cfg) -> dict:
    try:
        got = json.loads(_manifest_path(cfg).read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _write_manifest(cfg, manifest: dict) -> None:
    path = _manifest_path(cfg)
    tmp = path.with_name(f".cwd_cache.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(manifest))
    os.replace(tmp, path)


@contextlib.contextmanager
def _cache_lock(cfg):
    """Exclusive flock on <state>/cwd_cache.lock: hooks that overlap build one folder, one at a time."""
    import fcntl
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(cfg.state_dir) / "cwd_cache.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _applies(cfg, tree: str) -> bool:
    """The cache is for the session baseline only: a session whose baseline was built (session.json baseline_ok)
    and a tree that is that baseline folder."""
    from guardlib import baseline, ledger
    return bool(ledger.load(Path(cfg.state_dir)).get("baseline_ok")) and _real(tree) == _real(str(baseline.baseline_dir(cfg)))


def stale_key(cfg, tree: str) -> dict:
    """What the cached folder was built from; any change rebuilds it."""
    from guardlib import ledger
    from runner import warm
    bd = cfg.behavioral_diff
    return {"baseline_commit": ledger.load(Path(cfg.state_dir)).get("baseline_commit"),
            "generation": warm._generation(cfg), "tree": _real(tree), "dev_tree": _dev(_real(tree)),
            "dev_tmp": _dev(_real(tempfile.gettempdir())), "cwd_mode": str(bd.get("cwd_mode") or "auto"),
            "cwd": bd.get("cwd") or None, "list_version": LIST_VERSION}


def _is_cache_folder(cfg, p) -> bool:
    """A path the session cache may use or delete: an lg-cwd-base-* folder (not a symlink) directly in the temp
    folder, outside the project. Anything else a manifest names is neither reused nor deleted."""
    if not isinstance(p, str) or not p:
        return False
    p = p.rstrip(os.sep)
    return (os.path.basename(p).startswith(CACHE_PREFIX) and not os.path.islink(p)
            and _real(os.path.dirname(p)) == _real(tempfile.gettempdir()) and not _inside(p, str(cfg.root)))


def _reuse(cfg, manifest: dict, key: dict, tree: str, started: float) -> Cwd | None:
    folder = manifest.get("path")
    if (manifest.get("stale_key") != key or manifest.get("strategy") not in CACHE_STRATEGIES
            or not _is_cache_folder(cfg, folder)):
        return None
    if not os.path.isdir(folder) or fingerprint(folder) != manifest.get("fingerprint"):
        return None
    path, _ = _subdir(folder, key["cwd"], create=False)
    seconds = time.monotonic() - started
    label = f"{manifest.get('label', manifest['strategy'])}; reused from an earlier check this session (checked in {seconds:.2f} s)"
    return Cwd(path=path, strategy=manifest["strategy"], label=label, seconds=seconds, files=int(manifest.get("files") or 0),
               nbytes=int(manifest.get("nbytes") or 0), owned=False, tree=tree, folder=folder, reused=True)


def _cache_location(cfg, tree: str) -> str | None:
    folder = tempfile.mkdtemp(prefix=CACHE_PREFIX, dir=tempfile.gettempdir())
    if _inside(folder, str(cfg.root)) or _inside(folder, tree):
        shutil.rmtree(folder, ignore_errors=True)
        return None
    return folder


def session_baseline(cfg, tree: str, sandboxed: bool) -> Cwd:
    """The baseline's working folder, built once per session and reused while its stale key and fingerprint hold.
    Only clone and copy are cached (empty and off cost nothing). Outside a session baseline, or on any error, a
    per-check folder as in Part A."""
    started = time.monotonic()
    try:
        if not _applies(cfg, tree):
            return prepare_cwd(cfg, tree, sandboxed)
        key = stale_key(cfg, tree)
        got = _reuse(cfg, _read_manifest(cfg), key, tree, started)  # no lock: the manifest is replaced atomically
        if got is not None:
            return got
        with _cache_lock(cfg):
            manifest = _read_manifest(cfg)
            got = _reuse(cfg, manifest, key, tree, started)  # another hook built it while this one waited
            if got is not None:
                return got
            key = stale_key(cfg, tree)  # read again under the lock: the key describes what is cloned now
            folder = _cache_location(cfg, tree)
            if folder is None:
                return prepare_cwd(cfg, tree, sandboxed)
            retired = [p for p in manifest.get("retired") or [] if isinstance(p, str)]
            # Tracked before it is filled: a hook killed mid-build leaves nothing that SessionStart would miss.
            _write_manifest(cfg, {**manifest, "retired": [*retired, folder]})
            cwd = prepare_cwd(cfg, tree, sandboxed, folder)
            if cwd.folder != folder or cwd.strategy not in CACHE_STRATEGIES:
                shutil.rmtree(folder, ignore_errors=True)
                _write_manifest(cfg, {**manifest, "retired": retired})
                return cwd  # empty, off or a failure: per check, as in Part A
            old = manifest.get("path")
            if isinstance(old, str) and old and old != folder and old not in retired:
                retired.append(old)  # another hook may still be using it: deleted at the next SessionStart
            _write_manifest(cfg, {"path": folder, "strategy": cwd.strategy, "label": cwd.label, "files": cwd.files,
                                  "nbytes": cwd.nbytes, "stale_key": key, "fingerprint": fingerprint(folder),
                                  "retired": retired})
            cwd.owned = False
            cwd.label += "; built for this session, kept for later checks"
            return cwd
    except Exception:  # noqa: BLE001 - hooks fail open: a per-check folder, as in Part A
        return prepare_cwd(cfg, tree, sandboxed)


def _drop(cfg) -> None:
    """Delete the cached folder and every retired one, then the manifest; the caller holds the cache lock. Only
    folders that pass _is_cache_folder are deleted, whatever the manifest says."""
    manifest = _read_manifest(cfg)
    for p in [manifest.get("path"), *(manifest.get("retired") or [])]:
        if _is_cache_folder(cfg, p):
            shutil.rmtree(p, ignore_errors=True)
    with contextlib.suppress(FileNotFoundError):
        _manifest_path(cfg).unlink()


def drop_session_cache(cfg) -> None:
    """Delete the cached baseline folder and every retired one, then the manifest. Fails open."""
    try:
        with _cache_lock(cfg):
            _drop(cfg)
    except Exception:  # noqa: BLE001
        pass

