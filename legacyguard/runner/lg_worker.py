"""Runs in the target project's interpreter. Standard library and lg_codec only.

Usage: python -B lg_worker.py <request.json> <result.json>
Blocks network, databases, file writes and subprocesses before any target code is imported (D-012, NFR-05).
"""
from __future__ import annotations

import asyncio
import builtins
import datetime as _dt
import dis
import enum
import errno
import importlib
import inspect
import io
import ipaddress
import itertools
import json
import os
import queue
import random
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lg_codec  # noqa: E402
import lg_stubs  # noqa: E402

BLOCKS: list[str] = []
CALL_TIMEOUT_S = 5
SHORT_TIMEOUT_S = 0.5
MAX_TIMEOUTS = 10
FUNCTION_BUDGET_S = 10.0  # all inputs of one function together; the rest are skipped and reported as slow


class GuardBlocked(BaseException):
    """BaseException, so a target's `except Exception` cannot hide that it tried."""


class CallTimeout(BaseException):
    pass


def _blocker(kind: str):
    def block(*args, **kwargs):
        BLOCKS.append(kind)
        raise GuardBlocked(f"{kind} is blocked during the behavioral diff")
    return block


def _is_ip_literal(host) -> bool:
    try:
        ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
        return True
    except (ValueError, AttributeError, UnicodeDecodeError):
        return False


def install_blocks() -> None:
    net = _blocker("network")
    socket.socket.connect = net
    socket.socket.connect_ex = net
    socket.socket.sendto = net
    socket.create_connection = net
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host, *args, **kwargs):
        # A numeric address needs no lookup, so nothing leaves the machine. Libraries probe with one at import
        # (Ansible checks socket.getaddrinfo('127.0.0.1', ...) to decide on a patch). Names are still refused.
        if host is None or _is_ip_literal(host):
            return real_getaddrinfo(host, *args, **kwargs)
        return net()

    socket.getaddrinfo = guarded_getaddrinfo
    db = _blocker("database")
    sqlite3.connect = db
    sqlite3.dbapi2.connect = db
    real_open = _REAL_FS["open"]
    file_block = _blocker("file write")

    class GuardedOpen:
        """open() with writes into the repo refused. A callable object, not a function: Python 3.10's pathlib keeps
        io.open as a class attribute (_NormalAccessor.open), and a plain function stored there would be bound as a
        method, receiving the accessor as `file` and the path as `mode`.

        During setup (import, building inputs) a write to a cache-like path goes to the run's shadow folder instead
        (see _redirect); a read of a path written that way reads the shadow copy."""

        def __call__(self, file, mode="r", *args, **kwargs):
            writing = isinstance(mode, str) and any(c in mode for c in "wax+")
            if (writing and str(file) not in (os.devnull,)
                    and not GUARD_WRITES.get("allowed") and not _is_scratch(file)):
                shadow = _redirect(file, mode=mode)
                if shadow is None:
                    file_block()
                fh = real_open(shadow[1], mode, *args, **kwargs)
                SHADOW[shadow[0]] = shadow[1]
                return fh
            if not writing and SHADOW:
                file = _shadow_of(file) or file
            return real_open(file, mode, *args, **kwargs)

    guarded_open = GuardedOpen()
    builtins.open = guarded_open
    io.open = guarded_open
    _install_os_redirects()
    proc = _blocker("subprocess")
    subprocess.Popen.__init__ = proc  # patch the method: libraries subclass Popen at import time
    subprocess.run = proc
    os.system = proc
    if hasattr(sys, "addaudithook"):  # Python 3.8+; older interpreters keep the patches above only
        sys.addaudithook(_audit)


# Second layer (PEP 578). The patches above give clear reasons for the common calls; the audit hook sees the same
# operations however they are reached (os.open, os.remove, shutil.rmtree, _socket, posix_spawn, ...). It is a
# deny-list inside the same process, so it is not a security boundary by itself: the runner adds an OS sandbox
# where one is available.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PATH_EVENTS = {"os.remove": 0, "os.rmdir": 0, "os.rename": 0, "os.truncate": 0, "os.chmod": 0, "os.chown": 0,
                "os.link": 1, "os.symlink": 1, "os.mkdir": 0, "os.utime": 0, "shutil.rmtree": 0, "shutil.move": 0,
                "shutil.copyfile": 1, "shutil.copytree": 1, "os.removexattr": 0, "os.setxattr": 0}
_ALWAYS = {"subprocess.Popen": "subprocess", "os.system": "subprocess", "os.posix_spawn": "subprocess",
           "os.spawn": "subprocess", "os.exec": "subprocess", "os.fork": "subprocess", "os.forkpty": "subprocess",
           "os.kill": "subprocess", "os.killpg": "subprocess", "pty.spawn": "subprocess",
           "socket.connect": "network", "socket.sendto": "network", "socket.sendmsg": "network", "socket.bind": "network",
           "sqlite3.connect": "database", "ctypes.dlopen": "native code"}
_IN_CALL = {"active": False}  # ctypes is refused only while target code runs: some libraries load it at import


def _audit(event, args):
    if GUARD_WRITES["allowed"] or event[0] not in "ospc":  # cheap exit for the many events we never block
        return
    kind = None
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & _WRITE_FLAGS)
        if writing and path not in (os.devnull, None) and not isinstance(path, int) and not _is_scratch(path):
            kind = "file write"
    elif event in _PATH_EVENTS:
        # Every path the operation touches must be scratch: renaming a temp file over a repo file is a repo write.
        # The first argument is a path or a file descriptor; a second one counts only when it is a path (os.mkdir,
        # os.chmod and os.truncate pass a mode or a length there, which made every mkdir refused, even in the temp folder).
        paths = [a for a in args[:1] if isinstance(a, (str, bytes, int, os.PathLike))]
        paths += [a for a in args[1:2] if isinstance(a, (str, bytes, os.PathLike))]
        if not paths or any(isinstance(p, int) or not _is_scratch(p) for p in paths):
            kind = "file write"
    elif event in _ALWAYS:
        kind = _ALWAYS[event]
        if kind == "native code" and not _IN_CALL["active"]:
            kind = None
        if kind == "network" and event == "socket.connect" and len(args) > 1 and _is_local_socket(args[1]):
            kind = None
        if kind == "network" and event == "socket.bind" and len(args) > 1 and _is_loopback_bind(args[1]):
            kind = None  # urllib3 probes IPv6 support at import by binding to ::1; nothing leaves the machine
    elif event == "socket.getaddrinfo" and args and not (args[0] is None or _is_ip_literal(args[0])):
        kind = "network"
    if kind:
        BLOCKS.append(kind)
        raise GuardBlocked(f"{kind} is blocked during the behavioral diff ({event})")


def _is_loopback_bind(address) -> bool:
    if isinstance(address, (str, bytes)):
        return True  # a Unix socket path
    host = address[0] if isinstance(address, tuple) and address else None
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _is_local_socket(address) -> bool:
    """A Unix socket path inside the temp folder (multiprocessing helpers, test servers) is not the network."""
    return isinstance(address, (str, bytes)) and _is_scratch(address)


GUARD_WRITES = {"allowed": False}
STUBS = {"on": False}  # databases and HTTP clients answered by lg_stubs instead of blocked (roadmap C1)
SCRATCH = {"temp": "", "protect": [], "dir": "", "owned": False}


def _is_scratch(file) -> bool:
    """True for a path in the system temp folder that is not inside the guarded repo (D-041).

    Settings modules and libraries keep throwaway files there (a generated key, a log, a cache). Refusing those
    made every function of such a project Untested, while the files that matter are the repo's own.
    """
    if not SCRATCH["temp"] or isinstance(file, int):
        return False
    try:
        real = os.path.realpath(os.fspath(file))
    except TypeError:
        return False
    if isinstance(real, bytes):
        real = os.fsdecode(real)
    real += os.sep
    return real.startswith(SCRATCH["temp"]) and not any(real.startswith(p) for p in SCRATCH["protect"])


# ---- writes during setup (Task 6.8a) ----
# Importing a module or building an input may write a cache: a pickled table next to the module, a folder under
# ~/.cache, a lookup file. Refusing those left the whole module untested. While SETUP is on (module import, Django
# setup, building self and the arguments) such a write is not refused but redirected: the file goes to
# <run scratch>/shadow/<its real absolute path>, and later reads of that path in the same run read the shadow copy.
# The real file is never created or changed. Only cache-like paths are redirected (a path part containing "cache",
# or a .pickle/.pkl/.cache file); any other write outside the temp folder is still refused, and during the compared
# call itself nothing is redirected. Each run has its own scratch folder, deleted after the run; a warm server's
# children each get a fresh one on top of the server's (read-only to them), so nothing written in one run is seen by
# another.
_REAL_FS = {"open": builtins.open, "os_open": os.open, "mkdir": os.mkdir, "replace": os.replace, "rename": os.rename,
         "remove": os.remove, "unlink": os.unlink}
SETUP = {"on": False}
SHADOW: dict = {}  # real path -> shadow path, for files and folders written during setup in this process
CACHE_SUFFIXES = (".pickle", ".pkl", ".cache")


class setup_phase:
    """Context manager: target code runs here to import or to build inputs, not as the compared call."""

    def __enter__(self):
        self.prev = SETUP["on"]
        SETUP["on"] = True

    def __exit__(self, *exc):
        SETUP["on"] = self.prev
        return False


def _real_str(file):
    if isinstance(file, int):
        return None
    try:
        real = os.path.realpath(os.fspath(file))
    except (TypeError, ValueError):
        return None
    return os.fsdecode(real) if isinstance(real, bytes) else real


