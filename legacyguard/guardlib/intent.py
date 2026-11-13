"""Declared changes: Claude states which behavior change it intends, with examples the guard checks (roadmap D1).

Without this, an intended change wakes Claude with a "behavior difference", and Claude can only answer in prose.
With `declare_change`, Claude names the function, describes the change and gives examples such as
`{"call": "parse_int_list('5-8')", "returns": "[5, 6, 7]"}`. The guard runs the examples on the current code at
once and reports what holds. From then on, in this session, differences in that function are recorded as intended
(no wake) as long as every declared example still holds; if one stops holding, Claude is woken. Differences in
other functions still wake Claude, and every declaration is listed in the report for the developer, who still
decides: a declaration is a checked statement, not a waiver.
"""
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

from guardlib import ledger, symbols
from runner import examples as ex
from runner import lg_codec


class IntentError(ValueError):
    pass


def resolve(cfg, function: str) -> tuple[str, str]:
    """(file, qualname) for "path/to/file.py::qualname" or a qualname that is unique in the index."""
    if "::" in function:
        file, qualname = function.split("::", 1)
        if not (Path(cfg.root) / file).is_file():
            raise IntentError(f"{file} does not exist in this project")
        return file, qualname
    from indexer import db
    if not db.exists(cfg.root):
        raise IntentError("give the function as path/to/file.py::name (there is no index to look it up)")
    conn = db.connect(cfg.root)
    try:
        rows = conn.execute("SELECT DISTINCT file, qualname FROM symbols WHERE qualname = ? OR name = ?", (function, function)).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        found = ", ".join(f"{r[0]}::{r[1]}" for r in rows[:5]) or "nothing"
        raise IntentError(f"'{function}' matched {found}; give it as path/to/file.py::qualname")
    return rows[0][0], rows[0][1]


def _params(cfg, file: str, qualname: str) -> list[str]:
    text = (Path(cfg.root) / file).read_text(errors="replace")
    sym = next((s for s in symbols.extract(text)[0] if s.qualname == qualname), None)
    if sym is None:
        raise IntentError(f"{qualname} is not defined in {file}")
    return [p["name"] for p in sym.params if p["name"] not in ("self", "cls") and not p["name"].startswith("*")]


def parse_example(qualname: str, params: list[str], example: dict) -> dict:
    """One worker input with "expect": {"k": "return", "v": canon} or {"k": "raise", "type": name}."""
    name = qualname.rsplit(".", 1)[-1]
    text = str(example.get("call", "")).strip()
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise IntentError(f"cannot read the call {text!r}: {exc.msg}") from exc
    func = getattr(node, "func", None)
    if not isinstance(node, ast.Call) or (getattr(func, "id", None) != name and getattr(func, "attr", None) != name):
        raise IntentError(f"{text!r} is not a call to {name}")
    args = []
    for i, a in enumerate(node.args):
        value = ex._literal(a)
        if value is ex._NO or i >= len(params):
            raise IntentError(f"{text!r}: positional argument {i + 1} is not a plain literal value")
        args.append({"name": params[i], "kind": "pos", "value": lg_codec.encode_input(value)})
    for k in node.keywords:
        value = ex._literal(k.value)
        if value is ex._NO or k.arg not in params:
            raise IntentError(f"{text!r}: keyword {k.arg} is not a parameter of {name} with a plain literal value")
        args.append({"name": k.arg, "kind": "kw", "value": lg_codec.encode_input(value)})
    if "raises" in example:
        expect = {"k": "raise", "type": str(example["raises"]).split(".")[-1].split("(")[0].strip()}
    elif "returns" in example:
        raw = example["returns"]
        if isinstance(raw, str):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                pass  # a plain string result, given without quotes
        expect = {"k": "return", "v": lg_codec.canon(raw)}
    else:
        raise IntentError(f"{text!r}: say what it returns (\"returns\") or which exception it raises (\"raises\")")
    return {"args": args, "source": "declared", "expect": expect, "call": text}


