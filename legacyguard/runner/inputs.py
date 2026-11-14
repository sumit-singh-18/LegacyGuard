"""Inputs for the behavioral diff: harvested from real tests first, then Hypothesis from hints and defaults (FR-14 step 1)."""
from __future__ import annotations

import ast
import contextlib
import datetime as dt
import json
import os
import re
import signal
import threading
import time
import warnings
from decimal import Decimal
from pathlib import Path

from runner import lg_codec

DB_PARAM_NAMES = {"conn", "connection", "cursor", "cur", "db", "session", "engine", "transaction", "tx"}
MAX_GENERATED = 150
# Letters, digits and the punctuation that parsers branch on. A narrower alphabet missed every "/" case on a real repo.
TEXT_ALPHABET = "abcdefgh XYZ-_0123456789/.:()@,;=+#%&?!'\"[]{}<>*~"


def _hypothesis(state_dir: Path):
    os.environ.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", str(Path(state_dir) / ".hypothesis"))
    import hypothesis
    from hypothesis import strategies as st
    return hypothesis, st


def _with_specials(st, base, specials: list):
    """One draw in ten is an unusual value; the rest are as before."""
    return st.integers(0, 9).flatmap(lambda i: st.sampled_from(specials) if i == 0 else base)


def _with_pool(st, base, values: list):
    """Half the draws come from the project's own example values, when it has any of the right kind."""
    return st.one_of(st.sampled_from(values), base) if values else base


SEQUENCES = ("list", "List", "Sequence", "MutableSequence", "Iterable", "Iterator", "Collection", "KeysView", "ValuesView")
SETS = ("set", "Set", "frozenset", "FrozenSet", "AbstractSet")
MAPPINGS = ("dict", "Dict", "Mapping", "MutableMapping")
TYPE_VARIABLE = re.compile(r"_?[A-Z][A-Za-z0-9]{0,2}|_T\w*|Any|object|Hashable")


def _is_type_variable(ann: str) -> bool:
    return bool(TYPE_VARIABLE.fullmatch(ann))