def _cache_like(real: str) -> bool:
    """A path that looks like a cache, judged below the project (or home) folder that holds it, so a project that
    itself lives in a folder named "cache" does not make all its files cache-like."""
    base = ""
    for p in SCRATCH["protect"] + [os.path.join(os.path.realpath(os.path.expanduser("~")), "")]:
        if (real + os.sep).startswith(p) and len(p) > len(base):
            base = p
    rel = real[len(base):].lower()
    return any("cache" in part for part in rel.split(os.sep)) or rel.endswith(CACHE_SUFFIXES)


def _shadow_of(file):
    """The shadow copy of a path written during setup, or None."""
    real = _real_str(file)
    return SHADOW.get(real) if real else None


def _make_dirs(path: str) -> None:
    if os.path.isdir(path):
        return
    _make_dirs(os.path.dirname(path))
    try:
        _REAL_FS["mkdir"](path)
    except FileExistsError:
        pass


def _redirect(file, mode: str = "", flags: int = 0):
    """(real path, shadow path) for a write during setup to a cache-like path outside the temp folder, or None when
    the write must be refused. Raises what the real call would for a missing folder or an exclusive create of an
    existing file. Append and update modes start from the current content (the shadow copy or the real file)."""
    if not SETUP["on"] or not SCRATCH["dir"] or isinstance(file, int):
        return None
    real = _real_str(file)
    if not real or not _cache_like(real):
        return None
    target = os.path.join(SCRATCH["dir"], "shadow", real.lstrip(os.sep))
    folder, parent = os.path.dirname(target), os.path.dirname(real)
    if not os.path.isdir(folder):
        if not (parent in SHADOW or os.path.isdir(parent)):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", os.fspath(file))
        _make_dirs(folder)
    current = SHADOW.get(real) or (real if os.path.isfile(real) else None)
    exclusive = "x" in mode or (flags & os.O_CREAT and flags & os.O_EXCL)
    if exclusive and current:
        raise FileExistsError(errno.EEXIST, "File exists", os.fspath(file))
    keeps = ("a" in mode or "+" in mode) and "w" not in mode if mode else not flags & os.O_TRUNC
    if keeps and current and current != target and os.path.isfile(current):
        with _REAL_FS["open"](current, "rb") as src, _REAL_FS["open"](target, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return real, target


def _install_os_redirects() -> None:
    """os.open, os.mkdir (so os.makedirs), os.replace/rename and os.remove/unlink follow the same redirect. Anything
    else that writes is still seen, and refused, by the audit hook."""

    def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None and not isinstance(path, int):
            if flags & _WRITE_FLAGS and not GUARD_WRITES["allowed"] and not _is_scratch(path):
                shadow = _redirect(path, flags=flags)
                if shadow is not None:
                    fd = _REAL_FS["os_open"](shadow[1], flags, mode)
                    SHADOW[shadow[0]] = shadow[1]
                    return fd
            elif SHADOW and not flags & _WRITE_FLAGS:
                path = _shadow_of(path) or path
        return _REAL_FS["os_open"](path, flags, mode, dir_fd=dir_fd)

    def guarded_mkdir(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None and SETUP["on"] and not GUARD_WRITES["allowed"] and not _is_scratch(path):
            real = _real_str(path)
            if real and _cache_like(real) and SCRATCH["dir"]:
                if os.path.exists(real):
                    raise FileExistsError(errno.EEXIST, "File exists", os.fspath(path))
                if real in SHADOW:
                    return None  # already made in this run; os.makedirs(exist_ok=True) cannot see it, so do not fail it
                parent = os.path.dirname(real)
                if not (parent in SHADOW or os.path.isdir(parent)):
                    raise FileNotFoundError(errno.ENOENT, "No such file or directory", os.fspath(path))
                target = os.path.join(SCRATCH["dir"], "shadow", real.lstrip(os.sep))
                _make_dirs(target)
                SHADOW[real] = target
                return None
        return _REAL_FS["mkdir"](path, mode, dir_fd=dir_fd)

    def mover(name):
        real_fn = _REAL_FS[name]

        def move(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
            if (src_dir_fd is None and dst_dir_fd is None and SETUP["on"] and not GUARD_WRITES["allowed"]
                    and not _is_scratch(dst)):
                source = src if _is_scratch(src) else _shadow_of(src)
                shadow = _redirect(dst, mode="w") if source else None
                if shadow is not None:
                    if _is_scratch(src) or source.startswith(SCRATCH["dir"] + os.sep):
                        real_fn(source, shadow[1])
                    else:  # a warm server's copy, shared by every check: copy it, never move it
                        with _REAL_FS["open"](source, "rb") as fh, _REAL_FS["open"](shadow[1], "wb") as out:
                            shutil.copyfileobj(fh, out)
                    SHADOW.pop(_real_str(src), None)
                    SHADOW[shadow[0]] = shadow[1]
                    return None
            return real_fn(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        return move

    def remover(name):
        real_fn = _REAL_FS[name]

        def remove(path, *, dir_fd=None):
            if dir_fd is None and SHADOW and SETUP["on"] and not GUARD_WRITES["allowed"] and not _is_scratch(path):
                real = _real_str(path)
                shadow = SHADOW.get(real) if real else None
                if shadow is not None:
                    SHADOW.pop(real)
                    if shadow.startswith(SCRATCH["dir"] + os.sep):  # a copy from a warm server's layer stays for the others
                        real_fn(shadow)
                    return None
            return real_fn(path, dir_fd=dir_fd)
        return remove

    os.open = guarded_os_open
    os.mkdir = guarded_mkdir
    os.replace, os.rename = mover("replace"), mover("rename")
    os.remove, os.unlink = remover("remove"), remover("unlink")


def use_scratch(path: str | None = None) -> str:
    """Make path (or a fresh folder in the temp folder) the place where this run's redirected writes go."""
    allowed = GUARD_WRITES["allowed"]
    GUARD_WRITES["allowed"] = True
    try:
        if path:
            _make_dirs(path)
            SCRATCH["owned"] = False
        else:
            path = tempfile.mkdtemp(prefix="lg-scratch-")
            SCRATCH["owned"] = True
    finally:
        GUARD_WRITES["allowed"] = allowed
    SCRATCH["dir"] = os.path.realpath(path)
    return SCRATCH["dir"]


def drop_scratch() -> None:
    """Delete the scratch folder if this process made it (a runner-made one is deleted by the runner)."""
    if SCRATCH.get("owned") and SCRATCH["dir"]:
        GUARD_WRITES["allowed"] = True
        shutil.rmtree(SCRATCH["dir"], ignore_errors=True)
        SCRATCH["owned"] = False


def _alarm(signum, frame):
    raise CallTimeout()


def _with_timeout(fn, seconds: float = CALL_TIMEOUT_S):
    if hasattr(signal, "setitimer"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)


def _resolve(module, qualname):
    """(callable or None, needs_instance class or None, reason)."""
    try:
        with setup_phase():
            mod = importlib.import_module(module)
    except Exception:  # noqa: BLE001
        # The package's start-up can fail after this module has loaded: humanize/__init__.py imports every
        # submodule and then a _version file that exists only in an installed copy. A module that is in
        # sys.modules after such a failure ran to the end, so its functions are the real ones.
        mod = sys.modules.get(module)
        if mod is None or getattr(getattr(mod, "__spec__", None), "_initializing", False):
            raise
    parts = qualname.split(".")
    obj = mod
    owner = None
    for part in parts[:-1]:
        obj = getattr(obj, part)
        if not inspect.isclass(obj):
            return None, None, "nested function"
        owner = obj
    name = parts[-1]
    if owner is None:
        return getattr(mod, name), None, ""
    raw = inspect.getattr_static(owner, name)
    if isinstance(raw, staticmethod):
        return raw.__func__, None, ""
    if isinstance(raw, classmethod):
        return getattr(owner, name), None, ""
    if isinstance(raw, property):
        return raw.fget, owner, ""
    return raw, owner, ""


def _span(fn):
    """(file, first line, last line) of the function's own code, nested functions included. None if it has no Python code."""
    try:
        target = inspect.unwrap(getattr(fn, "__func__", fn))
        code = getattr(target, "__func__", target).__code__
    except (AttributeError, ValueError):
        return None
    lines = [code.co_firstlineno]
    stack = [code]
    while stack:
        c = stack.pop()
        lines += [line for _, line in dis.findlinestarts(c) if line]
        stack += [k for k in c.co_consts if inspect.iscode(k)]
    return code.co_filename, min(lines), max(lines)


def _tracer(span, hits: set, gate=None, gate_hits: set | None = None, per_call: list | None = None):
    """Record executed lines inside the function's span only. Every other frame is left untraced, so the cost stays small.

    gate: (real file path, set of line numbers) of an edited function's changed statements, for a caller that is run
    because it calls that function. Frames in that file record only those lines, into gate_hits.
    per_call: a one-item list holding a set that also receives the function's own lines; the caller swaps in a fresh
    set before each input, so it holds that input's lines (Task 6.8e)."""
    filename, first, last = span if span else ("", 0, -1)
    gate_file, gate_lines = gate if gate else ("", frozenset())
    real: dict = {}

    def is_gate_file(name: str) -> bool:
        if not gate_file:
            return False
        if name not in real:
            try:
                real[name] = os.path.realpath(name) == gate_file
            except (TypeError, ValueError):
                real[name] = False
        return real[name]

    if per_call is None:
        def local(frame, event, arg):
            if event == "line":
                hits.add(frame.f_lineno)
            return local

        def local_both(frame, event, arg):
            if event == "line":
                hits.add(frame.f_lineno)
                if frame.f_lineno in gate_lines:
                    gate_hits.add(frame.f_lineno)
            return local_both
    else:
        def local(frame, event, arg):
            if event == "line":
                hits.add(frame.f_lineno)
                per_call[0].add(frame.f_lineno)
            return local

        def local_both(frame, event, arg):
            if event == "line":
                hits.add(frame.f_lineno)
                per_call[0].add(frame.f_lineno)
                if frame.f_lineno in gate_lines:
                    gate_hits.add(frame.f_lineno)
            return local_both

    def local_gate(frame, event, arg):
        if event == "line" and frame.f_lineno in gate_lines:
            gate_hits.add(frame.f_lineno)
        return local_gate

    overlaps: dict = {}

    def touches_gate(code) -> bool:
        # Only code objects whose lines include a gate line are traced line by line: the cost stays with the edit.
        if code not in overlaps:
            try:
                lines = {line for _, line in dis.findlinestarts(code) if line}
            except Exception:  # noqa: BLE001 - when unsure, trace it
                lines = set(gate_lines)
            overlaps[code] = not lines.isdisjoint(gate_lines)
        return overlaps[code]

    def enter(frame, event, arg):
        code = frame.f_code
        own = code.co_filename == filename and first <= code.co_firstlineno <= last
        if is_gate_file(code.co_filename) and touches_gate(code):
            return local_both if own else local_gate
        if own:
            return local
        return None

    return enter


def _source_file(fn) -> str:
    try:
        return os.path.realpath(inspect.getsourcefile(inspect.unwrap(fn)) or "")
    except (TypeError, ValueError):
        return ""


def _finish(value):
    if inspect.iscoroutine(value):
        value = asyncio.run(value)
    if isinstance(value, types.GeneratorType) or inspect.isgenerator(value):
        value = list(itertools.islice(value, 1000))
    return value


# ---- building self for methods (roadmap B3) ----
# A method needs an instance. Recipes, first that works: a constructor call written in the repo with literal arguments
# (tests first), a no-argument constructor, then values made from the __init__ type hints. The recipe is chosen once
# per function and replayed for every input, so each input gets a fresh instance and old and new code get the same one.
_SAMPLE_DEPTH = 2


SELF_VARIANTS = 4


def _samples(variant: int):
    """Plain values per type, a different set for each variant: one instance alone hides bugs that only show for some
    state (abs(x) + abs(y) and abs(x + y) agree for Point(1, 1))."""
    import decimal
    pick = lambda options: options[variant % len(options)]  # noqa: E731
    return {int: pick([1, -3, 0, 7]), float: pick([1.5, -2.5, 0.0, 2.675]), str: pick(["x", "", "Hello World", "a-b/c"]),
            bool: pick([True, False]), bytes: pick([b"x", b""]), complex: 1j,
            bytearray: bytearray(pick([b"x", b""])), memoryview: memoryview(pick([b"x", b""])),
            decimal.Decimal: decimal.Decimal(pick(["10.00", "-2.675", "0", "0.005"])),
            _REAL["date"]: pick([_REAL["date"](2024, 1, 15), _REAL["date"](2000, 2, 29)]),
            _REAL["dt"]: pick([_REAL["dt"](2024, 1, 15, 12, 0), _REAL["dt"](1999, 12, 31, 23, 59, 59)]),
            _dt.timedelta: pick([_dt.timedelta(hours=1), _dt.timedelta(0)]),
            list: [], dict: {}, set: set(), tuple: (), frozenset: frozenset()}


def _sample(tp, depth: int = 0, variant: int = 0):
    """One plain, valid-looking value for a type hint. Raises LookupError when there is no sensible value."""
    import typing
    if tp is inspect.Parameter.empty or tp is typing.Any:
        raise LookupError("no type hint")
    origin = typing.get_origin(tp)
    if origin is typing.Union or (origin is not None and getattr(origin, "__name__", "") == "UnionType"):
        for m in [a for a in typing.get_args(tp) if a is not type(None)]:
            try:
                return _sample(m, depth, variant)
            except LookupError:
                continue
        return None
    base = origin if origin is not None else tp
    for k, v in _samples(variant).items():
        if base is k:
            return v
    if isinstance(base, type) and issubclass(base, enum.Enum):
        members = list(base)
        return members[variant % len(members)]
    if getattr(base, "__name__", "") in ("Sequence", "Iterable", "MutableSequence", "Collection"):
        return []
    if getattr(base, "__name__", "") in ("Mapping", "MutableMapping"):
        return {}
    if isinstance(base, type) and depth < _SAMPLE_DEPTH:
        return _synth(base, depth + 1, variant)
    raise LookupError(f"no value for {tp!r}")


def _init_hints(cls) -> tuple:
    import typing
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError) as exc:
        raise LookupError(f"no signature: {exc}") from exc
    try:
        hints = typing.get_type_hints(cls.__init__)
    except Exception:  # noqa: BLE001 - unresolvable forward references: fall back to the raw annotations
        hints = {}
    params = [p for p in list(sig.parameters.values())[1:] if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    return params, hints


def _synth(cls, depth: int = 0, variant: int = 0):
    """An instance of cls from its __init__ type hints. Optional parameters keep their defaults."""
    params, hints = _init_hints(cls)
    pos, kw = [], {}
    for i, p in enumerate(params):
        if p.default is not inspect.Parameter.empty:
            continue
        # Offset by position, so fields of one type differ from each other (Point(1, -3), not Point(1, 1)).
        value = _sample(hints.get(p.name, p.annotation), depth, variant + i)
        if p.kind == p.KEYWORD_ONLY:
            kw[p.name] = value
        else:
            pos.append(value)
    return cls(*pos, **kw)


def _instance_maker(owner, spec):
    """(make, recipe, failure). make(i) returns a fresh instance for input i, rotating through the instances that
    could be built, so the inputs see several different states. None and the reason when no recipe worked."""
    tried = []
    good_calls = []
    for call in spec.get("self_calls") or []:
        def from_call(call=call):
            pos, kw, _ = _build_args(call, None)
            return owner(*pos, **kw)
        try:
            reset_determinism()
            from_call()
            good_calls.append(from_call)
        except GuardBlocked:
            raise
        except BaseException as exc:  # noqa: BLE001
            tried.append(f"example call: {type(exc).__name__}")
    if good_calls:
        return (lambda i: good_calls[i % len(good_calls)]()), f"{len(good_calls)} example call(s) in the repo", ""
    variants = []
    for v in range(SELF_VARIANTS):
        try:
            reset_determinism()
            _synth(owner, 0, v)
            variants.append(v)
        except GuardBlocked:
            raise
        except BaseException as exc:  # noqa: BLE001
            if not variants and v == 0:
                tried.append(f"from type hints: {type(exc).__name__}: {str(exc)[:80]}")
    if variants:
        return (lambda i: _synth(owner, 0, variants[i % len(variants)])), "values made from the __init__ type hints", ""
    try:
        reset_determinism()
        owner()
        return (lambda i: owner()), "a no-argument constructor", ""
    except GuardBlocked:
        raise
    except BaseException as exc:  # noqa: BLE001
        tried.append(f"{owner.__name__}(): {type(exc).__name__}: {str(exc)[:80]}")
    # Last resorts (Task 6.8c). Each is a weaker stand-in for a real instance; the maker carries .weak = True when
    # its instance is a placeholder, and the run reports "self_weak" so the comparison labels the result.
    carved = _carved_selves(spec)
    if carved:
        return (lambda i: lg_codec.decode(carved[i % len(carved)])), f"{len(carved)} instance(s) recorded from real calls", ""
    made, why = _last_resort(owner, spec)
    if made is not None:
        return made
    tried += why
    return None, "", f"could not build a {owner.__name__} to call the method on ({'; '.join(tried)})"


# (class, harvested __init__ types) -> (maker tuple or None, [reasons]). The search runs once per class per worker
# process, whatever number of its methods are checked; a "none worked" outcome is kept too.
_LAST_RESORT: dict = {}


def _last_resort(owner, spec):
    """Placeholder constructor arguments, else an instance made without the constructor (see below), cached."""
    key = (owner, json.dumps(spec.get("self_types") or {}, sort_keys=True))
    if key in _LAST_RESORT:
        return _LAST_RESORT[key]
    why = []
    try:
        made, reason = _placeholder_maker(owner, spec)
    except GuardBlocked:
        raise
    except BaseException as exc:  # noqa: BLE001 - a bug in the search must not lose the reason found so far
        made, reason = None, f"placeholder arguments: {type(exc).__name__}: {str(exc)[:80]}"
    if made is None:
        why.append(reason)
        # Policy: when __init__ rejects every placeholder, the instance is still made without it. Methods that do not
        # depend on what __init__ computes are then checked (weak, labelled as on a placeholder); a method that reads
        # an attribute only __init__ would have computed raises on both sides, which counts as an invalid guess, so
        # it stays untested with the reason naming the placeholder.
        made, reason = _bypass_maker(owner)
        if made is None:
            why.append(reason)
    _LAST_RESORT[key] = (made, why)
    return made, why


# ---- last-resort self (Task 6.8c) ----
# A class whose constructor needs arguments, with no type hints and no literal constructor call in the repo. In order:
# an instance recorded from a real call (tests or callers, carried by inputs that have their own `self`); placeholder
# constructor arguments; an instance made without its constructor. The last two are guesses: the inputs run on them are
# weak, and the result says the instance was a placeholder. Every choice depends only on the class, so all three runs
# (old twice, new once) pick the same one unless the edit changed the constructor; the comparison checks that.
# The search is bounded by counts, never by total elapsed time, so a loaded machine does not change which recipe the
# old and new runs pick. The per-attempt limit only guards against a hang: an attempt that runs past it fails, and
# after PLACEHOLDER_TIMEOUTS such attempts the search stops (a slow constructor would make every attempt slow).
PLACEHOLDER_ATTEMPTS = 24    # constructor calls tried at most
PLACEHOLDER_CALL_S = 0.5     # for one of them
PLACEHOLDER_TIMEOUTS = 2


def _ph_callable(*args, **kwargs):
    """A placeholder for a parameter that must be callable: accepts anything, returns None."""
    return None


# Plain values tried for an untyped required parameter, by index; "h" is the value from its annotation, if any.
_PH_SHOW = ["None", "0", "1", "''", "'x'", "[]", "{}", "<object>", "<function>", "True"]


def _ph_value(k, hint=None):
    if k == "h":
        return _sample(hint, 0, 0)
    return [None, 0, 1, "", "x", [], {}, types.SimpleNamespace(), _ph_callable, True][k]


_PH_CALLABLE_NAMES = ("func", "fn", "f", "callback", "cb", "handler", "hook", "factory", "predicate", "getter", "setter",
                      "action", "loader", "wrapped", "callable")
_PH_MAPPING = ("config", "conf", "settings", "options", "opts", "params", "kwargs", "mapping", "meta", "attrs", "headers",
               "environ", "env", "context", "ctx", "dict", "data")
_PH_SEQUENCE = ("list", "items", "seq", "args", "array", "values", "rows")
_PH_TEXT = ("path", "file", "dir", "name", "url", "text", "prefix", "suffix", "label", "title", "root", "folder", "pattern")
_PH_NUMBER = ("count", "size", "num", "index", "idx", "limit", "port", "timeout", "width", "height", "depth", "level",
              "max", "min", "len", "offset", "length", "retries")
_PH_BY_TYPE = {"None": [0], "int": [2, 1], "float": [2, 1], "str": [4, 3], "bool": [9], "bytes": [4], "list": [5], "tuple": [5],
               "set": [5], "dict": [6]}


def _ph_order(name: str, hint, seen: dict | None) -> list:
    """The placeholder values to try for one required parameter, best guess first: the value its annotation gives,
    the types real calls passed (harvested from tests), then what its name suggests, then the rest of the pool."""
    prefs: list = []
    if hint is not inspect.Parameter.empty:
        try:
            _sample(hint, 0, 0)
            prefs.append("h")
        except Exception:  # noqa: BLE001 - an annotation it cannot build from still names the kind of value
            name = f"{name} {getattr(hint, '__name__', hint)}"
    for tname, _ in sorted((seen or {}).items(), key=lambda kv: (-kv[1], kv[0])):
        prefs += _PH_BY_TYPE.get(tname.split("[", 1)[0], [7])
    low = str(name).lower()
    words = set(low.replace(" ", "_").split("_"))
    if words & set(_PH_CALLABLE_NAMES) or low.endswith(("_func", "_fn", "_callback", "_cb", "_handler", "_hook", "_factory")):
        prefs.append(8)
    if any(w in low for w in _PH_MAPPING):
        prefs += [6, 7]
    if any(w in low for w in _PH_SEQUENCE) or (low.endswith("s") and not low.endswith("ss") and len(low) > 3):
        prefs.append(5)
    if any(w in low for w in _PH_TEXT):
        prefs += [4, 3]
    if any(w in low for w in _PH_NUMBER) or low in ("n", "i", "j", "k", "x", "y"):
        prefs += [2, 1]
    return list(dict.fromkeys(prefs + list(range(len(_PH_SHOW)))))


def _combos(lengths: list):
    """Index tuples in order of their sum (every parameter's first choice, then one step away, ...), lexicographic
    within one sum. Lazy, so a caller can stop after a few."""
    def parts(total, ls):
        if not ls:
            if total == 0:
                yield ()
            return
        for first in range(min(total, ls[0] - 1) + 1):
            for rest in parts(total - first, ls[1:]):
                yield (first,) + rest
    for total in range(sum(n - 1 for n in lengths) + 1):
        yield from parts(total, lengths)


def _placeholder_maker(owner, spec):
    """((make, recipe, ""), "") with placeholder arguments for the required parameters of __init__ that the type
    hints could not fill, or (None, reason). Bounded by PLACEHOLDER_ATTEMPTS calls and PLACEHOLDER_TIMEOUTS hangs."""
    params, hints = _init_hints(owner)
    required = [p for p in params if p.default is inspect.Parameter.empty]
    if not required:
        return None, "placeholder arguments: no required parameters to fill"
    seen = spec.get("self_types") or {}
    orders = [_ph_order(p.name, hints.get(p.name, p.annotation), seen.get(p.name)) for p in required]
    hint_of = {p.name: hints.get(p.name, p.annotation) for p in required}

    def build(combo):
        pos, kw = [], {}
        for p, k in zip(required, combo):
            value = _ph_value(k, hint_of[p.name])
            if p.kind == p.KEYWORD_ONLY:
                kw[p.name] = value
            else:
                pos.append(value)
        return owner(*pos, **kw)

    last = ""
    attempts = timeouts = 0
    for idx in _combos([len(o) for o in orders]):
        if attempts >= PLACEHOLDER_ATTEMPTS or timeouts >= PLACEHOLDER_TIMEOUTS:
            break
        attempts += 1
        combo = tuple(o[i] for o, i in zip(orders, idx))
        try:
            reset_determinism()
            _with_timeout(lambda: build(combo), PLACEHOLDER_CALL_S)
        except GuardBlocked:
            raise
        except CallTimeout:
            timeouts += 1
            last = f"ran past {PLACEHOLDER_CALL_S:g} s"
            continue
        except BaseException as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:60]}"
            continue
        shown = ", ".join(f"{p.name}=" + (repr(_ph_value(k, hint_of[p.name])) if k == "h" else _PH_SHOW[k]) for p, k in zip(required, combo))

        def make(i, combo=combo):
            return build(combo)
        make.weak = True
        return (make, f"placeholder constructor arguments {owner.__name__}({shown})", ""), ""
    stop = f", stopped after {timeouts} ran past {PLACEHOLDER_CALL_S:g} s" if timeouts >= PLACEHOLDER_TIMEOUTS else ""
    return None, f"placeholder arguments: {attempts} tried, none worked{stop} (last: {last})"


def _init_attributes(owner) -> dict:
    """{attribute: value} that the class's own __init__ sets at its top level from a literal, an empty container, or a
    parameter: a parameter with a default gives a copy of the default, a required one its first placeholder."""
    import ast
    import copy
    import textwrap
    init = next((k.__dict__["__init__"] for k in owner.__mro__ if "__init__" in k.__dict__), None)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(init)))
        sig = inspect.signature(init)
    except Exception:  # noqa: BLE001 - no source (a C type, a generated __init__): nothing to copy
        return {}
    fdef = tree.body[0] if tree.body and isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)) else None
    if fdef is None or not fdef.args.args:
        return {}
    me = fdef.args.args[0].arg
    out: dict = {}
    for stmt in fdef.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target, value = stmt.targets[0], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            target, value = stmt.target, stmt.value
        else:
            continue
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == me):
            continue
        if isinstance(value, ast.Name) and value.id in sig.parameters:
            p = sig.parameters[value.id]
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                out[target.attr] = () if p.kind == p.VAR_POSITIONAL else {}
            elif p.default is not inspect.Parameter.empty:
                out[target.attr] = copy.deepcopy(p.default)
            else:
                out[target.attr] = _ph_value(_ph_order(p.name, inspect.Parameter.empty, None)[0])
            continue
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("list", "dict", "set", "tuple") \
                and not value.args and not value.keywords:
            out[target.attr] = {"list": list, "dict": dict, "set": set, "tuple": tuple}[value.func.id]()
            continue
        try:
            out[target.attr] = ast.literal_eval(value)
        except Exception:  # noqa: BLE001 - computed: left unset
            continue
    return out


