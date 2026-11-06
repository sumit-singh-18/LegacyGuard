"""Runs in the target project's interpreter. Standard library and lg_codec only.

Usage: python -B lg_worker.py <request.json> <result.json>
Blocks network, databases, file writes and subprocesses before any target code is imported (D-012, NFR-05).
"""
from __future__ import annotations

import asyncio
import builtins
import dis
import importlib
import inspect
import io
import ipaddress
import itertools
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lg_codec  # noqa: E402

BLOCKS: list[str] = []
CALL_TIMEOUT_S = 5


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
    real_open = builtins.open
    file_block = _blocker("file write")

    def guarded_open(file, mode="r", *args, **kwargs):
        if (any(c in mode for c in "wax+") and str(file) not in (os.devnull,) and not GUARD_WRITES.get("allowed")
                and not _is_scratch(file)):
            file_block()
        return real_open(file, mode, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_open
    proc = _blocker("subprocess")
    subprocess.Popen.__init__ = proc  # patch the method: libraries subclass Popen at import time
    subprocess.run = proc
    os.system = proc


GUARD_WRITES = {"allowed": False}
SCRATCH = {"temp": "", "protect": []}


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


def _alarm(signum, frame):
    raise CallTimeout()


def _with_timeout(fn):
    if hasattr(signal, "setitimer"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, CALL_TIMEOUT_S)
    try:
        return fn()
    finally:
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)


def _resolve(module, qualname):
    """(callable or None, needs_instance class or None, reason)."""
    try:
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


def _tracer(span, hits: set):
    """Record executed lines inside the function's span only. Every other frame is left untraced, so the cost stays small."""
    filename, first, last = span

    def local(frame, event, arg):
        if event == "line":
            hits.add(frame.f_lineno)
        return local

    def enter(frame, event, arg):
        code = frame.f_code
        if code.co_filename == filename and first <= code.co_firstlineno <= last:
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


def _build_args(item, owner):
    pos, kw = [], {}
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
    if owner is not None and not item.get("has_self"):
        pos.insert(0, owner())
    return pos, kw


def run_function(spec, root_real):
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
    trace = _tracer(span, hits) if span else None
    for item in spec["inputs"]:
        start = len(BLOCKS)
        try:
            pos, kw = _build_args(item, owner)
        except BaseException as exc:  # noqa: BLE001
            results.append({"outcome": "setup_failed", "detail": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            sys.settrace(trace)
            try:
                value = _with_timeout(lambda: _finish(fn(*pos, **kw)))  # noqa: B023
            finally:
                sys.settrace(None)
            outcome = {"outcome": "return", "value": lg_codec.canon(value)}
        except GuardBlocked as exc:
            outcome = {"outcome": "blocked", "detail": str(exc)}
        except CallTimeout:
            outcome = {"outcome": "timeout"}
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                outcome = {"outcome": "exit", "detail": type(exc).__name__}
            else:
                outcome = {"outcome": "raise", "type": type(exc).__name__, "message": lg_codec._scrub(str(exc))[:500]}
        if len(BLOCKS) > start:
            outcome["blocked_kinds"] = sorted(set(BLOCKS[start:]))
        results.append(outcome)
    return {"status": "ran", "results": results, "lines": sorted(hits) if span else None}


def main(request_path, result_path):
    GUARD_WRITES["allowed"] = True
    with open(request_path) as fh:
        request = json.load(fh)
    # Before the blocks go in: on Python 3.9 finding the temp folder writes a probe file through open().
    SCRATCH["temp"] = os.path.realpath(tempfile.gettempdir()) + os.sep
    GUARD_WRITES["allowed"] = False
    install_blocks()
    if HERE in sys.path:
        sys.path.remove(HERE)
    if sys.path and sys.path[0] in ("", HERE):
        sys.path.pop(0)
    root = request["root"]
    os.chdir(root)
    for extra in reversed(request.get("sys_path") or []):
        sys.path.insert(0, extra)
    root_real = os.path.realpath(root) + os.sep
    SCRATCH["protect"] = [root_real] + [os.path.realpath(p) + os.sep for p in request.get("protect") or []]
    setup_error = None
    django_settings = request.get("django_settings")
    if django_settings:
        try:
            os.environ["DJANGO_SETTINGS_MODULE"] = django_settings
            import django
            django.setup()
        except BaseException as exc:  # noqa: BLE001
            setup_error = f"{type(exc).__name__}: {exc}"
    out = {}
    real_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - devnull is allowed by the guard
    if setup_error:
        # Code that does not need the framework can still be run. Code that does will fail to import below,
        # and the runner adds this as the cause.
        out["__setup_error__"] = f"{django_settings}: {setup_error}"[:400]
    for spec in request["functions"]:
        key = f"{spec['module']}:{spec['qualname']}"
        try:
            out[key] = run_function(spec, root_real)
        except BaseException as exc:  # noqa: BLE001
            out[key] = {"status": "worker_error", "reason": "".join(traceback.format_exception_only(type(exc), exc)).strip()}
    GUARD_WRITES["allowed"] = True
    with open(result_path, "w") as fh:
        json.dump(out, fh, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    os._exit(0)