def _split_top(text: str, sep: str) -> list[str]:
    """Split on a separator that is not inside brackets: 'tuple[int,int]|None' on '|' has two parts."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _collection_strategy(ann: str, st, pool: list):
    """Strategies for the shapes real signatures use: tuple[float, float] ranges, list[T], Iterable[str], unions (FR-40)."""
    union = _split_top(ann, "|")
    if len(union) > 1:
        built = [s for s in (_strategy_for(part, None, st, pool) for part in union) if s is not None]
        return st.one_of(*built) if built else None
    m = re.fullmatch(r"(\w+)\[(.+)\]", ann)
    if not m:
        return None
    outer, inner = m.group(1), _split_top(m.group(2), ",")
    if outer in ("tuple", "Tuple"):
        if len(inner) == 2 and inner[1] == "...":
            one = _strategy_for(inner[0], None, st, pool)
            return st.lists(one, min_size=1, max_size=4).map(tuple) if one is not None else None
        parts = [_strategy_for(i, None, st, pool) for i in inner]
        if any(p is None for p in parts):
            return None
        both = st.tuples(*parts)
        if len(inner) == 2 and inner[0] == inner[1] and inner[0] in ("int", "float"):
            # A pair of numbers is nearly always (low, high). Half the draws are put in that order.
            return st.one_of(both.map(lambda t: tuple(sorted(t)) if all(v == v for v in t) else t), both)
        return both
    if outer in SEQUENCES and len(inner) == 1:
        one = _strategy_for(inner[0], None, st, pool)
        return st.lists(one, max_size=4) if one is not None else None
    if outer in SETS and len(inner) == 1:
        one = _strategy_for(inner[0], None, st, pool)
        return st.lists(one, max_size=4).map(_as_set) if one is not None else None
    if outer in MAPPINGS and len(inner) == 2 and inner[0] == "str":
        one = _strategy_for(inner[1], None, st, pool)
        return st.dictionaries(st.text("abc", max_size=3), one, max_size=3) if one is not None else None
    return None


def _as_set(values: list):
    try:
        return set(values)
    except TypeError:
        return set()


def _strategy_for(annotation: str | None, default: str | None, st, pool: list | None = None):
    if _fake_kind(annotation):
        return st.just(lg_codec.FakeMarker(_fake_kind(annotation)))
    pool = pool or []
    strs = [v for v in pool if isinstance(v, str)]
    ints = [v for v in pool if isinstance(v, int) and not isinstance(v, bool)]
    nums = [v for v in pool if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
    ann = (annotation or "").replace("typing.", "").replace("decimal.", "").replace("datetime.", "").replace(" ", "").strip("\"'")
    ann = ann or _type_from_default(default)
    money = st.decimals(min_value=Decimal("-100000"), max_value=Decimal("100000"), places=2, allow_nan=False, allow_infinity=False)
    rate = st.decimals(min_value=Decimal("0"), max_value=Decimal("1"), places=3, allow_nan=False, allow_infinity=False)
    # Three decimal places: one value in ten ends in 5, the tie that separates rounding rules at two places.
    fine = st.decimals(min_value=Decimal("-1000"), max_value=Decimal("1000"), places=3, allow_nan=False, allow_infinity=False)
    if ann:
        optional = False
        m = re.fullmatch(r"Optional\[(.+)\]", ann)
        if m:
            ann, optional = m.group(1), True
        if ann.endswith("|None") or ann.startswith("None|"):
            ann, optional = ann.replace("|None", "").replace("None|", ""), True
        base = None
        if ann == "Decimal":
            base = _with_pool(st, st.one_of(money, rate, fine), [Decimal(str(v)) for v in nums])
        elif ann == "int":
            base = _with_pool(st, st.one_of(st.integers(-1000, 1000), st.integers(-2**40, 2**40)), ints)
        elif ann == "float":
            base = _with_specials(st, _with_pool(st, st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False), [float(v) for v in nums]), SPECIAL_FLOATS)
        elif ann == "str":
            base = _with_pool(st, st.text(TEXT_ALPHABET, max_size=16), strs)
        elif ann == "bool":
            base = st.booleans()
        elif ann == "date":
            base = st.dates(dt.date(2000, 1, 1), dt.date(2040, 12, 31))
        elif ann == "datetime":
            base = st.datetimes(dt.datetime(2000, 1, 1), dt.datetime(2040, 12, 31))
        elif _is_type_variable(ann):
            # list[_T] or item: _T: any value will do, and the project's own example values are the likeliest fit
            base = _with_pool(st, st.one_of(st.text("abcxyz", min_size=1, max_size=4), st.integers(0, 20)), strs + ints)
        else:
            base = _collection_strategy(ann, st, pool)
        if base is not None:
            return st.one_of(st.none(), base) if optional else base
    if default:
        m = re.fullmatch(r"""Decimal\(["']([^"']+)["']\)""", default)
        if m:
            return st.one_of(st.just(Decimal(m.group(1))), money)
        try:
            value = ast.literal_eval(default)
        except (ValueError, SyntaxError):
            value = "not-a-literal"
        if value is None:
            return _with_pool(st, st.one_of(st.none(), money, st.integers(-100, 100), st.text("abc", max_size=4)), pool)
        if isinstance(value, bool):
            return st.booleans()
        if isinstance(value, int):
            return _with_pool(st, st.integers(-1000, 1000), ints)
        if isinstance(value, float):
            return st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)
        if isinstance(value, str) and value != "not-a-literal":
            return _with_pool(st, st.one_of(st.just(value), st.text("abcdefgh", max_size=8)), strs)
        return None  # a default we cannot read a type from (settings.X, {}, a call): only the default is known to be valid
    if ann:
        return None  # a type we cannot build
    return _with_pool(st, st.one_of(money, fine, st.integers(-100, 100), st.text("abcxyz", max_size=4), st.none()), pool)


MAX_GRID = 400
MAX_OPTIONAL_VALUES = 10
GRID_PER_PARAM = 8
SMALL_INTS = [0, 1, 2, 3]
# Values where rounding rules part ways. Random floats almost never land on a tie.
TIE_FLOATS = [0.5, 1.5, 2.5, 0.125, 2.675, 1.005]
TIE_DECIMALS = [Decimal("0.005"), Decimal("1.005"), Decimal("2.675"), Decimal("-0.125")]
# Values code rarely plans for. Two versions that agree on every ordinary number can still part ways here
# (boltons clamp: min(max(x, lo), hi) and max(min(x, hi), lo) differ only for a NaN bound with a Decimal value).
SPECIAL_FLOATS = [0.0, float("inf"), float("-inf"), float("nan")]
SPECIAL_DECIMALS = [Decimal("NaN"), Decimal("Infinity")]
CONSTRUCTOR_DEFAULT = re.compile(r"(float|int|str|bool|Decimal)\(.*\)")


def _type_from_default(default: str | None) -> str:
    """lower=float('-inf') says what the parameter takes even without a type hint. settings.X says nothing."""
    m = CONSTRUCTOR_DEFAULT.fullmatch((default or "").replace("decimal.", "").strip())
    return m.group(1) if m else ""


def is_special(value) -> bool:
    if isinstance(value, float):
        return value != value or value in (float("inf"), float("-inf"))
    if isinstance(value, Decimal):
        return not value.is_finite()
    return False


def _fake_kind(annotation: str | None) -> str | None:
    m = re.fullmatch(r"__fake_(\w+)__", annotation or "")
    return m.group(1) if m else None


def _grid_values(p: dict, pool: list) -> list | None:
    """A short list of likely values for one parameter, or None to leave it at its default."""
    if _fake_kind(p.get("annotation")):
        return [lg_codec.FakeMarker(_fake_kind(p["annotation"]))]
    ann = (p.get("annotation") or "").replace("typing.", "").replace("decimal.", "").replace(" ", "").strip("\"'")
    ann = re.sub(r"^Optional\[(.+)\]$", r"\1", ann).replace("|None", "").replace("None|", "")
    ann = ann or _type_from_default(p.get("default"))
    strs = [v for v in pool if isinstance(v, str)]
    ints = [v for v in pool if isinstance(v, int) and not isinstance(v, bool)]
    nums = [v for v in pool if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
    floats = [float(v) for v in nums if not float(v).is_integer()]
    members = _split_top(ann, "|") if ann else []
    if len(members) > 1:
        # float | str: the likely values of each member, a few of each
        merged: list = []
        for member in members:
            merged += (_grid_values({**p, "annotation": member, "default": None}, pool) or [])[:6]
        return merged or None
    if ann == "bool":
        return [False, True]
    if ann == "int":
        return list(dict.fromkeys(SMALL_INTS + ints))[:GRID_PER_PARAM]
    if ann == "float":
        return list(dict.fromkeys(TIE_FLOATS + floats[:4] + [float(v) for v in nums][:4])) + SPECIAL_FLOATS
    if ann == "Decimal":
        return list(dict.fromkeys([Decimal(str(v)) for v in nums][:GRID_PER_PARAM] + TIE_DECIMALS + [Decimal(str(v)) for v in TIE_FLOATS])) + SPECIAL_DECIMALS
    if ann == "str":
        return strs[:GRID_PER_PARAM] or None
    if _is_type_variable(ann):
        return (strs[:6] + list(dict.fromkeys(ints))[:4]) or None
    m = re.fullmatch(r"(?:tuple|Tuple)\[(int|float),(int|float)\]", ann)
    if m:
        # (low, high) ranges from the example numbers, smallest spans first: (1, 100), (1, 255), ...
        ordered = sorted({v for v in nums if isinstance(v, (int, float)) and not is_special(v)})[:6]
        pairs = [(a, b) for i, a in enumerate(ordered) for b in ordered[i + 1:]]
        return (pairs[:8] + [(1, 100), (0, 1), (1, 255)])[:10]
    m = re.fullmatch(r"(\w+)\[([^\[\],]+)\]", ann)
    if m and m.group(1) in SEQUENCES:
        inner = m.group(2)
        values = strs if inner == "str" or _is_type_variable(inner) else ints if inner == "int" else [float(v) for v in nums] if inner == "float" else []
        values = list(dict.fromkeys(values))
        # the examples as one list, and shorter prefixes of it: lengths 4, 3 and 2 behave differently under division
        return [values[:n] for n in (4, 3, 2, 1) if len(values) >= n] or None
    if ann:
        return None
    if p.get("default") is not None:
        return None  # an untyped parameter with a default: the default is the one value known to be valid
    # Nothing says what this parameter takes. The project's examples come first, then the tie values in the
    # forms money usually arrives in: a float and an exact Decimal.
    return strs[:16] + list(dict.fromkeys(nums))[:6] + TIE_FLOATS + TIE_DECIMALS + SPECIAL_FLOATS[1:] + SPECIAL_DECIMALS


def _grid(sym, pool: list | None) -> list[dict]:
    """Combinations of the project's example values and small numbers, tried before random guesses (FR-36).

    A tie like (0.125, 2 places) or a size like '1.7K' is one specific pairing; drawing each value at random almost
    never produces it. Required parameters are combined with each other. Optional ones are varied one at a time,
    the rest left at their defaults: functions often forbid setting two options together (Odoo's float_round takes
    precision_digits or precision_rounding, not both), and a full product would be mostly invalid calls."""
    pool = pool or []
    params = [p for p in sym["params"] if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]
    required: list[tuple[dict, list]] = []
    optional: list[tuple[dict, list]] = []
    for p in params:
        values = _grid_values(p, pool)
        if p.get("default") is None:
            if values is None:
                return []  # a required parameter we have no likely values for
            required.append((p, values))
        elif values is not None:
            # Short lists for options, so the whole grid fits and nothing is left to a random sample:
            # Odoo's float_round(2.675, precision_digits=2) was in the sample only one run in three.
            ordinary = [v for v in values if not is_special(v)][:MAX_OPTIONAL_VALUES]
            optional.append((p, ordinary + [v for v in values if is_special(v)]))
    if not required and not optional:
        return []
    import itertools
    import random
    base = list(itertools.islice(itertools.product(*[v for _, v in required]), 2000)) if required else [()]
    combos: list[tuple[tuple, tuple | None]] = [(b, None) for b in base]
    for p, values in optional:
        combos += [(b, (p, v)) for b in base for v in values]
    if len(combos) > MAX_GRID:
        combos = random.Random(0).sample(combos, MAX_GRID)
    weak = any(not p.get("annotation") for p, _ in required)
    out = []
    for b, extra in combos:
        try:
            args = [{"name": p["name"], "kind": "pos", "value": lg_codec.encode_input(v)} for (p, _), v in zip(required, b)]
            if extra is not None:
                args.append({"name": extra[0]["name"], "kind": "kw", "value": lg_codec.encode_input(extra[1])})
        except Exception:  # noqa: BLE001
            continue
        if args:
            out.append({"args": args, "source": "generated", "weak": weak})
    return _crossed(required, optional, weak) + out


MAX_CROSSED = 81


def _representatives(values: list) -> list:
    """One value of each kind: text, an ordinary number, an exact Decimal, NaN, infinity."""
    picks: list = []
    def first(test):
        for v in values:
            if test(v):
                picks.append(v)
                return
    first(lambda v: isinstance(v, str))
    first(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and not is_special(v))
    first(lambda v: isinstance(v, Decimal) and not is_special(v))
    first(lambda v: isinstance(v, float) and v != v)
    if len(picks) < 3:
        picks += [v for v in values if not is_special(v) and not any(v is q for q in picks)][:3 - len(picks)]
    return picks


def _crossed(required: list, optional: list, weak: bool) -> list[dict]:
    """A few representative values of every parameter, all combined (FR-37).

    Varying one option at a time cannot find a break that needs two of them set: boltons' clamp rewrite differs only
    for a Decimal value with one ordinary bound and one NaN bound."""
    import itertools
    import random
    chosen = [(p, _representatives(v), "pos") for p, v in required] + [(p, _representatives(v), "kw") for p, v in optional]
    if not 2 <= len(chosen) <= 4 or not any(o for _, o, kind in chosen if kind == "kw"):
        return []  # with no optional parameters the main grid is already a full product
    combos = list(itertools.product(*[v for _, v, _ in chosen]))
    if len(combos) > MAX_CROSSED:
        combos = random.Random(0).sample(combos, MAX_CROSSED)
    out = []
    for combo in combos:
        try:
            args = [{"name": p["name"], "kind": kind, "value": lg_codec.encode_input(v)} for (p, _, kind), v in zip(chosen, combo)]
        except Exception:  # noqa: BLE001
            continue
        out.append({"args": args, "source": "generated", "weak": weak})
    return out


def _generated(sym, count: int, state_dir: Path, pool: list | None = None) -> list[dict]:
    params = [p for p in sym["params"] if p["name"] not in ("self", "cls")]
    if any(p["name"].startswith("*") for p in params):
        params = [p for p in params if not p["name"].startswith("*")]
    if not params:
        return [{"args": [], "source": "generated"}]
    hypothesis, st = _hypothesis(state_dir)
    strategies = {}
    weak = False
    for p in params:
        if not p.get("annotation") and not p.get("default"):
            weak = True  # nothing says what this parameter accepts, so a value may simply be invalid
        strat = _strategy_for(p.get("annotation"), p.get("default"), st, pool)
        if strat is None:
            if p.get("default") is not None:
                continue  # leave it to its default
            return []
        strategies[p["name"]] = strat
    if not strategies:
        return [{"args": [], "source": "generated"}]
    out: list[dict] = []
    seen: set[str] = set()

    @hypothesis.given(st.fixed_dictionaries(strategies))
    @hypothesis.settings(max_examples=count, database=None, derandomize=True, deadline=None, phases=[hypothesis.Phase.generate],
                         suppress_health_check=list(hypothesis.HealthCheck))
    def collect(values):
        args = []
        positional = True
        for p in params:
            if p["name"] in values:
                args.append({"name": p["name"], "kind": "pos" if positional else "kw", "value": lg_codec.encode_input(values[p["name"]])})
            else:
                positional = False
        sig = json.dumps(args, sort_keys=True)
        if sig not in seen:
            seen.add(sig)
            out.append({"args": args, "source": "generated", "weak": weak})

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            collect()
        except Exception:  # noqa: BLE001 - generation failure means harvested inputs only (TESTING.md §8)
            pass
    return out


def _example_calls(sym: dict, calls: list) -> list[dict]:
    """Whole calls written in the repo with literal arguments. They are real uses, so they are not marked weak."""
    out = []
    for call in calls:
        try:
            args = [{"name": name, "kind": kind, "value": lg_codec.encode_input(value)} for name, kind, value in call]
        except Exception:  # noqa: BLE001 - a value the codec cannot carry is skipped, not fatal
            continue
        out.append({"args": args, "has_self": False, "source": "examples"})
    return out


# ---- inputs seeded from tests' objects and from the edit (roadmap 3c, Sprint 5 B) ----

MAX_EDIT_BASES = 6       # example calls an edit's values are spliced into
MAX_EDIT_ITEMS = 120
MAX_REGEX_SAMPLES = 6    # strings kept per changed regex
REGEX_DRAWS = 100        # past about 100, Hypothesis's draws from a regex get several times slower


def _plain_params(sym: dict) -> list[dict]:
    return [p for p in sym["params"] if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]


def _object_items(sym: dict, objects: dict, bases: list[dict]) -> list[dict]:
    """Calls that pass objects the project's tests build (examples.param_objects) for parameters nothing else fills.

    The other arguments come from the first example call, or are left to their defaults. They are guesses for this
    function, so they are marked weak: two rejections of an object it was never meant to take prove nothing."""
    if not objects:
        return []
    params = _plain_params(sym)
    base = next((b for b in bases if not b.get("has_self")), None)
    known = {a["name"]: a for a in (base or {}).get("args", [])}
    out = []
    for name, values in objects.items():
        for obj in values:
            args = []
            positional = True
            for p in params:
                if p["name"] == name:
                    try:
                        value = lg_codec.encode_input(obj)
                    except Exception:  # noqa: BLE001
                        args = None
                        break
                    args.append({"name": name, "kind": "pos" if positional else "kw", "value": value})
                elif p["name"] in known:
                    args.append({**known[p["name"]], "kind": "pos" if positional else "kw"})
                elif p.get("default") is not None:
                    positional = False
                else:
                    args = None  # a required parameter with no value
                    break
            if args:
                out.append({"args": args, "has_self": False, "source": "examples", "weak": True})
    return out


REGEX_CALL_S = 0.05       # one pattern on one string, in the guard's own process
REGEX_PAIR_BUDGET_S = 1.0
REGEX_DRAW_S = 1.0        # Hypothesis drawing from one pair of patterns (the child's hard timeout is the outer bound)


class _RegexSlow(BaseException):
    """BaseException, so code the timer interrupts (Hypothesis included) does not catch it as an ordinary error."""


@contextlib.contextmanager
def _time_limit(seconds: float):
    """Stop a regex that backtracks for too long. The edited pattern may be the one that does (a planted bug often is).
    Off the main thread no timer can interrupt it, so only short strings are tried there."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield
        return
    def stop(signum, frame):
        raise _RegexSlow()
    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _regex_outcome(compiled, fn: str, text: str):
    if fn == "match":
        m = compiled.match(text)
    elif fn == "fullmatch":
        m = compiled.fullmatch(text)
    elif fn in ("search", "compile"):
        m = compiled.search(text)
    else:  # sub, subn, findall, finditer, split: every match counts
        return [(m.span(), m.groups()) for _, m in zip(range(20), compiled.finditer(text))]
    return (m.span(), m.groups()) if m else None