def _bypass_maker(owner):
    """((make, recipe, ""), "") for an instance made without calling __init__: the class's __new__ with no arguments,
    then the attributes _init_attributes finds. Class-level defaults stay visible through the class as usual. A class
    whose __new__ needs arguments, or that cannot be instantiated at all, is skipped with the reason."""
    new = owner.__new__

    def make(i=0):
        obj = object.__new__(owner) if new is object.__new__ else new(owner)
        for k, v in _init_attributes(owner).items():
            try:
                object.__setattr__(obj, k, v)
            except Exception:  # noqa: BLE001 - slots, read-only: left unset
                continue
        return obj
    try:
        reset_determinism()
        attrs = sorted(_init_attributes(owner))
        _with_timeout(make, PLACEHOLDER_CALL_S)
    except GuardBlocked:
        raise
    except BaseException as exc:  # noqa: BLE001
        what = "its __new__ needs arguments" if new is not object.__new__ and isinstance(exc, TypeError) else "it cannot be made without its constructor"
        return None, f"{what} ({type(exc).__name__}: {str(exc)[:60]})"
    make.weak = True
    detail = f"attributes from __init__: {', '.join(attrs)}" if attrs else "no attributes from __init__"
    return (make, f"an instance made without calling its constructor ({detail})", ""), ""


