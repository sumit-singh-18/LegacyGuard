"""pytest plugin: record the arguments real tests pass to functions in the guarded code (FR-14 step 1).

Runs in the target interpreter during `/legacyguard:index`. Standard library and lg_codec only.
"""
from __future__ import annotations

import json
import os
import sys
import itertools
import threading

import lg_codec

ROOT = os.path.realpath(os.environ.get("LG_HARVEST_ROOT", os.getcwd())) + os.sep
OUT = os.environ.get("LG_HARVEST_OUT", "")
MAX_PER_FUNCTION = int(os.environ.get("LG_HARVEST_MAX", "40"))
_SKIP = (os.sep + "tests" + os.sep, os.sep + "test" + os.sep, os.sep + ".venv" + os.sep, os.sep + "venv" + os.sep,
         os.sep + "site-packages" + os.sep, os.sep + ".legacyguard" + os.sep, os.sep + "migrations" + os.sep)
MAX_TYPED_CALLS = 200
_code_rel: dict = {}
_records: dict = {}
_lock = threading.Lock()


def _rel(code):
    try:
        return _code_rel[code]
    except KeyError:
        pass
    path = os.path.realpath(code.co_filename) if code.co_filename and not code.co_filename.startswith("<") else ""
    keep = path.startswith(ROOT) and not any(s in path for s in _SKIP) and not os.path.basename(path).startswith("test_") \
        and os.path.basename(path) != "conftest.py"
    rel = path[len(ROOT):].replace(os.sep, "/") if keep else None
    _code_rel[code] = rel
    return rel


_SIMPLE = {bool: "bool", int: "int", float: "float", str: "str", bytes: "bytes", type(None): "None"}


def type_name(value, depth: int = 0) -> str:
    """A type hint for one runtime value, in the spelling the input builder reads: int, Decimal, list[int],
    tuple[float,float], dict[str,int]. Other classes are "class:module:qualname", which the builder skips."""
    t = type(value)
    if t in _SIMPLE:
        return _SIMPLE[t]
    mod, name = getattr(t, "__module__", ""), t.__qualname__
    if mod == "decimal" and name == "Decimal":
        return "Decimal"
    if mod == "datetime" and name in ("date", "datetime"):
        return name
    if depth == 0 and t in (list, set, frozenset):
        inner = {type_name(v, 1) for v in itertools.islice(value, 20)}
        return f"{t.__name__}[{inner.pop()}]" if len(inner) == 1 else t.__name__
    if depth == 0 and t is tuple and 0 < len(value) <= 4:
        parts = [type_name(v, 1) for v in value]
        return "tuple[" + ",".join(parts) + "]" if all(p in _SIMPLE.values() or p in ("Decimal", "date", "datetime") for p in parts) else "tuple"
    if depth == 0 and t is dict:
        keys = {type_name(k, 1) for k in itertools.islice(value.keys(), 20)}
        vals = {type_name(v, 1) for v in itertools.islice(value.values(), 20)}
        return f"dict[{keys.pop()},{vals.pop()}]" if len(keys) == 1 and len(vals) == 1 else "dict"
    if t in (list, tuple, set, frozenset, dict):
        return t.__name__
    return f"class:{mod}:{name}"


def _profile(frame, event, arg):
    if event != "call":
        return
    code = frame.f_code
    rel = _rel(code)
    if rel is None:
        return
    qual = getattr(code, "co_qualname", code.co_name)
    if "<" in qual:  # lambdas, comprehensions, local functions
        return
    key = f"{rel}::{qual}"
    rec = _records.get(key)
    if rec is not None and len(rec["inputs"]) >= MAX_PER_FUNCTION and rec["typed_calls"] >= MAX_TYPED_CALLS:
        return
    names = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    flags = code.co_flags
    var = code.co_argcount + code.co_kwonlyargcount
    varargs = code.co_varnames[var] if flags & 0x04 else None
    varkw = code.co_varnames[var + (1 if flags & 0x04 else 0)] if flags & 0x08 else None
    loc = frame.f_locals
    with _lock:
        rec = _records.setdefault(key, {"inputs": [], "seen": set(), "blocked": {}, "types": {}, "typed_calls": 0})
        if rec["typed_calls"] < MAX_TYPED_CALLS:
            # Types are recorded for every call, even ones whose values cannot be stored: untyped legacy code then
            # still gets inputs of the right kind (roadmap B4, after MonkeyType).
            rec["typed_calls"] += 1
            for n in names:
                if n in loc and n not in ("self", "cls"):
                    seen_types = rec["types"].setdefault(n, {})
                    tn = type_name(loc[n])
                    seen_types[tn] = seen_types.get(tn, 0) + 1
        if len(rec["inputs"]) >= MAX_PER_FUNCTION:
            return
    enc, blocked = [], []
    for i, n in enumerate(names):
        try:
            enc.append({"name": n, "kind": "pos" if i < code.co_argcount else "kw", "value": lg_codec.encode_input(loc[n])})
        except Exception:  # noqa: BLE001 - a value we cannot store just means this call is not replayable
            blocked.append(n)
    if varargs:
        try:
            enc.append({"name": varargs, "kind": "varargs", "value": lg_codec.encode_input(list(loc[varargs]))})
        except Exception:  # noqa: BLE001
            blocked.append(varargs)
    if varkw:
        try:
            enc.append({"name": varkw, "kind": "varkw", "value": lg_codec.encode_input(dict(loc[varkw]))})
        except Exception:  # noqa: BLE001
            blocked.append(varkw)
    with _lock:
        for n in blocked:
            rec["blocked"][n] = rec["blocked"].get(n, 0) + 1
        if not blocked:
            sig = json.dumps(enc, sort_keys=True)
            if sig not in rec["seen"]:
                rec["seen"].add(sig)
                rec["inputs"].append(enc)


def pytest_configure(config):
    threading.setprofile(_profile)
    sys.setprofile(_profile)


def pytest_unconfigure(config):
    sys.setprofile(None)
    threading.setprofile(None)
    if OUT:
        data = {k: {"inputs": v["inputs"], "blocked": v["blocked"], "types": v["types"]} for k, v in _records.items()}
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, OUT)