SEED_BUDGET_S = 2.0       # all edit-derived input building for one function: object scanning and regex sampling


def _deadline_left(deadline: float | None) -> float:
    return SEED_BUDGET_S if deadline is None else deadline - time.monotonic()


def _regex_child(regexes: list[dict], screen: list[str], state_dir: Path, seconds: float) -> tuple[list[str] | None, list[str]]:
    """(strings on which a changed regex backtracks too long, regex samples), worked out in a child process killed
    after `seconds`. The first is None when the child did not get that far: the caller then drops what it meant to
    screen.

    Hypothesis's from_regex can itself backtrack for minutes on a nested pattern (youtube-dl's js_to_json), and off the
    main thread no signal can stop that, so the outer bound is a process boundary, not a timer."""
    if not regexes or seconds <= 0.1:
        return None, []
    import subprocess
    import sys
    code = "import sys; sys.path.insert(0, sys.argv[1]); from runner import inputs; inputs._child_main()"
    env = {**os.environ, "HYPOTHESIS_STORAGE_DIRECTORY": str(Path(state_dir) / ".hypothesis")}
    payload = json.dumps({"regexes": regexes, "screen": screen, "state_dir": str(state_dir)})
    try:
        done = subprocess.run([sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parent.parent)], input=payload,
                              capture_output=True, text=True, timeout=seconds, env=env)
        text = done.stdout if done.returncode == 0 else ""
    except subprocess.TimeoutExpired as exc:  # what the child printed before it was stopped still counts
        text = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    except OSError:
        return None, []
    slow, samples = None, []
    for line in text.splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and isinstance(msg.get("slow"), list):
            slow = [t for t in msg["slow"] if isinstance(t, str)]
        if isinstance(msg, dict) and isinstance(msg.get("samples"), list):
            samples = [t for t in msg["samples"] if isinstance(t, str)]
    return slow, samples


