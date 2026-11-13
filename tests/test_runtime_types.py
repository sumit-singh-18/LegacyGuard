"""Runtime types from the test suite fill in missing type hints (roadmap B4)."""
import json
import os
import shutil
import subprocess
from decimal import Decimal

from runner import inputs, lg_codec

from conftest import PLUGIN, TARGET_PY, write_tree


def test_type_names():
    import sys
    sys.path.insert(0, str(PLUGIN / "runner"))
    from legacyguard_harvest import type_name
    assert type_name(3) == "int" and type_name(True) == "bool" and type_name(None) == "None"
    assert type_name(Decimal("1.5")) == "Decimal"
    assert type_name([1, 2]) == "list[int]" and type_name([1, "a"]) == "list"
    assert type_name((1.0, 2.0)) == "tuple[float,float]"
    assert type_name({"a": 1}) == "dict[str,int]"
    assert type_name(object()).startswith("class:")


def sym(*names):
    return {"qualname": "f", "kind": "function", "params": [{"name": n, "annotation": None, "default": None} for n in names]}


def test_inferred_hint_and_optional():
    harvested = {"types": {"price": {"float": 9, "int": 1}, "code": {"str": 3, "None": 2}, "obj": {"class:m:Thing": 4}}}
    params = {p["name"]: p for p in inputs.with_runtime_types(sym("price", "code", "obj"), harvested)["params"]}
    assert params["price"]["annotation"] == "float" and params["price"]["inferred"]
    assert params["code"]["annotation"] == "Optional[str]"
    assert params["obj"]["annotation"] is None


def test_declared_hint_wins():
    s = {"qualname": "f", "kind": "function", "params": [{"name": "x", "annotation": "int", "default": None}]}
    assert inputs.with_runtime_types(s, {"types": {"x": {"str": 5}}})["params"][0]["annotation"] == "int"


def test_inferred_types_give_real_inputs(tmp_path):
    items, reason = inputs.build(sym("rate"), {"types": {"rate": {"float": 4}}}, 200, tmp_path)
    values = [lg_codec.decode(a["value"]) for i in items for a in i["args"]]
    assert reason is None and values and all(isinstance(v, float) for v in values)
    assert not any(i.get("weak") for i in items)


def test_harvest_plugin_records_types(tmp_path):
    root = write_tree(tmp_path / "proj", {
        "shop/__init__.py": "",
        "shop/pricing.py": "def discount(price, rate):\n    return round(price * (1 - rate), 2)\n",
        "tests/test_pricing.py": "from shop.pricing import discount\n\ndef test_d():\n    assert discount(100.0, 0.25) == 75.0\n    assert discount(10, 0.5) == 5.0\n",
    })
    shim = tmp_path / "shim"
    shim.mkdir()
    for name in ("lg_codec.py", "legacyguard_harvest.py"):
        shutil.copy(PLUGIN / "runner" / name, shim / name)
    out = tmp_path / "harvest.json"
    env = {**os.environ, "PYTHONPATH": f"{shim}{os.pathsep}{root}", "LG_HARVEST_OUT": str(out), "LG_HARVEST_ROOT": str(root)}
    res = subprocess.run([TARGET_PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "legacyguard_harvest"], cwd=root, env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    rec = json.loads(out.read_text())["shop/pricing.py::discount"]
    assert rec["types"]["price"] == {"float": 1, "int": 1}
    assert rec["types"]["rate"] == {"float": 2}
    assert len(rec["inputs"]) == 2
