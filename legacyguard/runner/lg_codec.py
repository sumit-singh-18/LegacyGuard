"""Value encoding shared by the guard and the target interpreter. Standard library only.

encode_input: reversible, for function arguments. canon: never fails, deterministic, for comparing results.
"""
from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import decimal
import enum
import importlib
import json
import math
import pickle
import re
import uuid

MAX_DEPTH = 8
MAX_ITEMS = 2000
MAX_PICKLE = 20000
_ADDR = re.compile(r"0x[0-9a-fA-F]{6,}")


class Unencodable(Exception):
    pass


def _scrub(text: str) -> str:
    return _ADDR.sub("0x…", text)


def _simple(value):
    """Tagged form of an atomic value, or None if it is not atomic."""
    if value is None or isinstance(value, (bool, str)):
        return {"t": "v", "v": value}
    if isinstance(value, enum.Enum):
        return {"t": "enum", "cls": _class_ref(type(value)), "v": value.name}
    if isinstance(value, int):
        return {"t": "v", "v": value} if abs(value) < 2**53 else {"t": "bigint", "v": str(value)}
    if isinstance(value, float):
        return {"t": "float", "v": repr(value)}
    if isinstance(value, decimal.Decimal):
        return {"t": "Decimal", "v": str(value)}
    if isinstance(value, dt.datetime):
        return {"t": "datetime", "v": value.isoformat()}
    if isinstance(value, dt.date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, dt.time):
        return {"t": "time", "v": value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {"t": "timedelta", "v": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, uuid.UUID):
        return {"t": "uuid", "v": str(value)}
    if isinstance(value, (bytes, bytearray)):
        return {"t": "bytes", "v": base64.b64encode(bytes(value)).decode()}
    return None


def _class_ref(cls) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _load_class(ref: str):
    module, qual = ref.split(":")
    obj = importlib.import_module(module)
    for part in qual.split("."):
        obj = getattr(obj, part)
    return obj


def _key(encoded) -> str:
    return json.dumps(encoded, sort_keys=True, ensure_ascii=False)


def encode_input(value, depth: int = 0):
    """Reversible encoding. Falls back to pickle for rich objects; raises Unencodable otherwise."""
    if depth > MAX_DEPTH:
        raise Unencodable("too deep")
    simple = _simple(value)
    if simple is not None:
        return simple
    t = type(value)
    if t in (list, tuple):
        if len(value) > MAX_ITEMS:
            raise Unencodable("too large")
        return {"t": t.__name__, "v": [encode_input(v, depth + 1) for v in value]}
    if t in (set, frozenset):
        return {"t": t.__name__, "v": sorted((encode_input(v, depth + 1) for v in value), key=_key)}
    if t is dict:
        if len(value) > MAX_ITEMS:
            raise Unencodable("too large")
        return {"t": "dict", "v": [[encode_input(k, depth + 1), encode_input(v, depth + 1)] for k, v in value.items()]}
    try:
        blob = pickle.dumps(value, protocol=4)
    except Exception as exc:
        raise Unencodable(f"{t.__name__}: {exc}") from exc
    if len(blob) > MAX_PICKLE:
        raise Unencodable(f"{t.__name__}: pickle too large")
    return {"t": "pickle", "cls": _class_ref(t), "v": base64.b64encode(blob).decode()}


def decode(enc):
    t = enc["t"]
    v = enc.get("v")
    if t == "v":
        return v
    if t == "bigint":
        return int(v)
    if t == "float":
        return float(v)
    if t == "Decimal":
        return decimal.Decimal(v)
    if t == "datetime":
        return dt.datetime.fromisoformat(v)
    if t == "date":
        return dt.date.fromisoformat(v)
    if t == "time":
        return dt.time.fromisoformat(v)
    if t == "timedelta":
        return dt.timedelta(days=v[0], seconds=v[1], microseconds=v[2])
    if t == "uuid":
        return uuid.UUID(v)
    if t == "bytes":
        return base64.b64decode(v)
    if t == "list":
        return [decode(x) for x in v]
    if t == "tuple":
        return tuple(decode(x) for x in v)
    if t == "set":
        return {decode(x) for x in v}
    if t == "frozenset":
        return frozenset(decode(x) for x in v)
    if t == "dict":
        return {decode(k): decode(x) for k, x in v}
    if t == "enum":
        return _load_class(enc["cls"])[v]
    if t == "pickle":
        return pickle.loads(base64.b64decode(v))  # noqa: S301 - written by this guard during the developer's own test run
    raise ValueError(f"unknown tag {t}")


def canon(value, depth: int = 0, seen=None):
    """Deterministic comparable form. Sets and dicts are order-independent; object ids are scrubbed."""
    seen = seen if seen is not None else set()
    simple = _simple(value)
    if simple is not None:
        if simple["t"] == "v" and isinstance(simple["v"], str):
            return {"t": "v", "v": _scrub(simple["v"])}
        return simple
    if depth > MAX_DEPTH:
        return {"t": "deep"}
    if id(value) in seen:
        return {"t": "cycle"}
    seen = seen | {id(value)}
    if isinstance(value, (list, tuple)):
        name = "tuple" if isinstance(value, tuple) else "list"
        return {"t": name, "v": [canon(v, depth + 1, seen) for v in list(value)[:MAX_ITEMS]]}
    if isinstance(value, (set, frozenset)):
        return {"t": "set", "v": sorted((canon(v, depth + 1, seen) for v in value), key=_key)}
    if isinstance(value, dict):
        items = [[canon(k, depth + 1, seen), canon(v, depth + 1, seen)] for k, v in value.items()]
        return {"t": "dict", "v": sorted(items, key=lambda kv: _key(kv[0]))}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        state = {f.name: getattr(value, f.name, None) for f in dataclasses.fields(value)}
        return {"t": "obj", "cls": _class_ref(type(value)), "state": canon(state, depth + 1, seen)}
    if isinstance(value, type):
        return {"t": "class", "v": _class_ref(value)}
    if callable(value) and hasattr(value, "__qualname__"):
        return {"t": "callable", "v": f"{getattr(value, '__module__', '?')}:{value.__qualname__}"}
    state = getattr(value, "__dict__", None)
    if isinstance(state, dict):
        kept = {k: v for k, v in state.items() if k != "_state" and not k.startswith("_prefetched")}
        return {"t": "obj", "cls": _class_ref(type(value)), "state": canon(kept, depth + 1, seen)}
    return {"t": "repr", "cls": _class_ref(type(value)), "v": _scrub(repr(value))[:2000]}


def display(enc, plain_decimal: bool = False) -> str:
    """Short Python-like text for a tagged value."""
    t = enc.get("t")
    v = enc.get("v")
    if t == "v":
        return repr(v)
    if t == "bigint":
        return v
    if t == "float":
        return v
    if t == "Decimal":
        return v if plain_decimal else f'Decimal("{v}")'
    if t == "date":
        return f'date.fromisoformat("{v}")'
    if t == "datetime":
        return f'datetime.fromisoformat("{v}")'
    if t in ("list", "tuple", "set", "frozenset"):
        inner = ", ".join(display(x, plain_decimal) for x in v)
        return {"list": f"[{inner}]", "tuple": f"({inner})"}.get(t, f"{{{inner}}}")
    if t == "dict":
        return "{" + ", ".join(f"{display(k, plain_decimal)}: {display(x, plain_decimal)}" for k, x in v) + "}"
    if t == "enum":
        return f"{enc['cls'].split(':')[-1]}.{v}"
    if t == "pickle":
        return f"<{enc['cls'].split(':')[-1]} from a test>"
    if t == "obj":
        return f"<{enc['cls'].split(':')[-1]}>"
    if t == "repr":
        return v
    if t == "exception":
        return f"raises {enc['type']}: {enc['message']}"
    return str(v) if v is not None else f"<{t}>"


def is_finite_number(value) -> bool:
    return not (isinstance(value, float) and (math.isnan(value) or math.isinf(value)))
