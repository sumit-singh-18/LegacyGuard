"""Inputs for the behavioral diff: harvested from real tests first, then Hypothesis from hints and defaults (FR-14 step 1)."""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
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


def _with_pool(st, base, values: list):
    """Half the draws come from the project's own example values, when it has any of the right kind."""
    return st.one_of(st.sampled_from(values), base) if values else base


def _strategy_for(annotation: str | None, default: str | None, st, pool: list | None = None):
    pool = pool or []
    strs = [v for v in pool if isinstance(v, str)]
    ints = [v for v in pool if isinstance(v, int) and not isinstance(v, bool)]
    nums = [v for v in pool if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
    ann = (annotation or "").replace("typing.", "").replace("decimal.", "").replace("datetime.", "").replace(" ", "").strip("\"'")
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
            base = _with_pool(st, st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False), [float(v) for v in nums])
        elif ann == "str":
            base = _with_pool(st, st.text(TEXT_ALPHABET, max_size=16), strs)
        elif ann == "bool":
            base = st.booleans()
        elif ann == "date":
            base = st.dates(dt.date(2000, 1, 1), dt.date(2040, 12, 31))
        elif ann == "datetime":
            base = st.datetimes(dt.datetime(2000, 1, 1), dt.datetime(2040, 12, 31))
        else:
            m = re.fullmatch(r"(?:list|List)\[(.+)\]", ann)
            if m:
                inner = _strategy_for(m.group(1), None, st, pool)
                base = st.lists(inner, max_size=4) if inner is not None else None
            m = re.fullmatch(r"(?:dict|Dict)\[str,(.+)\]", ann)
            if m and base is None:
                inner = _strategy_for(m.group(1), None, st, pool)
                base = st.dictionaries(st.text("abc", max_size=3), inner, max_size=3) if inner is not None else None
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


MAX_GRID = 150
GRID_PER_PARAM = 8
SMALL_INTS = [0, 1, 2, 3]
# Values where rounding rules part ways. Random floats almost never land on a tie.
TIE_FLOATS = [0.5, 1.5, 2.5, 0.125, 2.675, 1.005]
TIE_DECIMALS = [Decimal("0.005"), Decimal("1.005"), Decimal("2.675"), Decimal("-0.125")]


def _grid_values(p: dict, pool: list) -> list | None:
    """A short list of likely values for one parameter, or None to leave it at its default."""
    ann = (p.get("annotation") or "").replace("typing.", "").replace("decimal.", "").replace(" ", "").strip("\"'")
    ann = re.sub(r"^Optional\[(.+)\]$", r"\1", ann).replace("|None", "").replace("None|", "")
    strs = [v for v in pool if isinstance(v, str)]
    ints = [v for v in pool if isinstance(v, int) and not isinstance(v, bool)]
    nums = [v for v in pool if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
    floats = [float(v) for v in nums if not float(v).is_integer()]
    if ann == "bool":
        return [False, True]
    if ann == "int":
        return list(dict.fromkeys(SMALL_INTS + ints))[:GRID_PER_PARAM]
    if ann == "float":
        return list(dict.fromkeys(floats + TIE_FLOATS + [float(v) for v in nums]))[:GRID_PER_PARAM + len(TIE_FLOATS)]
    if ann == "Decimal":
        return list(dict.fromkeys([Decimal(str(v)) for v in nums][:GRID_PER_PARAM] + TIE_DECIMALS + [Decimal(str(v)) for v in TIE_FLOATS]))
    if ann == "str":
        return strs[:GRID_PER_PARAM] or None
    if ann:
        return None
    if p.get("default") is not None:
        return None  # an untyped parameter with a default: the default is the one value known to be valid
    # Nothing says what this parameter takes. The project's examples come first, then the tie values in the
    # forms money usually arrives in: a float and an exact Decimal.
    return strs[:16] + list(dict.fromkeys(nums))[:6] + TIE_FLOATS + TIE_DECIMALS


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
            optional.append((p, values))
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


def build(sym: dict, harvested: dict | None, max_inputs: int, state_dir: Path, examples: dict | None = None) -> tuple[list[dict], str | None]:
    """(inputs, reason it cannot be run). sym: {qualname, kind, params}."""
    names = [p["name"].lstrip("*") for p in sym["params"]]
    blocked = (harvested or {}).get("blocked", {})
    db_params = sorted(set(blocked) & DB_PARAM_NAMES) or sorted(set(names) & DB_PARAM_NAMES)
    if db_params:
        return [], f"needs a database connection (parameter {db_params[0]})"
    items = []
    for call in (harvested or {}).get("inputs", []):
        has_self = bool(call) and call[0]["name"] == "self"
        items.append({"args": call, "has_self": has_self, "source": "tests"})
    items += _example_calls(sym, (examples or {}).get("calls", []))
    items = items[:max_inputs]
    pool = (examples or {}).get("pool")
    items += _grid(sym, pool)[:max(0, max_inputs - len(items))]
    remaining = min(max_inputs - len(items), MAX_GENERATED)
    if remaining > 0:
        items += _generated(sym, remaining, state_dir, pool)
    return items, None