def _carved_selves(spec) -> list:
    """Encoded `self` values that inputs carry (recorded on the baseline from tests or callers), not guesses, each
    once, in input order."""
    out, seen = [], set()
    for item in spec.get("inputs") or []:
        if not item.get("has_self") or item.get("weak") or item.get("source") == "edit":
            continue
        first = (item.get("args") or [{}])[0]
        if first.get("name") != "self":
            continue
        key = json.dumps(first.get("value"), sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(first["value"])
    return out[:SELF_VARIANTS]


def _build_args(item, make_self, index: int = 0):
    """(positional, keyword, watched): watched pairs each argument's name with the object passed, for state after the call."""
    pos, kw, watched = [], {}, []
    for arg in item["args"]:
        value = lg_codec.decode(arg["value"])
        kind = arg.get("kind", "pos")
        if kind == "pos":
            pos.append(value)
        elif kind == "varargs":
            pos.extend(value)
        elif kind == "varkw":
            kw.update(value)
        else:
            kw[arg["name"]] = value
        watched.append((arg.get("name") or "?", value))
    if make_self is not None and not item.get("has_self"):
        instance = make_self(index)
        pos.insert(0, instance)
        watched.insert(0, ("self", instance))
    return pos, kw, watched


_IMMUTABLE = (str, bytes, int, float, complex, bool, type(None), tuple, frozenset, range, type, types.ModuleType,
              types.FunctionType, types.BuiltinFunctionType, types.MethodType)


def _watchable(value) -> bool:
    if isinstance(value, _IMMUTABLE) or isinstance(value, enum.Enum) or isinstance(value, (lg_stubs.FakeConnection, lg_stubs.FakeCursor)):
        return False
    return isinstance(value, (list, dict, set, bytearray)) or isinstance(getattr(value, "__dict__", None), dict)


# Methods whose job is to set up the instance: their whole result on self is what they do, so it is compared in full.
_BUILDERS = {"__init__", "__post_init__", "__setstate__", "__new__"}


def _self_before(fn, watched) -> object:
    """canon(self) before the call, for comparing only what the call does to it; None when not needed."""
    name = getattr(getattr(fn, "__func__", fn), "__name__", "")
    if name in _BUILDERS:
        return None
    for n, value in watched:
        if n == "self" and _watchable(value):
            try:
                return lg_codec.canon(value)
            except Exception:  # noqa: BLE001
                return None
    return None


_REMOVED = {"t": "removed"}


def _effect(before, after):
    """What a call did to self: the attributes it added, removed (marked) or changed, with their values after the
    call, in the same shape as canon. None when it did nothing.

    The instance is built differently by old and new code (a field the new __init__ adds), or comes from a recording of
    the old code. A method is judged on what it does to self, not on how self was built: that is __init__'s own
    result, compared in full there."""
    if before == after:
        return None
    if not (isinstance(before, dict) and isinstance(after, dict) and before.get("t") == after.get("t") == "obj"
            and (before.get("state") or {}).get("t") == (after.get("state") or {}).get("t") == "dict"):
        return after
    old = {lg_codec._key(k): (k, v) for k, v in before["state"]["v"]}
    new = {lg_codec._key(k): (k, v) for k, v in after["state"]["v"]}
    changed = [list(new[k]) for k in new if k not in old or old[k][1] != new[k][1]]
    changed += [[old[k][0], _REMOVED] for k in old if k not in new]
    if not changed and before.get("cls") == after.get("cls"):
        return None
    changed.sort(key=lambda kv: lg_codec._key(kv[0]))
    # "effect": an attribute missing here was left as it was, not unset (the counterexample text says "unchanged").
    return {**after, "state": {"t": "dict", "v": changed}, "effect": True}


def _state_after(watched, self_before=None) -> dict | None:
    """What the call left behind in its mutable arguments and in self (FR-14, roadmap B1).

    Old and new code that return the same value can still differ in what they do to a list or an object they were
    given. Immutable values cannot change, so they are left out and the result stays small. Other arguments are
    compared in full: both versions build them from the same input. For self, given self_before, only the call's own
    effect is kept (_effect)."""
    state = {}
    for name, value in watched:
        if not _watchable(value):
            continue
        try:
            canon = lg_codec.canon(value)
        except Exception:  # noqa: BLE001 - an object we cannot read is simply not compared
            continue
        if name == "self" and self_before is not None:
            canon = _effect(self_before, canon)
            if canon is None:
                continue
        state[name] = canon
    return state or None


# ---- determinism (roadmap B2) ----
# Old and new code are run with the same random seed, the same clock and the same uuid sequence, reset before every
# input. Values that came from randomness or the time then agree whenever the code agrees, so such functions can be
# checked instead of being dropped as non-deterministic. The clock moves forward 1 ms per reading, so code that waits
# for time to pass still finishes. Consequence: an edit that only adds or removes a clock reading (time.time, and since
# Task 6.8d also monotonic/perf_counter in the project's code) shifts every later reading by 1 ms, so a value computed
# from the clock differs; diff.py keeps that a difference and adds a hedge (Result.clock_note).
FROZEN_START = 1705320000.0  # 2024-01-15 12:00:00 UTC
MONO_START = 1000.0          # where the frozen monotonic, perf_counter, process_time and thread_time readings start
_CLOCK = {"t": FROZEN_START, "mono": MONO_START, "reads": 0}  # reads: frozen readings during this input
CLOCK_LOOP_READS = 200  # a call that ran out of time after this many frozen readings was probably waiting on the clock
_IDS = random.Random(0)
DET = {"on": False}
_REAL = {"dt": _dt.datetime, "date": _dt.date, "time": time.time, "time_ns": time.time_ns,
         "uuid1": uuid.uuid1, "uuid4": uuid.uuid4,
         "monotonic": time.monotonic, "monotonic_ns": time.monotonic_ns, "perf_counter": time.perf_counter,
         "perf_counter_ns": time.perf_counter_ns, "process_time": time.process_time, "process_time_ns": time.process_time_ns,
         "thread_time": time.thread_time, "thread_time_ns": time.thread_time_ns, "sleep": time.sleep,
         "get_ident": threading.get_ident, "get_native_id": threading.get_native_id,
         "getpid": os.getpid, "getppid": os.getppid,
         "ident": threading.Thread.ident, "native_id": threading.Thread.native_id,
         "event_wait": threading.Event.wait, "condition_wait": threading.Condition.wait, "join": threading.Thread.join,
         "queue_get": queue.Queue.get, "select": select.select}
_monotonic = _REAL["monotonic"]  # the worker's own time limits always use the real clock

# What one run looked like from outside (Task 6.8d): its real process ids, the threads it saw and the span of each real
# clock. When OLD-A and OLD-B disagree, diff.py compares the differing values with these to name the cause
# (thread ids, process ids, time readings); nothing here changes what is compared.
_ENV_CLOCKS = ("time", "monotonic", "perf_counter")  # process_time is left out: its small readings look like any number
_ENV_START = {k: _REAL[k]() for k in _ENV_CLOCKS}
_SEEN_THREADS: set = set()
MAX_SEEN_THREADS = 64


def _note_threads() -> None:
    try:
        for t in threading.enumerate():
            for prop in ("ident", "native_id"):
                value = _REAL[prop].fget(t)
                if value is not None and len(_SEEN_THREADS) < MAX_SEEN_THREADS:
                    _SEEN_THREADS.add(value)
    except Exception:  # noqa: BLE001 - the snapshot is a diagnosis aid only
        pass


def env_snapshot() -> dict:
    _note_threads()
    return {"pid": _REAL["getpid"](), "ppid": _REAL["getppid"](), "threads": sorted(_SEEN_THREADS),
            "clocks": {k: [_ENV_START[k], _REAL[k]()] for k in _ENV_CLOCKS},
            "paths": [p for p in (SCRATCH.get("dir"), SCRATCH.get("temp")) if p],
            "heap": [id(object()), id(sys), id(_SEEN_THREADS)]}


def _tick() -> float:
    _CLOCK["reads"] += 1
    _CLOCK["t"] += 0.001
    return _CLOCK["t"]


def _fake_time():
    return _tick()


def _fake_time_ns():
    return int(_tick() * 1_000_000_000)


def _fake_uuid4():
    return uuid.UUID(int=_IDS.getrandbits(128), version=4)


def _fake_uuid1(node=None, clock_seq=None):
    return uuid.UUID(int=_IDS.getrandbits(128), version=1)


# Task 6.8d: clocks and thread ids read by the project's own code (process ids: see install_determinism and
# lg_codec.set_pids; they stay real so the OS can be asked about them). Two runs of the same old code read
# different values from them (they are not seeded), so a method that stores time.monotonic() or threading.get_ident()
# on self disagreed with itself and was never checked. They are frozen only for callers in the project (and the
# TIME_HELPERS modules): asyncio, threading, queue and other libraries keep the real values, so their own timers and
# waits behave as before. The worker's own limits use the real clock (_monotonic).
PROJECT = {"root": ""}
_FROM_PROJECT: dict = {}
_OWN_MODULES = {"__main__", "lg_worker", "lg_codec", "lg_stubs", "lg_server", "legacyguard_harvest"}
_THREAD_IDS: dict = {}  # real ident or native id -> a small stable number, in the order the project's code first read it


def _from_project(depth: int = 2) -> bool:
    """True when the function `depth` frames up (the caller of a fake) belongs to the project."""
    root = PROJECT["root"]
    if not root or not DET["on"]:
        return False
    try:
        g = sys._getframe(depth).f_globals
    except ValueError:
        return False
    name = g.get("__name__")
    hit = _FROM_PROJECT.get(name)
    if hit is None:
        path = g.get("__file__")
        hit = bool(path) and name not in _OWN_MODULES and (name in TIME_HELPERS or os.path.realpath(path).startswith(root))
        _FROM_PROJECT[name] = hit
    return hit


def _mono_tick() -> float:
    _CLOCK["reads"] += 1
    _CLOCK["mono"] += 0.001
    return _CLOCK["mono"]


def _frozen_clock(real_name: str, ns: bool):
    real = _REAL[real_name]

    def reading():
        if not _from_project():
            return real()
        value = _mono_tick()
        return int(value * 1_000_000_000) if ns else value
    reading.__name__ = real_name
    return reading


def _advance(seconds) -> None:
    """Move both frozen clocks on by the time a wait asked for (the same amount in every run, whatever the real wait)."""
    try:
        step = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return
    _CLOCK["t"] += step
    _CLOCK["mono"] += step


def _fake_sleep(seconds):
    """A real sleep. For the project's code the frozen clocks also move on by the time asked for, so a loop that sleeps
    until a deadline read from them ends as it would with the real clock."""
    _REAL["sleep"](seconds)
    if _from_project():
        _advance(seconds)


# The standard blocking waits with a timeout, as called by the project's code: the wait is real, and when it ran out
# (no event, no item, nothing ready) the frozen clocks move on by the timeout, as for time.sleep. A loop such as
# `while time.monotonic() < deadline: event.wait(0.01)` then ends after as many rounds as with the real clock. A wait
# that returned early moves nothing (the real time it took is not repeatable). Calls from library code (queue's own
# Condition.wait, asyncio) stay untouched, so a wait is never counted twice. Not covered: Lock/Semaphore.acquire with a
# timeout and selectors/socket timeouts; a call that runs out of time after many frozen readings says so (clock_loop).
def _event_wait(self, timeout=None):
    got = _REAL["event_wait"](self, timeout)
    if not got and timeout is not None and _from_project():
        _advance(timeout)
    return got


def _condition_wait(self, timeout=None):
    got = _REAL["condition_wait"](self, timeout)
    if not got and timeout is not None and _from_project():
        _advance(timeout)
    return got


def _queue_get(self, block=True, timeout=None):
    try:
        return _REAL["queue_get"](self, block, timeout)
    except queue.Empty:
        if block and timeout is not None and _from_project():
            _advance(timeout)
        raise


def _thread_join(self, timeout=None):
    _REAL["join"](self, timeout)
    if timeout is not None and self.is_alive() and _from_project():
        _advance(timeout)


def _fake_select(rlist, wlist, xlist, timeout=None):
    got = _REAL["select"](rlist, wlist, xlist, timeout)
    if timeout is not None and not any(got) and _from_project():
        _advance(timeout)
    return got


def _stable_thread_id(real, kind: str):
    if real is None:
        return None
    return _THREAD_IDS.setdefault((kind, real), sum(1 for k in _THREAD_IDS if k[0] == kind) + 1)


def _fake_get_ident():
    real = _REAL["get_ident"]()
    return _stable_thread_id(real, "ident") if _from_project() else real


def _fake_get_native_id():
    real = _REAL["get_native_id"]()
    return _stable_thread_id(real, "native") if _from_project() else real


def _thread_ident(self):
    real = _REAL["ident"].fget(self)
    return _stable_thread_id(real, "ident") if _from_project() else real


def _thread_native_id(self):
    real = _REAL["native_id"].fget(self)
    return _stable_thread_id(real, "native") if _from_project() else real


_CLOCKS = {name: _frozen_clock(name, name.endswith("_ns")) for name in
           ("monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns", "process_time", "process_time_ns",
            "thread_time", "thread_time_ns")}


class _SameAsReal(type):
    """isinstance(real_datetime, FrozenDatetime) stays True, so code that checks types behaves as before."""
    def __instancecheck__(cls, obj):
        return isinstance(obj, cls._real)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, cls._real)