def _child_main() -> None:
    """Entry point of the child _regex_child starts: screening first (flushed), then sampling, one JSON line each."""
    import sys
    req = json.load(sys.stdin)
    compiled = []
    for r in req["regexes"]:
        for pattern, flags in ((r["old"], r.get("flags_old", 0)), (r["new"], r.get("flags_new", 0))):
            try:
                compiled.append((re.compile(pattern, flags), r.get("fn", "search")))
            except Exception:  # noqa: BLE001
                continue
    slow = []
    for text in req["screen"]:
        for rx, fn in compiled:
            try:
                with _time_limit(REGEX_CALL_S):
                    _regex_outcome(rx, fn, text)
            except _RegexSlow:
                slow.append(text)
                break
            except Exception:  # noqa: BLE001
                continue
    print(json.dumps({"slow": slow}), flush=True)
    print(json.dumps({"samples": _regex_samples(req["regexes"], Path(req["state_dir"]))}), flush=True)


def _regex_samples(regexes: list[dict], state_dir: Path) -> list[str]:
    """Strings drawn from both versions of each changed regex that the two treat differently, under the re function the
    code calls (match, search, fullmatch, or every match for sub/findall/split). Patterns Hypothesis cannot draw from are
    skipped. Draws are derandomized, so OLD and NEW runs get the same strings."""
    if not regexes:
        return []
    try:
        hypothesis, st = _hypothesis(state_dir)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for r in regexes:
        try:
            old = re.compile(r["old"], r.get("flags_old", 0))
            new = re.compile(r["new"], r.get("flags_new", 0))
        except (re.error, TypeError, ValueError, OverflowError, RecursionError):
            continue
        fn = r.get("fn", "search")
        strategies = []
        for compiled in (old, new):
            for full in ((True, False) if fn == "match" else (fn == "fullmatch",)):
                try:
                    strategies.append(st.from_regex(compiled, fullmatch=full))
                except Exception:  # noqa: BLE001 - a construct Hypothesis does not support
                    continue
        if not strategies:
            continue
        drawn: list[str] = []

        @hypothesis.given(st.one_of(*strategies))
        @hypothesis.settings(max_examples=REGEX_DRAWS, database=None, derandomize=True, deadline=None, phases=[hypothesis.Phase.generate],
                             suppress_health_check=list(hypothesis.HealthCheck))
        def draw(text):
            drawn.append(text)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                with _time_limit(REGEX_DRAW_S):  # from_regex itself can backtrack on a nested pattern
                    draw()
            except (Exception, _RegexSlow):  # noqa: BLE001
                pass
        kept = []
        longest = 200 if threading.current_thread() is threading.main_thread() else 16
        texts = [t for t in dict.fromkeys(drawn) if len(t) <= longest and not any("\ud800" <= ch <= "\udfff" for ch in t)]
        texts.sort(key=lambda t: (len(t), t))
        spent = 0.0
        for text in texts:
            if spent > REGEX_PAIR_BUDGET_S:
                break
            started = time.monotonic()
            try:
                with _time_limit(REGEX_CALL_S):
                    if _regex_outcome(old, fn, text) != _regex_outcome(new, fn, text):
                        kept.append(text)
            except _RegexSlow:
                spent += REGEX_PAIR_BUDGET_S  # one pathological pattern: the rest of its strings would be as slow
            except Exception:  # noqa: BLE001
                pass
            spent += time.monotonic() - started
            if len(kept) >= MAX_REGEX_SAMPLES:
                break
        kept.sort(key=lambda t: (len(t), t))
        out += kept[:MAX_REGEX_SAMPLES]
    return list(dict.fromkeys(out))


