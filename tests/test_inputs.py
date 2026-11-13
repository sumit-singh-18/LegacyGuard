from decimal import Decimal

from runner import inputs, lg_codec


def decoded(items, name):
    out = []
    for item in items:
        for a in item["args"]:
            if a["name"] == name:
                out.append(lg_codec.decode(a["value"]))
    return out


def test_db_param_is_untested(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "cursor", "annotation": None, "default": None}]}
    items, reason = inputs.build(sym, None, 50, tmp_path)
    assert items == [] and "database" in reason


def test_float_grid_has_ties_and_specials(tmp_path):
    sym = {"qualname": "r", "kind": "function", "params": [{"name": "x", "annotation": "float", "default": None}]}
    items, reason = inputs.build(sym, None, 200, tmp_path)
    xs = decoded(items, "x")
    assert reason is None
    assert 2.675 in xs and float("inf") in xs and any(v != v for v in xs)


def test_harvested_inputs_come_first(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "n", "annotation": "int", "default": None}]}
    harvested = {"inputs": [[{"name": "n", "kind": "pos", "value": lg_codec.encode_input(41)}]]}
    items, _ = inputs.build(sym, harvested, 50, tmp_path)
    assert items[0]["source"] == "tests" and decoded(items[:1], "n") == [41]


def test_decimal_grid(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "amount", "annotation": "Decimal", "default": None}]}
    items, _ = inputs.build(sym, None, 200, tmp_path)
    assert Decimal("2.675") in decoded(items, "amount")


def test_max_inputs_respected(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "s", "annotation": "str", "default": None}]}
    items, _ = inputs.build(sym, None, 20, tmp_path)
    assert 0 < len(items) <= 20