class FrozenDatetime(_dt.datetime, metaclass=_SameAsReal):
    _real = _dt.datetime

    @classmethod
    def now(cls, tz=None):
        return _REAL["dt"].fromtimestamp(_tick(), tz)

    @classmethod
    def utcnow(cls):
        return _REAL["dt"].fromtimestamp(_tick(), _dt.timezone.utc).replace(tzinfo=None)

    @classmethod
    def today(cls):
        return cls.now()


class FrozenDate(_dt.date, metaclass=_SameAsReal):
    _real = _dt.date

    @classmethod
    def today(cls):
        return _REAL["date"].fromtimestamp(_tick())


_SWAPS = {id(_REAL["dt"]): FrozenDatetime, id(_REAL["date"]): FrozenDate, id(_REAL["time"]): _fake_time,
          id(_REAL["time_ns"]): _fake_time_ns, id(_REAL["uuid4"]): _fake_uuid4, id(_REAL["uuid1"]): _fake_uuid1,
          id(_REAL["sleep"]): _fake_sleep, id(_REAL["get_ident"]): _fake_get_ident,
          id(_REAL["get_native_id"]): _fake_get_native_id,
          id(_REAL["select"]): _fake_select, **{id(_REAL[n]): fake for n, fake in _CLOCKS.items()}}