def _number_values(pairs: list) -> list:
    """At, between and around an edit's old and new numeric constants."""
    out: list = []
    for a, b in pairs:
        out += [a, b]
        mid = (a + b) / 2
        out.append(int(mid) if isinstance(a, int) and isinstance(b, int) and mid == int(mid) else mid)
        for v in (a, b):
            step = 1 if isinstance(v, int) else max(abs(v) * 0.01, 0.001)
            out += [v - step, v + step]
    seen, unique = set(), []
    for v in out:
        key = (type(v).__name__, repr(v))
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def _synthetic_bases(sym: dict) -> list[dict]:
    """With no example call to build on: placeholders for each required parameter that holds text or numbers."""
    params = _plain_params(sym)
    required = [p for p in params if p.get("default") is None]
    if not required:
        return []
    text_args, num_args, weak = [], [], False
    for p in required:
        ann = (p.get("annotation") or "").replace(" ", "")
        if not ann:
            weak = True
            text_args.append({"name": p["name"], "kind": "pos", "value": lg_codec.encode_input("")})
            num_args.append({"name": p["name"], "kind": "pos", "value": lg_codec.encode_input(0)})
        elif ann in ("str", "Optional[str]", "str|None"):
            text_args.append({"name": p["name"], "kind": "pos", "value": lg_codec.encode_input("")})
            num_args.append(text_args[-1])
        elif ann in ("int", "float", "Optional[int]", "Optional[float]", "int|None", "float|None"):
            zero = 0 if "int" in ann else 0.0
            text_args.append({"name": p["name"], "kind": "pos", "value": lg_codec.encode_input(zero)})
            num_args.append(text_args[-1])
        else:
            return []
    bases = [{"args": text_args, "has_self": False, "weak": weak}]
    if num_args != text_args:
        bases.append({"args": num_args, "has_self": False, "weak": weak})
    return bases


