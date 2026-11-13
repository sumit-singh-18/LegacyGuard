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
import importlib
import inspect
import io
import ipaddress
import itertools
import json
import os
import random
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import types
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lg_codec  # noqa: E402

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
        paths = [a for a in args[:2] if isinstance(a, (str, bytes, int, os.PathLike))]
        if not paths or any(isinstance(p, int) or not _is_scratch(p) for p in paths):
            kind = "file write"
    elif event in _ALWAYS:
        kind = _ALWAYS[event]
        if kind == "native code" and not _IN_CALL["active"]:
            kind = None
        if kind == "network" and event == "socket.connect" and len(args) > 1 and _is_local_socket(args[1]):
            kind = None
    elif event == "socket.getaddrinfo" and args and not (args[0] is None or _is_ip_literal(args[0])):
        kind = "network"
    if kind:
        BLOCKS.append(kind)
        raise GuardBlocked(f"{kind} is blocked during the behavioral diff ({event})")


def _is_local_socket(address) -> bool:
    """A Unix socket path inside the temp folder (multiprocessing helpers, test servers) is not the network."""
    return isinstance(address, (str, bytes)) and _is_scratch(address)


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
    return None, "", f"could not build a {owner.__name__} to call the method on ({'; '.join(tried)})"


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


def _state_after(watched) -> dict | None:
    """What the call left behind in its mutable arguments and in self (FR-14, roadmap B1).

    Old and new code that return the same value can still differ in what they do to a list or an object they were
    given. Immutable values cannot change, so they are left out and the result stays small."""
    state = {}
    for name, value in watched:
        if isinstance(value, _IMMUTABLE) or isinstance(value, enum.Enum):
            continue
        if not isinstance(value, (list, dict, set, bytearray)) and not isinstance(getattr(value, "__dict__", None), dict):
            continue
        try:
            state[name] = lg_codec.canon(value)
        except Exception:  # noqa: BLE001 - an object we cannot read is simply not compared
            continue
    return state or None


# ---- determinism (roadmap B2) ----
# Old and new code are run with the same random seed, the same clock and the same uuid sequence, reset before every
# input. Values that came from randomness or the time then agree whenever the code agrees, so such functions can be
# checked instead of being dropped as non-deterministic. The clock moves forward 1 ms per reading, so code that waits
# for time to pass still finishes.
FROZEN_START = 1705320000.0  # 2024-01-15 12:00:00 UTC
_CLOCK = {"t": FROZEN_START}
_IDS = random.Random(0)
DET = {"on": False}
_REAL = {"dt": _dt.datetime, "date": _dt.date, "time": time.time, "time_ns": time.time_ns,
         "uuid1": uuid.uuid1, "uuid4": uuid.uuid4}


def _tick() -> float:
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
          id(_REAL["time_ns"]): _fake_time_ns, id(_REAL["uuid4"]): _fake_uuid4, id(_REAL["uuid1"]): _fake_uuid1}
_SCANNED: set = set()


def install_determinism() -> None:
    DET["on"] = True
    time.time, time.time_ns = _fake_time, _fake_time_ns
    uuid.uuid4, uuid.uuid1 = _fake_uuid4, _fake_uuid1


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
    _CLOCK["t"] = FROZEN_START
    _IDS.seed(0)
    random.seed(0)
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
    except GuardBlocked as exc:
        return {"status": "import_blocked", "reason": str(exc)}
    except BaseException as exc:  # noqa: BLE001
        return {"status": "import_error", "reason": f"{type(exc).__name__}: {exc}"}
    results = [_call_once(call, item, None, i) for i, item in enumerate(spec["inputs"])]
    return {"status": "ran", "results": results, "lines": None}


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
    trace = _tracer(span, hits) if span else None
    freeze_references(root_real)
    make_self, recipe, no_self = None, "", ""
    if owner is not None and not all(i.get("has_self") for i in spec["inputs"]):
        try:
            make_self, recipe, no_self = _instance_maker(owner, spec)
        except GuardBlocked as exc:
            no_self = f"building a {owner.__name__} was blocked: {exc}"
    fn_deadline = time.monotonic() + FUNCTION_BUDGET_S
    for index, item in enumerate(spec["inputs"]):
        if owner is not None and make_self is None and not item.get("has_self"):
            results.append({"outcome": "setup_failed", "detail": no_self})
            continue
        # After two inputs time out, the rest get a short limit: one slow function must not use up the whole check.
        # After MAX_TIMEOUTS the rest are skipped, not run: they are almost surely as slow, and are reported as such.
        timeouts = sum(1 for r in results if r.get("outcome") in ("timeout", "skipped_slow"))
        if timeouts >= MAX_TIMEOUTS or time.monotonic() > fn_deadline:
            results.append({"outcome": "skipped_slow"})
            continue
        results.append(_call_once(fn, item, make_self, index, trace, CALL_TIMEOUT_S if timeouts < 2 else SHORT_TIMEOUT_S))
    out = {"status": "ran", "results": results, "lines": sorted(hits) if span else None}
    if recipe:
        out["self_recipe"] = recipe
    if spec.get("mutants"):
        out["mutation"] = _run_mutants(fn, spec, make_self, owner, results)
    return out


def _call_once(fn, item, make_self, index: int, trace=None, seconds: float = CALL_TIMEOUT_S) -> dict:
    """One input through fn, with a fresh seed, clock and instance. The outcome as the comparison reads it."""
    start = len(BLOCKS)
    reset_determinism()
    try:
        pos, kw, watched = _build_args(item, make_self, index)
    except BaseException as exc:  # noqa: BLE001
        return {"outcome": "setup_failed", "detail": f"{type(exc).__name__}: {exc}"}
    try:
        sys.settrace(trace)
        _IN_CALL["active"] = True
        frozen_datetime_module(True)
        try:
            value = _with_timeout(lambda: _finish(fn(*pos, **kw)), seconds)
        finally:
            frozen_datetime_module(False)
            _IN_CALL["active"] = False
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
    if outcome["outcome"] in ("return", "raise"):
        state = _state_after(watched)
        if state:
            outcome["state"] = state
    if len(BLOCKS) > start:
        outcome["blocked_kinds"] = sorted(set(BLOCKS[start:]))
    return outcome


MUTANT_CALL_S = 0.5      # a planted bug often loops forever; such a mutant counts as caught once it times out
MUTANT_BUDGET_S = 10.0   # all mutants of one function together


def _same(a: dict, b: dict) -> bool:
    keys = ("outcome", "value", "type", "message", "state")
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
    deadline = time.monotonic() + MUTANT_BUDGET_S
    killed, survived, total = 0, [], 0
    for m in plan["mutants"]:
        if time.monotonic() > deadline:
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
            if time.monotonic() > deadline:
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
    GUARD_WRITES["allowed"] = False
    install_blocks()
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
            out[key] = run_function(spec, root_real)
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
    root_real, setup_error = prepare(request)
    write_result(execute(request, root_real, setup_error), result_path)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
    sys.stdout.flush()
    os._exit(0)