_SCANNED: set = set()


def install_determinism() -> None:
    DET["on"] = True
    time.time, time.time_ns = _fake_time, _fake_time_ns
    uuid.uuid4, uuid.uuid1 = _fake_uuid4, _fake_uuid1
    for name, fake in _CLOCKS.items():
        setattr(time, name, fake)
    time.sleep = _fake_sleep
    threading.get_ident, threading.get_native_id = _fake_get_ident, _fake_get_native_id
    # Process ids are not faked: the project's code (and any C extension) may hand its pid to the OS. canon writes the
    # real ids as "<pid>"/"<parent pid>" instead, so the three runs agree on them (lg_codec.set_pids).
    lg_codec.set_pids(_REAL["getpid"](), _REAL["getppid"]())
    threading.Thread.ident = property(_thread_ident, doc=_REAL["ident"].__doc__)
    threading.Thread.native_id = property(_thread_native_id, doc=_REAL["native_id"].__doc__)
    threading.Event.wait, threading.Condition.wait, threading.Thread.join = _event_wait, _condition_wait, _thread_join
    queue.Queue.get = _queue_get
    select.select = _fake_select


# Library modules whose own "now" should follow the frozen clock. Other libraries keep their references: swapping a
# class some library compares by identity (pydantic, ORMs) could change its behavior instead of fixing the clock.
TIME_HELPERS = {"django.utils.timezone"}


def freeze_references(root_real: str) -> None:
    """Replace names bound at import time (`from datetime import datetime`, `from uuid import uuid4`) in the
    project's own modules and in a few framework time helpers."""
    if not DET["on"]:
        return
    for name, mod in list(sys.modules.items()):
        if name in _SCANNED or mod is None or name in ("lg_codec", "__main__"):
            continue
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        if name not in TIME_HELPERS and not os.path.realpath(path).startswith(root_real):
            continue
        _SCANNED.add(name)
        try:
            items = list(vars(mod).items())
        except TypeError:
            continue
        for attr, value in items:
            swap = _SWAPS.get(id(value))
            if swap is not None:
                try:
                    setattr(mod, attr, swap)
                except (AttributeError, TypeError):
                    pass


def frozen_datetime_module(on: bool) -> None:
    """datetime.datetime and datetime.date are swapped only while target code runs. Swapped for good, a library
    that subclasses datetime at import (pydantic, ORMs) would get FrozenDatetime and its metaclass as a base."""
    if not DET["on"]:
        return
    _dt.datetime, _dt.date = (FrozenDatetime, FrozenDate) if on else (_REAL["dt"], _REAL["date"])


def reset_determinism() -> None:
    if not DET["on"]:
        return
    _CLOCK["t"], _CLOCK["mono"], _CLOCK["reads"] = FROZEN_START, MONO_START, 0
    _IDS.seed(0)
    random.seed(0)
    _THREAD_IDS.clear()
    if hasattr(threading, "_counter"):
        # Default thread names ("Thread-3 (run)") count from 1 again for every input: other inputs and planted bugs
        # that start threads must not shift the numbers.
        threading._counter = itertools.count(1).__next__
    np = sys.modules.get("numpy")
    if np is not None:
        try:
            np.random.seed(0)
        except Exception:  # noqa: BLE001
            pass


def _http_caller(app_ref: str):
    """A callable(method, path, json=None, params=None) that sends one request to the app in process and returns
    {status, content_type, body}: Flask's test client, or Starlette/FastAPI's TestClient (roadmap C3)."""
    mod, _, attr = app_ref.partition(":")
    with setup_phase():
        app = getattr(importlib.import_module(mod), attr or "app")
    if hasattr(app, "test_client"):  # Flask
        client = app.test_client()

        def call(method, path, json=None, params=None):
            resp = client.open(path, method=method, json=json, query_string=params)
            body = resp.get_json(silent=True)
            return {"status": resp.status_code, "content_type": (resp.content_type or "").split(";")[0],
                    "body": body if body is not None else resp.get_data(as_text=True)[:4000]}
        return call
    from starlette.testclient import TestClient  # FastAPI and Starlette; no lifespan, so start-up hooks do not run
    client = TestClient(app, raise_server_exceptions=False)

    def call(method, path, json=None, params=None):
        resp = client.request(method, path, json=json, params=params)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text[:4000]
        return {"status": resp.status_code, "content_type": resp.headers.get("content-type", "").split(";")[0], "body": body}
    return call


def run_route(spec) -> dict:
    """Requests to one route of the app, compared like a function's return values."""
    try:
        call = _http_caller(spec["app"])
        if STUBS["on"]:
            lg_stubs.patch_loaded()  # the handlers' own outgoing calls are stubbed; the test client stays in process
    except GuardBlocked as exc:
        return {"status": "import_blocked", "reason": str(exc)}
    except BaseException as exc:  # noqa: BLE001
        return {"status": "import_error", "reason": f"{type(exc).__name__}: {exc}"}
    results = [_call_once(call, item, None, i) for i, item in enumerate(spec["inputs"])]
    return {"status": "ran", "results": results, "lines": None, "env": env_snapshot()}


