"""Why two runs of the same old code disagreed on one input (Task 6.8d).

compare_one runs the old code twice (OLD-A, OLD-B) and drops inputs on which the two runs differ. This module diffs
the two canonical outcomes and names what differed: object addresses or ids, thread or process ids, thread names,
time readings, uuids, random bytes, numbers or tokens, set or dict iteration order, temporary file names, float noise,
or "unclassified values". It only explains; it never changes what is compared.

Each run's worker reports an "env" snapshot (lg_worker.env_snapshot: its real pid, the thread ids it saw, the span of
each real clock, its scratch and temp folders). A differing value is put down to a thread id, a process id or a time
reading only when each run's value matches that run's own snapshot.
"""
from __future__ import annotations

import difflib
import json
import re

ADDRESSES = "object addresses"
IDS = "values that look like object ids"  # heuristic labels say so: they explain, never excuse
PROCESS_IDS = "process ids"
THREAD_IDS = "thread ids"
THREAD_NAMES = "thread names"
TIME = "time readings"
UUIDS = "uuids"
RANDOM_BYTES = "bytes that look random"
RANDOM_NUMBERS = "values that look like random numbers"
RANDOM_TOKENS = "values that look like random tokens"
ORDER = "set or dict iteration order"
TEMP_NAMES = "temporary file names"
FLOAT_NOISE = "float noise"
OTHER = "unclassified values"
UNFINISHED = "a run that did not finish"