def _edit_items(sym: dict, bases: list[dict], edit: dict | None, state_dir: Path, deadline: float | None = None) -> list[dict]:
    """Inputs aimed at the edit: its regexes' distinguishing strings, its numbers, its literals spliced into text and
    dict examples. Each is a copy of an example call (or placeholder call) with one argument replaced.

    Every one is marked weak, like a generated guess: a spliced or sampled value may simply be invalid, and two
    different rejections of it ("too many values to unpack" against "bad address") say nothing about valid calls. A
    value one version accepts and the other rejects, or two different results, still count."""
    if not edit:
        return []
    literals = edit.get("literals") or []
    tokens = [v for v in literals if isinstance(v, str) and 0 < len(v) <= 20] + list(edit.get("fragments") or [])
    tokens = list(dict.fromkeys(tokens))[:12]
    numbers = _number_values(edit.get("numbers") or [])
    regexes = edit.get("regexes") or []
    dict_values = list(literals) + [True, False, None]
    dict_values = [v for i, v in enumerate(dict_values) if not any(type(v) is type(w) and v == w for w in dict_values[:i])]
    dict_values = [v for v in dict_values if not isinstance(v, str) or len(v) <= 40][:12]
    bases = [b for b in bases if b.get("args")][:MAX_EDIT_BASES] or _synthetic_bases(sym)
    kinds: dict[str, list[dict]] = {"regex": [], "number": [], "dict": [], "splice": []}
    texts: dict[int, str] = {}  # id(splice item) -> the text it passes, for screening

    def variant(base, index, value, kind):
        try:
            encoded = lg_codec.encode_input(value)
        except Exception:  # noqa: BLE001
            return
        args = [dict(a) for a in base["args"]]
        args[index] = {**args[index], "value": encoded}
        item = {"args": args, "has_self": base.get("has_self", False), "source": "edit", "weak": True}
        kinds[kind].append(item)
        if isinstance(value, str):
            texts[id(item)] = value

    for base in bases:
        for i, arg in enumerate(base["args"]):
            if arg.get("name") == "self" or arg.get("kind") in ("varargs", "varkw"):
                continue
            enc = arg["value"]
            t, v = enc.get("t"), enc.get("v")
            if t == "v" and isinstance(v, str):
                for tok in tokens:
                    for spliced in (tok + v, v + tok, v[:1] + tok + v[1:]):
                        variant(base, i, spliced, "splice")
            elif (t == "v" and isinstance(v, int) and not isinstance(v, bool)) or t == "float":
                for n in numbers:
                    variant(base, i, n, "number")
            elif t == "dict":
                for j, (k, _) in enumerate(v):
                    for value in dict_values:
                        pairs = [list(kv) for kv in v]
                        try:
                            pairs[j] = [k, lg_codec.encode_input(value)]
                        except Exception:  # noqa: BLE001
                            continue
                        args = [dict(a) for a in base["args"]]
                        args[i] = {**args[i], "value": {"t": "dict", "v": pairs}}
                        kinds["dict"].append({"args": args, "has_self": base.get("has_self", False), "source": "edit", "weak": True})
    if regexes:
        # Drop splices on which a changed regex backtracks for too long: old and new would both time out, so they
        # compare nothing and only use up the worker's time (youtube-dl's js_to_json on an unterminated quote).
        screen = list(dict.fromkeys(texts[id(item)] for item in kinds["splice"]))
        slow, samples = _regex_child(regexes, screen, state_dir, _deadline_left(deadline))
        if slow is None:
            kinds["splice"] = []  # not screened in time: none rather than risk them
        elif slow:
            dropped = set(slow)
            kinds["splice"] = [item for item in kinds["splice"] if texts[id(item)] not in dropped]
        for base in bases:
            for i, arg in enumerate(base["args"]):
                enc = arg["value"]
                if (arg.get("name") != "self" and arg.get("kind") not in ("varargs", "varkw") and enc.get("t") == "v"
                        and isinstance(enc.get("v"), str)):
                    for sample in samples:
                        variant(base, i, sample, "regex")
    # Round robin over the kinds, so a cap keeps some of each.
    out: list[dict] = []
    seen: set[str] = {json.dumps(b["args"], sort_keys=True) for b in bases}
    queues = [list(q) for q in kinds.values()]
    while any(queues) and len(out) < MAX_EDIT_ITEMS:
        for q in queues:
            while q:
                item = q.pop(0)
                sig = json.dumps(item["args"], sort_keys=True)
                if sig not in seen:
                    seen.add(sig)
                    out.append(item)
                    break
    return out