def run_function(spec, root_real):
    if spec.get("kind") == "http":
        return run_route(spec)
    module, qualname = spec["module"], spec["qualname"]
    try:
        fn, owner, reason = _resolve(module, qualname)
    except GuardBlocked as exc:
        return {"status": "import_blocked", "reason": str(exc)}
    except (ImportError, AttributeError) as exc:
        # "missing" means the function or its own module is not there. A module that exists but cannot import
        # one of its dependencies is an import error, and must be reported as that.
        own = isinstance(exc, ModuleNotFoundError) and exc.name is not None and (module == exc.name or module.startswith(exc.name + "."))
        kind = "missing" if isinstance(exc, AttributeError) or own else "import_error"
        return {"status": kind, "reason": f"{type(exc).__name__}: {exc}"}
    except BaseException as exc:  # noqa: BLE001 - target module code can raise anything at import
        return {"status": "import_error", "reason": f"{type(exc).__name__}: {exc}"}
    if fn is None:
        return {"status": "uncallable", "reason": reason}
    source = _source_file(fn)
    if source and root_real and not source.startswith(root_real):
        # A decorator from elsewhere is fine; the module's own file decides.
        modfile = os.path.realpath(getattr(sys.modules.get(module), "__file__", "") or "")
        if not modfile.startswith(root_real):
            return {"status": "wrong_tree", "reason": f"{module} resolved to {modfile}"}
    results = []
    span = _span(fn)
    hits: set = set()
    gate, gate_hits = _gate_spec(spec, root_real), set()
    # Task 6.8e: each input's own lines, so a second pass can start from the inputs that got furthest.
    per_call = [set()] if spec.get("input_lines") and span else None
    trace = _tracer(span, hits, gate, gate_hits, per_call) if span or gate else None
    freeze_references(root_real)
    if STUBS["on"]:
        lg_stubs.patch_loaded()
    make_self, recipe, no_self = None, "", ""
    if owner is not None and not all(i.get("has_self") for i in spec["inputs"]):
        try:
            with setup_phase():
                make_self, recipe, no_self = _instance_maker(owner, spec)
        except GuardBlocked as exc:
            no_self = f"building a {owner.__name__} was blocked: {exc}"
    # A second pass (Task 6.8e) gets what the first left of the function's time and carries its timeouts over.
    fn_deadline = _monotonic() + min(FUNCTION_BUDGET_S, float(spec.get("budget") or FUNCTION_BUDGET_S))
    timeouts_before = int(spec.get("timeouts_before") or 0)
    input_lines: list = []
    # Per input: the seconds the call itself took and the time limit it ran under (None when it did not run). They sit
    # beside the outcomes, not in them, so outcomes stay what the comparison reads. A slowdown check (Task 6.6) re-runs
    # single inputs with "full_limit" (never the reduced limit) and "indexes" (each input's position in the first run,
    # which picks how its instance is built).
    seconds: list = []
    limits: list = []
    clock_reads: list = []  # Task 6.8d: frozen clock readings per input (None when it did not run)
    indexes = spec.get("indexes") or []
    for index, item in enumerate(spec["inputs"]):
        seconds.append(None)
        limits.append(None)
        clock_reads.append(None)
        input_lines.append(None)
        if owner is not None and make_self is None and not item.get("has_self"):
            results.append({"outcome": "setup_failed", "detail": no_self})
            continue
        # After two inputs time out, the rest get a short limit: one slow function must not use up the whole check.
        # After MAX_TIMEOUTS the rest are skipped, not run: they are almost surely as slow, and are reported as such.
        timeouts = timeouts_before + sum(1 for r in results if r.get("outcome") in ("timeout", "skipped_slow"))
        if timeouts >= MAX_TIMEOUTS or _monotonic() > fn_deadline:
            results.append({"outcome": "skipped_slow"})
            continue
        limit = CALL_TIMEOUT_S if timeouts < 2 or spec.get("full_limit") else SHORT_TIMEOUT_S
        position = indexes[index] if index < len(indexes) else index
        if per_call is not None:
            per_call[0] = set()
        results.append(_call_once(fn, item, make_self, position, trace, limit))
        seconds[-1], limits[-1], clock_reads[-1] = LAST_CALL["seconds"], limit, LAST_CALL.get("clock_reads")
        if per_call is not None:
            input_lines[-1] = sorted(per_call[0])
    out = {"status": "ran", "results": results, "lines": sorted(hits) if span else None, "seconds": seconds, "limits": limits,
           "clock_reads": clock_reads, "env": env_snapshot()}
    if per_call is not None:
        out["input_lines"] = input_lines
    if gate:
        out["gate_lines"] = sorted(gate_hits)
    if recipe:
        out["self_recipe"] = recipe
    if getattr(make_self, "weak", False):
        out["self_weak"] = True  # Task 6.8c: the instance is a placeholder; inputs run on it count as guesses
    if spec.get("mutants"):
        out["mutation"] = _run_mutants(fn, spec, make_self, owner, results)
    return out


# ---- carving (roadmap 3c, Sprint 5 task E) ----
# The caller's inputs run on the baseline while a trace hook records the arguments of every call into an edited
# function, at the call, before the body can change them (test carving, Elbaum et al. 2009). Each recorded call is
# encoded like a harvested test input, so it replays through _build_args. A call with a value that cannot be encoded
# is skipped and counted; `self` that cannot be encoded is left out, and the replay builds an instance as usual.
CAPTURE_MAX = 40
CAPTURE_INPUTS = 100   # caller inputs run at most, per caller
_GEN_FLAGS = inspect.CO_GENERATOR | inspect.CO_COROUTINE | getattr(inspect, "CO_ASYNC_GENERATOR", 0)


def _encode_frame_call(frame, code, first: str) -> dict:
    """{"args": [...], "has_self": bool} for the call that opened frame. first: "self" (a method: its instance is kept
    when it can be encoded), "drop" (a classmethod's cls) or "" (a plain function). Raises lg_codec.Unencodable."""
    loc = frame.f_locals
    n = code.co_argcount + code.co_kwonlyargcount
    names = code.co_varnames[:n]
    args, has_self = [], False
    for i, name in enumerate(names):
        if i == 0 and first == "drop":
            continue
        kind = "pos" if i < code.co_argcount else "kw"
        if i == 0 and first == "self":
            try:
                args.append({"name": "self", "kind": "pos", "value": lg_codec.encode_input(loc[name])})
                has_self = True
            except Exception:  # noqa: BLE001 - the replay builds an instance instead
                pass
            continue
        args.append({"name": name, "kind": kind, "value": lg_codec.encode_input(loc[name])})
    if code.co_flags & inspect.CO_VARARGS:
        name = code.co_varnames[n]
        args.append({"name": name, "kind": "varargs", "value": lg_codec.encode_input(list(loc[name]))})
    if code.co_flags & inspect.CO_VARKEYWORDS:
        name = code.co_varnames[n + (1 if code.co_flags & inspect.CO_VARARGS else 0)]
        args.append({"name": name, "kind": "varkw", "value": lg_codec.encode_input(dict(loc[name]))})
    return {"args": args, "has_self": has_self}


def _capture_tracer(wanted: dict, state: dict):
    """A global trace function recording calls into the code objects in wanted ({code: (key, first, limit)}) into
    state["calls"][key] (distinct by encoded value) and counting unencodable ones in state["unstored"][key].

    Each recorded call carries "weak": whether the caller input running now (state["weak_now"]) is a guess. A call
    recorded first under a guess and later made again under a non-guess becomes non-weak, even past the limit."""
    resumed: list = []  # generator frames already recorded: a resume is not a new call

    def enter(frame, event, arg):
        hit = wanted.get(frame.f_code)
        if hit is None or event != "call":
            return None
        key, first, limit = hit
        calls = state["calls"][key]
        weak_now = bool(state.get("weak_now"))
        full = len(calls) >= limit
        if full and (weak_now or not state["weak_left"][key]):
            return None
        if frame.f_code.co_flags & _GEN_FLAGS:
            if any(f is frame for f in resumed):
                return None
            resumed.append(frame)
        try:
            item = _encode_frame_call(frame, frame.f_code, first)
        except Exception:  # noqa: BLE001 - Unencodable, or a value whose pickling fails: this call is not replayable
            state["unstored"][key] += 1  # calls, repeats included: a value that cannot be encoded cannot be compared either
            return None
        sig = json.dumps(item, sort_keys=True)
        known = state["seen"][key].get(sig)
        if known is not None:
            if known["weak"] and not weak_now:
                known["weak"] = False
                state["weak_left"][key] -= 1
        elif not full:
            item["weak"] = weak_now
            state["seen"][key][sig] = item
            state["weak_left"][key] += weak_now
            calls.append(item)
        return None
    return enter


def _capture_target(entry):
    """(code object, first-argument rule) of an edited function, or None when it has no Python code to watch."""
    fn, owner, _ = _resolve(entry["module"], entry["qualname"])
    if fn is None:
        return None
    first = "self" if owner is not None else ("drop" if inspect.ismethod(fn) else "")
    target = inspect.unwrap(getattr(fn, "__func__", fn))
    code = getattr(target, "__code__", None)
    return (code, first) if code is not None else None


def run_capture(spec, root_real) -> dict:
    """Run a caller's inputs and record the calls it makes into the edited functions in spec["capture"]
    ([{"module", "qualname", "max"}]). Returns {"status", "captured": {key: [call, ...]}, "unstored": {key: n}}.
    A caller input with "weak" set is a guess: the calls it makes are recorded with "weak": True (see _capture_tracer)."""
    keys = [f"{e['module']}:{e['qualname']}" for e in spec["capture"]]
    state = {"calls": {k: [] for k in keys}, "unstored": {k: 0 for k in keys}, "seen": {k: {} for k in keys},
             "weak_left": {k: 0 for k in keys}, "weak_now": False}
    out = {"status": "ran", "captured": state["calls"], "unstored": state["unstored"]}
    try:
        fn, owner, reason = _resolve(spec["module"], spec["qualname"])
    except BaseException as exc:  # noqa: BLE001 - nothing captured; the main runs report the import problem
        return {**out, "status": "import_error", "reason": f"{type(exc).__name__}: {exc}"}
    if fn is None:
        return {**out, "status": "uncallable", "reason": reason}
    wanted = {}
    for key, entry in zip(keys, spec["capture"]):
        try:
            found = _capture_target(entry)
        except BaseException:  # noqa: BLE001
            found = None
        if found is not None:
            wanted[found[0]] = (key, found[1], int(entry.get("max") or CAPTURE_MAX))
    if not wanted:
        return out
    freeze_references(root_real)
    if STUBS["on"]:
        lg_stubs.patch_loaded()
    make_self = None
    if owner is not None and not all(i.get("has_self") for i in spec["inputs"]):
        try:
            with setup_phase():
                make_self, _, _ = _instance_maker(owner, spec)
        except GuardBlocked:
            make_self = None
    trace = _capture_tracer(wanted, state)
    deadline = _monotonic() + min(FUNCTION_BUDGET_S, float(spec.get("budget") or FUNCTION_BUDGET_S))
    timeouts = 0
    inputs = spec["inputs"][:CAPTURE_INPUTS]
    strong_left = [False] * (len(inputs) + 1)  # strong_left[i]: some input from i on is not a guess
    for i in range(len(inputs) - 1, -1, -1):
        guess = inputs[i].get("weak") or (getattr(make_self, "weak", False) and not inputs[i].get("has_self"))
        strong_left[i] = strong_left[i + 1] or not guess
    for index, item in enumerate(inputs):
        # Full everywhere: stop, unless a non-guess input still to come could make a call recorded under a guess non-weak.
        if all(len(state["calls"][k]) >= lim and not (state["weak_left"][k] and strong_left[index]) for k, _, lim in wanted.values()):
            break
        left = deadline - _monotonic()
        if left <= 0.01 or timeouts >= 2:
            break
        if owner is not None and make_self is None and not item.get("has_self"):
            break
        # A call made on a placeholder instance (Task 6.8c) is recorded as a guess too.
        state["weak_now"] = bool(item.get("weak")) or (getattr(make_self, "weak", False) and not item.get("has_self"))
        # One slow call must not run past the caller's share of the recording time: the limit is what is left of it.
        got = _call_once(fn, item, make_self, index, trace, min(SHORT_TIMEOUT_S if timeouts else CALL_TIMEOUT_S, left))
        timeouts += got.get("outcome") == "timeout"
    return out


