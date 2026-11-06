"""pytest plugin: record the arguments real tests pass to functions in the guarded code (FR-14 step 1).

Runs in the target interpreter during `/legacyguard:index`. Standard library and lg_codec only.
"""
from __future__ import annotations

import json
import os
import sys
import threading

import lg_codec

ROOT = os.path.realpath(os.environ.get("LG_HARVEST_ROOT", os.getcwd())) + os.sep
OUT = os.environ.get("LG_HARVEST_OUT", "")
MAX_PER_FUNCTION = int(os.environ.get("LG_HARVEST_MAX", "40"))
_SKIP = (os.sep + "tests" + os.sep, os.sep + "test" + os.sep, os.sep + ".venv" + os.sep, os.sep + "venv" + os.sep,
         os.sep + "site-packages" + os.sep, os.sep + ".legacyguard" + os.sep, os.sep + "migrations" + os.sep)
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
    if rec is not None and len(rec["inputs"]) >= MAX_PER_FUNCTION:
        return
    names = code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]
    flags = code.co_flags
    var = code.co_argcount + code.co_kwonlyargcount
    varargs = code.co_varnames[var] if flags & 0x04 else None
    varkw = code.co_varnames[var + (1 if flags & 0x04 else 0)] if flags & 0x08 else None
    loc = frame.f_locals
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
        rec = _records.setdefault(key, {"inputs": [], "seen": set(), "blocked": {}})
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
        data = {k: {"inputs": v["inputs"], "blocked": v["blocked"]} for k, v in _records.items()}
        tmp = OUT + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, OUT)