def matches(expect: dict, outcome: dict) -> bool:
    if expect["k"] == "raise":
        return outcome.get("outcome") == "raise" and outcome.get("type") == expect["type"]
    return outcome.get("outcome") == "return" and outcome.get("value") == expect["v"]


def describe(expect: dict) -> str:
    return f"raises {expect['type']}" if expect["k"] == "raise" else f"returns {lg_codec.display(expect['v'], plain_decimal=True)}"


def got_text(outcome: dict) -> str:
    if outcome.get("outcome") == "return":
        return f"returns {lg_codec.display(outcome['value'], plain_decimal=True)}"
    if outcome.get("outcome") == "raise":
        return f"raises {outcome['type']}: {outcome.get('message', '')[:120]}"
    return outcome.get("outcome", "did not run")


def run_examples(cfg, file: str, qualname: str, items: list[dict]) -> list[dict]:
    """The declared examples on the current working tree. [{call, expected, got, ok}]."""
    from guardlib import config as cfgmod
    from runner import diff
    module, sys_rel = diff.module_for(file, cfg.root)
    paths = list(dict.fromkeys([*(cfg.raw.get("source_roots") or []), sys_rel, ""]))
    roots = [str(Path(cfg.root) / p) if p else cfg.root for p in paths]
    spec = {"module": module, "qualname": qualname, "inputs": [{k: v for k, v in i.items() if k != "expect"} for i in items]}
    out = diff._run_worker(cfg, cfgmod.target_python(cfg), cfg.root, roots, [spec], float(cfg.behavioral_diff["timeout_s"]))
    res = out.get(f"{module}:{qualname}") or {}
    if res.get("status") != "ran":
        reason = out.get(diff.FAILED) or res.get("reason") or res.get("status") or "it could not be run"
        return [{"call": i["call"], "expected": describe(i["expect"]), "got": f"not run: {reason}", "ok": False} for i in items]
    return [{"call": i["call"], "expected": describe(i["expect"]), "got": got_text(o), "ok": matches(i["expect"], o)}
            for i, o in zip(items, res["results"])]


def declare(cfg, function: str, description: str, examples: list[dict]) -> str:
    """Check the examples now, store the declaration, mark open differences in the function as intended if they hold."""
    if not examples:
        raise IntentError("give at least one example: {\"call\": \"f(1)\", \"returns\": \"2\"}")
    file, qualname = resolve(cfg, function)
    params = _params(cfg, file, qualname)
    items = [parse_example(qualname, params, e) for e in examples[:20]]
    checked = run_examples(cfg, file, qualname, items)
    ok = all(c["ok"] for c in checked)
    key = f"{file}::{qualname}"
    with ledger.locked(cfg.state_dir) as data:
        data.setdefault("declarations", {})[key] = {"description": description.strip()[:500], "examples": items,
                                                    "time": dt.datetime.now().isoformat(timespec="seconds"), "held": ok}
        marked = 0
        if ok:
            for f in data["findings"]:
                if f["kind"] == "behavior_difference" and f["state"] == "open" and f["file"] == file and f["item"] == qualname:
                    f["state"], f["intent"] = "intended", description.strip()[:500]
                    marked += 1
    lines = [f"Declared change to {key}: {description.strip()}"]
    lines += [f"  {'ok  ' if c['ok'] else 'FAIL'} {c['call']}: expected {c['expected']}, got {c['got']}" for c in checked]
    if ok:
        lines.append(f"All {len(checked)} examples hold on the current code. Differences in {qualname} are now recorded as intended "
                     f"while these examples keep holding{f'; {marked} open difference(s) marked intended' if marked else ''}. "
                     "Differences in other functions are still reported. The developer sees this declaration in the report.")
    else:
        lines.append("Not every example holds, so nothing was marked intended. Fix the code or the examples and declare again.")
    return "\n".join(lines)


def declaration_for(data: dict, file: str, qualname: str) -> dict | None:
    return (data.get("declarations") or {}).get(f"{file}::{qualname}")