def _gate_spec(spec, root_real):
    """(real path, line numbers) for spec["gate"] = {"file": path relative to the root, "ranges": [[first, last], ...]}."""
    g = spec.get("gate") or {}
    if not g.get("file") or not g.get("ranges"):
        return None
    lines = frozenset(n for first, last in g["ranges"] for n in range(int(first), int(last) + 1))
    return os.path.realpath(os.path.join(root_real or ".", g["file"])), lines


LAST_CALL = {"seconds": None, "clock_reads": None}  # how long the last _call_once call ran (the real clock: _monotonic)


def _call_once(fn, item, make_self, index: int, trace=None, seconds: float = CALL_TIMEOUT_S) -> dict:
    """One input through fn, with a fresh seed, clock and instance. The outcome as the comparison reads it."""
    start = len(BLOCKS)
    LAST_CALL["seconds"] = LAST_CALL["clock_reads"] = None
    reset_determinism()
    lg_stubs.reset()
    try:
        with setup_phase():
            pos, kw, watched = _build_args(item, make_self, index)
    except BaseException as exc:  # noqa: BLE001
        return {"outcome": "setup_failed", "detail": f"{type(exc).__name__}: {exc}"}
    self_before = _self_before(fn, watched)
    try:
        sys.settrace(trace)
        _IN_CALL["active"] = True
        setup_was, SETUP["on"] = SETUP["on"], False  # the compared call: nothing is redirected, every write is refused
        frozen_datetime_module(True)
        try:
            began = _monotonic()
            try:
                value = _with_timeout(lambda: _finish(fn(*pos, **kw)), seconds)
            finally:
                LAST_CALL["seconds"] = round(_monotonic() - began, 4)
        finally:
            frozen_datetime_module(False)
            SETUP["on"] = setup_was
            _IN_CALL["active"] = False
            sys.settrace(None)
        outcome = {"outcome": "return", "value": lg_codec.canon(value)}
    except GuardBlocked as exc:
        outcome = {"outcome": "blocked", "detail": str(exc)}
    except CallTimeout:
        outcome = {"outcome": "timeout"}
        if DET["on"] and _CLOCK["reads"] >= CLOCK_LOOP_READS:
            outcome["clock_loop"] = _CLOCK["reads"]
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            outcome = {"outcome": "exit", "detail": type(exc).__name__}
        else:
            outcome = {"outcome": "raise", "type": type(exc).__name__, "message": lg_codec._scrub(str(exc))[:500]}
    _note_threads()
    # Beside the outcome, not in it (like the seconds): lets diff.py hedge a difference that is only a clock shift.
    LAST_CALL["clock_reads"] = _CLOCK["reads"] if DET["on"] else None
    if outcome["outcome"] in ("return", "raise"):
        state = _state_after(watched, self_before)
        if state:
            outcome["state"] = state
        if lg_stubs.EVENTS:
            outcome["io"] = list(lg_stubs.EVENTS)
    if len(BLOCKS) > start:
        outcome["blocked_kinds"] = sorted(set(BLOCKS[start:]))
    return outcome


MUTANT_CALL_S = 0.5      # a planted bug often loops forever; such a mutant counts as caught once it times out
MUTANT_BUDGET_S = 10.0   # all mutants of one function together


def _same(a: dict, b: dict) -> bool:
    keys = ("outcome", "value", "type", "message", "state", "io")
    return all(a.get(k) == b.get(k) for k in keys)


def _compile_variant(fn, source: str, name: str):
    """The function compiled again from source in the namespace of the module that defines fn."""
    target = getattr(fn, "__func__", fn)
    namespace = dict(getattr(target, "__globals__", {}))
    exec(compile(source, getattr(target, "__code__", None) and target.__code__.co_filename or "<mutant>", "exec"), namespace)  # noqa: S102
    return namespace[name]


def _run_mutants(fn, spec, make_self, owner, results) -> dict:
    """{"applicable", "total", "killed", "survived": [change, ...]} for the planted bugs in spec["mutants"].

    Only inputs whose real outcome compares (a return or a raise) are used. A mutant is caught as soon as one input
    gives a different outcome, or the mutant runs out of time."""
    plan = spec["mutants"]
    usable = [(i, item, r) for i, (item, r) in enumerate(zip(spec["inputs"], results)) if r.get("outcome") in ("return", "raise")]
    if not usable:
        return {"applicable": False, "reason": "no input ran to completion"}
    if owner is not None and make_self is None:
        return {"applicable": False, "reason": "no instance"}
    try:
        identity = _compile_variant(fn, plan["identity"], plan["name"])
    except BaseException as exc:  # noqa: BLE001
        return {"applicable": False, "reason": f"the function cannot be recompiled on its own ({type(exc).__name__})"}
    for i, item, real in usable:
        if not _same(_call_once(identity, item, make_self, i), real):
            return {"applicable": False, "reason": "the function behaves differently when recompiled on its own"}
    deadline = _monotonic() + MUTANT_BUDGET_S
    killed, survived, total = 0, [], 0
    for m in plan["mutants"]:
        if _monotonic() > deadline:
            break
        total += 1
        try:
            mutant = _compile_variant(fn, m["source"], plan["name"])
        except BaseException:  # noqa: BLE001 - a mutant that does not compile is not a bug the inputs had to catch
            total -= 1
            continue
        caught = False
        for i, item, real in usable:
            got = _call_once(mutant, item, make_self, i, None, MUTANT_CALL_S)
            if got["outcome"] == "timeout" or not _same(got, real):
                caught = True
                break
            if _monotonic() > deadline:
                break
        if caught:
            killed += 1
        else:
            survived.append(m["change"])
    return {"applicable": total > 0, "total": total, "killed": killed, "survived": survived[:5]}


def setup_django(request) -> str | None:
    django_settings = request.get("django_settings")
    if not django_settings:
        return None
    try:
        os.environ["DJANGO_SETTINGS_MODULE"] = django_settings
        with setup_phase():
            import django
            django.setup()
        return None
    except BaseException as exc:  # noqa: BLE001
        return f"{django_settings}: {type(exc).__name__}: {exc}"[:400]


def prepare(request, with_django: bool = True) -> tuple[str, str | None]:
    """Blocks, determinism, import path and working folder for one tree. Returns (root_real, Django setup error)."""
    GUARD_WRITES["allowed"] = True
    # Before the blocks go in: on Python 3.9 finding the temp folder writes a probe file through open().
    SCRATCH["temp"] = os.path.realpath(tempfile.gettempdir()) + os.sep
    use_scratch(request.get("scratch"))
    GUARD_WRITES["allowed"] = False
    install_blocks()
    if request.get("io", "stub") == "stub":
        STUBS["on"] = True
        lg_stubs.install()
        lg_codec.FAKES.update({"db": lg_stubs.FakeConnection, "cursor": lg_stubs.FakeCursor})
    if request.get("deterministic", True):
        install_determinism()
    if HERE in sys.path:
        sys.path.remove(HERE)
    if sys.path and sys.path[0] in ("", HERE):
        sys.path.pop(0)
    root = request["root"]
    os.chdir(root)
    add_paths(request)
    root_real = os.path.realpath(root) + os.sep
    PROJECT["root"] = root_real
    SCRATCH["protect"] = [root_real] + [os.path.realpath(p) + os.sep for p in request.get("protect") or []]
    return root_real, (setup_django(request) if with_django else None)


def add_paths(request) -> None:
    for extra in reversed(request.get("sys_path") or []):
        if extra not in sys.path:
            sys.path.insert(0, extra)


def execute(request, root_real: str, setup_error: str | None) -> dict:
    """Run every requested function. Output of the target code goes nowhere."""
    out = {}
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - devnull is allowed by the guard
    if setup_error:
        # Code that does not need the framework can still be run. Code that does will fail to import below,
        # and the runner adds this as the cause.
        out["__setup_error__"] = setup_error
    for spec in request["functions"]:
        key = f"{spec['module']}:{spec['qualname']}"
        try:
            out[key] = run_capture(spec, root_real) if spec.get("capture") else run_function(spec, root_real)
        except BaseException as exc:  # noqa: BLE001
            out[key] = {"status": "worker_error", "reason": "".join(traceback.format_exception_only(type(exc), exc)).strip()}
    return out


def write_result(out: dict, result_path: str) -> None:
    GUARD_WRITES["allowed"] = True
    with open(result_path, "w") as fh:
        json.dump(out, fh, sort_keys=True)


def main(request_path, result_path):
    with open(request_path) as fh:
        request = json.load(fh)
    try:
        root_real, setup_error = prepare(request)
        write_result(execute(request, root_real, setup_error), result_path)
    finally:
        drop_scratch()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    os._exit(0)