# Hints the strategies above can build. A hint they cannot build would leave the parameter worse off than no hint.
BUILDABLE_HINT = re.compile(r"int|float|str|bool|Decimal|date|datetime|(?:list|set|frozenset)\[(?:int|float|str|bool|Decimal)\]"
                            r"|tuple\[(?:int|float|str|bool|Decimal)(?:,(?:int|float|str|bool|Decimal))*\]|dict\[str,(?:int|float|str|bool|Decimal)\]")


def with_runtime_types(sym: dict, harvested: dict | None) -> dict:
    """Fill in missing type hints from the types real test runs passed (roadmap B4).

    The most common type wins; if the tests also passed None, the hint becomes Optional. Classes are left alone:
    the builder cannot make them here."""
    seen = (harvested or {}).get("types") or {}
    if not seen:
        return sym
    params = []
    for p in sym["params"]:
        counts = seen.get(p["name"].lstrip("*"))
        if p.get("annotation") or not counts or p["name"].startswith("*"):
            params.append(p)
            continue
        usable = {t: n for t, n in counts.items() if BUILDABLE_HINT.fullmatch(t)}
        if not usable:
            params.append(p)
            continue
        best = max(sorted(usable), key=lambda t: usable[t])
        hint = f"Optional[{best}]" if "None" in counts else best
        params.append({**p, "annotation": hint, "inferred": True})
    return {**sym, "params": params}