FLOAT_NOISE_REL = 1e-9
CLOCK_SLACK_S = 1.0
_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_HEX_ADDR = re.compile(r"0x[0-9a-fA-F]+")
_ISO_TIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d(\.\d+)?)?")
_TEMP_WORD = re.compile(r"(^|[/\\_.-])(tmp|temp)[A-Za-z0-9_]{6,}", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{8,}")
_HEX = re.compile(r"[0-9a-fA-F]{8,}")
ID_MIN = 2 ** 32        # a CPython object address on a 64-bit build is at least this large and 16-aligned
HEAP_SPAN = 2 ** 40     # and lies within this distance of the run's own sample addresses, when the run reported them
RANDOM_MIN_BYTES = 8
RANDOM_MIN_DIGITS = 12  # random.random() gives about 17 significant digits; 0.25 or 0.5 is not a random-looking number
_NUMBER = re.compile(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?")
MAX_LEAVES = 50


def _key(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


class _Env:
    def __init__(self, env: dict | None):
        env = env or {}
        self.pids = {v for v in (env.get("pid"), env.get("ppid")) if isinstance(v, int)}
        self.threads = {v for v in env.get("threads") or [] if isinstance(v, int)}
        self.clocks = [tuple(span) for span in (env.get("clocks") or {}).values() if isinstance(span, (list, tuple)) and len(span) == 2]
        self.paths = [p for p in env.get("paths") or [] if isinstance(p, str) and p]
        self.heap = [v for v in env.get("heap") or [] if isinstance(v, int)]

    def near_heap(self, value: int) -> bool:
        return not self.heap or any(abs(value - h) < HEAP_SPAN for h in self.heap)

    def in_clock(self, value: float) -> bool:
        for lo, hi in self.clocks:
            for scale in (1, 1000, 1_000_000, 1_000_000_000):
                if lo * scale - CLOCK_SLACK_S * scale <= value <= hi * scale + CLOCK_SLACK_S * scale:
                    return True
        return False


def _number(a, b, ea: _Env, eb: _Env) -> str:
    if isinstance(a, bool) or isinstance(b, bool):
        return OTHER
    if isinstance(a, int) and isinstance(b, int):
        if a in ea.pids and b in eb.pids:
            return PROCESS_IDS
        if a in ea.threads and b in eb.threads:
            return THREAD_IDS
    if ea.clocks and eb.clocks and ea.in_clock(a) and eb.in_clock(b) and (abs(a) > 1 or abs(b) > 1):
        return TIME
    if isinstance(a, float) or isinstance(b, float):
        x, y = float(a), float(b)
        if x == x and y == y and abs(x - y) <= FLOAT_NOISE_REL * max(abs(x), abs(y), 1e-300):
            return FLOAT_NOISE
        if 0 <= x < 1 and 0 <= y < 1 and _digits(x) >= RANDOM_MIN_DIGITS and _digits(y) >= RANDOM_MIN_DIGITS:
            return RANDOM_NUMBERS
        return OTHER
    # id() of an object: an address-sized, 16-aligned number near the run's own heap.
    if a >= ID_MIN and b >= ID_MIN and a % 16 == 0 and b % 16 == 0 and ea.near_heap(a) and eb.near_heap(b):
        return IDS
    return OTHER


def _digits(x: float) -> int:
    return len(repr(x).lstrip("-0.").replace(".", "").split("e")[0])


def _random_token(word: str) -> bool:
    """A word that looks generated: hex, or letters mixed with digits, at least 8 characters. Plain words do not."""
    if not _TOKEN.fullmatch(word):
        return False
    return any(c.isdigit() for c in word) and any(c.isalpha() for c in word)


def _word_at(text: str, start: int, end: int) -> str:
    lo, hi = start, end
    while lo > 0 and not text[lo - 1].isspace() and text[lo - 1] not in "'\"<>()[]{},;=":
        lo -= 1
    while hi < len(text) and not text[hi].isspace() and text[hi] not in "'\"<>()[]{},;=":
        hi += 1
    return text[lo:hi]


def _segment(a: str, b: str, wa: str, wb: str, before: str, around: tuple[str, str], ea: _Env, eb: _Env) -> str:
    """The cause of one differing piece of two strings; wa/wb are the whole words around it, before the text before it
    in the first string, around the text near it in each string."""
    if _HEX_ADDR.search(wa) and _HEX_ADDR.search(wb):
        return ADDRESSES
    if _UUID.search(wa) and _UUID.search(wb):
        return UUIDS
    if _ISO_TIME.search(around[0]) and _ISO_TIME.search(around[1]):
        return TIME
    if (any(p in wa for p in ea.paths) and any(p in wb for p in eb.paths)) or (_TEMP_WORD.search(wa) and _TEMP_WORD.search(wb)):
        return TEMP_NAMES
    if re.search(r"Thread-\d*$", before) and a.isdigit() and b.isdigit():
        return THREAD_NAMES
    na, nb = _NUMBER.fullmatch(wa) and wa, _NUMBER.fullmatch(wb) and wb
    if not (na and nb):
        na, nb = _NUMBER.fullmatch(a) and a, _NUMBER.fullmatch(b) and b
    if na and nb:
        try:
            x = int(na) if re.fullmatch(r"[-+]?\d+", na) else float(na)
            y = int(nb) if re.fullmatch(r"[-+]?\d+", nb) else float(nb)
        except ValueError:
            return OTHER
        return _number(x, y, ea, eb)
    if len(wa) == len(wb) and _random_token(wa) and _random_token(wb):
        return RANDOM_TOKENS
    return OTHER


def _text(a: str, b: str, ea: _Env, eb: _Env) -> set[str]:
    ta, tb = re.findall(r"\w+|\W", a), re.findall(r"\w+|\W", b)
    if sorted(ta) == sorted(tb) and len(ta) > 1:
        return {ORDER}  # the same pieces in another order: a string built from a set or dict
    causes = set()
    pos_a, pos_b = [0], [0]
    for t in ta:
        pos_a.append(pos_a[-1] + len(t))
    for t in tb:
        pos_b.append(pos_b[-1] + len(t))
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        sa, sb = "".join(ta[i1:i2]), "".join(tb[j1:j2])
        wa = _word_at(a, pos_a[i1], pos_a[i2]) or sa
        wb = _word_at(b, pos_b[j1], pos_b[j2]) or sb
        around = (a[max(0, pos_a[i1] - 20):pos_a[i2] + 12], b[max(0, pos_b[j1] - 20):pos_b[j2] + 12])
        causes.add(_segment(sa, sb, wa, wb, a[:pos_a[i1]], around, ea, eb))
        if len(causes) > 4:
            break
    return causes or {OTHER}


def _walk(a, b, ea: _Env, eb: _Env, out: set, budget: list) -> None:
    if budget[0] <= 0 or _key(a) == _key(b):
        return
    budget[0] -= 1
    if isinstance(a, dict) and isinstance(b, dict) and "t" in a and "t" in b:  # a tagged canonical value
        ta, tb = a.get("t"), b.get("t")
        if ta != tb:
            out.add(OTHER)
            return
        va, vb = a.get("v"), b.get("v")
        if ta == "v":
            if isinstance(va, str) and isinstance(vb, str):
                out.update(_text(va, vb, ea, eb))
            elif isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                out.add(_number(va, vb, ea, eb))
            else:
                out.add(OTHER)
        elif ta in ("float", "bigint"):
            try:
                x, y = (float(va), float(vb)) if ta == "float" else (int(va), int(vb))
            except (TypeError, ValueError):
                out.add(OTHER)
                return
            out.add(_number(x, y, ea, eb))
        elif ta in ("datetime", "date", "time", "timedelta"):
            out.add(TIME)
        elif ta == "uuid":
            out.add(UUIDS)
        elif ta in ("bytes", "bytearray", "memoryview"):
            long_enough = isinstance(va, str) and isinstance(vb, str) and len(va) == len(vb) and len(va) * 3 // 4 >= RANDOM_MIN_BYTES
            out.add(RANDOM_BYTES if long_enough else OTHER)
        elif ta == "repr":
            if a.get("cls") != b.get("cls"):
                out.add(OTHER)
            else:
                out.update(_text(str(va), str(vb), ea, eb))
        elif ta in ("list", "tuple", "set", "dict") and isinstance(va, list) and isinstance(vb, list):
            _seq(va, vb, ea, eb, out, budget)
        elif ta == "obj":
            if a.get("cls") != b.get("cls"):
                out.add(OTHER)
            else:
                _walk(a.get("state"), b.get("state"), ea, eb, out, budget)
        else:
            _plain(a, b, ea, eb, out, budget)
        return
    if isinstance(a, list) and isinstance(b, list):
        _seq(a, b, ea, eb, out, budget)
        return
    if isinstance(a, dict) and isinstance(b, dict):
        _plain(a, b, ea, eb, out, budget)
        return
    if isinstance(a, str) and isinstance(b, str):
        out.update(_text(a, b, ea, eb))
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        out.add(_number(a, b, ea, eb))
    else:
        out.add(OTHER)


def _seq(a: list, b: list, ea, eb, out, budget) -> None:
    if len(a) != len(b):
        out.add(OTHER)
        return
    if sorted(_key(x) for x in a) == sorted(_key(x) for x in b):
        out.add(ORDER)  # the same items in another order
        return
    for x, y in zip(a, b):
        _walk(x, y, ea, eb, out, budget)


def _plain(a: dict, b: dict, ea, eb, out, budget) -> None:
    if a.keys() != b.keys():
        out.add(OTHER)
        return
    for k in a:
        _walk(a[k], b[k], ea, eb, out, budget)


def _where_state(name: str) -> str:
    return "self" if name == "self" else f"argument {name}"


def causes(ca: dict | None, cb: dict | None, env_a: dict | None = None, env_b: dict | None = None) -> list[tuple[str, str]]:
    """[(cause, where)] for two comparable outcomes (diff._comparable) of the same input from OLD-A and OLD-B.
    where is "the return value", "the exception", "self", "argument <name>", "the recorded calls" or "the outcome"."""
    if ca is None or cb is None:
        return [(UNFINISHED, "the outcome")]
    ea, eb = _Env(env_a), _Env(env_b)
    found: list[tuple[str, str]] = []

    def add(where: str, a, b):
        out: set = set()
        _walk(a, b, ea, eb, out, [MAX_LEAVES])
        found.extend((c, where) for c in sorted(out))

    if ca["k"] != cb["k"]:
        return [(OTHER, "the outcome")]
    if ca["k"] == "return":
        add("the return value", ca.get("v"), cb.get("v"))
    else:
        if ca.get("type") != cb.get("type"):
            found.append((OTHER, "the exception"))
        else:
            add("the exception", ca.get("message"), cb.get("message"))
    sa, sb = ca.get("s") or {}, cb.get("s") or {}
    for name in sorted(set(sa) | set(sb)):
        if name not in sa or name not in sb:
            found.append((OTHER, _where_state(name)))
        else:
            add(_where_state(name), sa[name], sb[name])
    if _key(ca.get("io")) != _key(cb.get("io")):
        add("the recorded calls", ca.get("io") or [], cb.get("io") or [])
    return found or [(OTHER, "the outcome")]


def summary(counts: dict[str, int], limit: int = 3) -> str:
    """'time readings in self (3), thread ids in self (3)' from {"time readings in self": 3, ...}, most frequent first."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    text = ", ".join(f"{label} ({n})" for label, n in ranked[:limit])
    if len(ranked) > limit:
        text += f" and {len(ranked) - limit} more"
    return text


def numbers_only(a, b) -> bool:
    """True when two canonical values differ and every differing piece is a number (a numeric leaf, a datetime, or a
    numeric run inside an otherwise equal string)."""
    if _key(a) == _key(b):
        return False
    return _numeric_diff(a, b)


_NUMERIC_TAGS = ("float", "bigint", "datetime", "date", "time", "timedelta")


def _numeric_diff(a, b) -> bool:
    if _key(a) == _key(b):
        return True
    if isinstance(a, dict) and isinstance(b, dict) and "t" in a and "t" in b and a.get("t") == b.get("t"):
        t, va, vb = a["t"], a.get("v"), b.get("v")
        if t in _NUMERIC_TAGS:
            return True
        if t == "v":
            if isinstance(va, bool) or isinstance(vb, bool):
                return False
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                return True
            if isinstance(va, str) and isinstance(vb, str):
                return _NUMBER.sub("#", va) == _NUMBER.sub("#", vb)
            return False
        if t == "repr":
            return a.get("cls") == b.get("cls") and _NUMBER.sub("#", str(va)) == _NUMBER.sub("#", str(vb))
        if t in ("obj",):
            return a.get("cls") == b.get("cls") and _numeric_diff(a.get("state"), b.get("state"))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_numeric_diff(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_numeric_diff(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return True
    if isinstance(a, str) and isinstance(b, str):
        return _NUMBER.sub("#", a) == _NUMBER.sub("#", b)
    return False