FAKE_PARAMS = {"conn": "db", "connection": "db", "db": "db", "cursor": "cursor", "cur": "cursor"}
ORM_PARAMS = DB_PARAM_NAMES - set(FAKE_PARAMS)  # session, engine, transaction: an ORM a DB-API stand-in cannot imitate


def build(sym: dict, harvested: dict | None, max_inputs: int, state_dir: Path, examples: dict | None = None,
          stub_db: bool = False) -> tuple[list[dict], str | None]:
    """(inputs, reason it cannot be run). sym: {qualname, kind, params}. With stub_db, a connection or cursor
    parameter gets a stand-in that records the SQL it is sent (roadmap C1)."""
    sym = with_runtime_types(sym, harvested)
    names = [p["name"].lstrip("*") for p in sym["params"]]
    blocked = (harvested or {}).get("blocked", {})
    db_params = sorted(set(blocked) & DB_PARAM_NAMES) or sorted(set(names) & DB_PARAM_NAMES)
    if db_params and stub_db and not set(db_params) & ORM_PARAMS:
        sym = {**sym, "params": [{**p, "annotation": f"__fake_{FAKE_PARAMS[p['name']]}__"} if p["name"] in FAKE_PARAMS else p
                                 for p in sym["params"]]}
        db_params = []
    if db_params:
        return [], f"needs a database connection (parameter {db_params[0]})"
    items = []
    for call in (harvested or {}).get("inputs", []):
        has_self = bool(call) and call[0]["name"] == "self"
        items.append({"args": call, "has_self": has_self, "source": "tests"})
    items += _example_calls(sym, (examples or {}).get("calls", []))
    # Both are bonuses built from values of any shape (harvested pickles, odd example calls). An unexpected shape must
    # leave the inputs as they were before these existed, not stop the check.
    try:
        items += _object_items(sym, (examples or {}).get("objects") or {}, items)
    except Exception:  # noqa: BLE001
        pass
    try:
        edit = _edit_items(sym, items, (examples or {}).get("edit"), state_dir, (examples or {}).get("seed_deadline"))
    except Exception:  # noqa: BLE001
        edit = []
    # Seeded inputs (tests, examples, the edit) use at most two thirds of a full budget; the rest is left for the
    # generic grid and Hypothesis, which find what no example points at. The edit's own inputs get a share of the seeded
    # part even when tests alone would fill it.
    seeded_limit = max_inputs - max_inputs // 3
    edit_share = min(len(edit), max(1, seeded_limit // 3)) if edit else 0
    front = items[:max(0, seeded_limit - edit_share)]
    edit_taken = edit[:seeded_limit - len(front)]
    leftover = items[len(front):] + edit[len(edit_taken):]
    items = front + edit_taken
    pool = (examples or {}).get("pool")
    if pool is not None and (examples or {}).get("edit"):
        # The edit's constants join the pool the grid and Hypothesis draw from (constant seeding).
        pool = list(pool) + [v for v in (examples["edit"].get("literals") or []) if v is not None and not isinstance(v, bool)]
    items += _grid(sym, pool)[:max(0, max_inputs - len(items))]
    remaining = min(max_inputs - len(items), MAX_GENERATED)
    if remaining > 0:
        items += _generated(sym, remaining, state_dir, pool)
    # When generic generation left room, the seeded inputs held back above take it.
    items += leftover[:max(0, max_inputs - len(items))]
    return items, None
