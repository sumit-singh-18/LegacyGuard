"""Whole API routes, old against new (roadmap C3).

When a changed function (or one of its callers) is a route handler, the guard also sends the same HTTP requests to
the app in the baseline and in the working tree, in process (Flask's test client, Starlette/FastAPI's TestClient),
and compares status, content type and body. That is the level where an API change hurts its consumers, and it
covers handlers that take a Request or a pydantic body the function-level diff cannot build.

Requests come from the project's tests first (literal `client.get("/items/1")` and `.post(..., json={...})`
calls), then from the route itself: path parameters filled from the handler's type hints, and query parameters
varied one at a time.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from runner import examples, lg_codec

MAX_REQUESTS = 24
METHODS = ("get", "post", "put", "patch", "delete")
# FastAPI/Starlette {name} or {name:path}; Flask <name> or <int:name>
_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}|<(?:(\w+):)?(\w+)>")
SAMPLES = {"int": [1, 2, 0, 42], "float": [1.5, 0.0], "str": ["abc", "x-1", "Hello"], "bool": [True, False], "": ["1", "abc"]}


def path_params(path: str) -> list[tuple[str, str]]:
    """[(name, converter)] in a route path. The converter is Flask's (int, float, path...) or ''."""
    out = []
    for m in _PARAM.finditer(path):
        if m.group(1):
            out.append((m.group(1), ""))
        else:
            out.append((m.group(3), m.group(2) or ""))
    return out


def template_regex(path: str) -> re.Pattern:
    """The route path as a regex that matches concrete paths: /items/{id} matches /items/7."""
    out, last = "", 0
    for m in _PARAM.finditer(path):
        out += re.escape(path[last:m.start()]) + "[^/]+"
        last = m.end()
    return re.compile("^" + out + re.escape(path[last:]) + "$")


def fill(path: str, values: dict) -> str:
    return _PARAM.sub(lambda m: str(values.get(m.group(1) or m.group(3), "1")), path)


def _hint(annotation: str | None, converter: str) -> str:
    ann = (annotation or "").replace(" ", "")
    for t in ("int", "float", "bool", "str"):
        if ann == t or ann.startswith(f"{t}|") or converter == t:
            return t
    return ""


def _item(method: str, path: str, source: str, body=None, params=None) -> dict | None:
    args = [{"name": "method", "kind": "kw", "value": lg_codec.encode_input(method)},
            {"name": "path", "kind": "kw", "value": lg_codec.encode_input(path)}]
    try:
        if body is not None:
            args.append({"name": "json", "kind": "kw", "value": lg_codec.encode_input(body)})
        if params:
            args.append({"name": "params", "kind": "kw", "value": lg_codec.encode_input(params)})
    except Exception:  # noqa: BLE001
        return None
    return {"args": args, "source": source}


def from_tests(root: str, method: str, path: str, files: list[str]) -> list[dict]:
    """Literal requests in the project's files that match this route: client.get("/items/1"), .post(..., json=...)."""
    pattern = template_regex(path)
    out = []
    for rel in files:
        try:
            tree = ast.parse((Path(root) / rel).read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method.lower() and node.args):
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            url, _, query = first.value.partition("?")
            if not pattern.match(url):
                continue
            kw = {k.arg: examples._literal(k.value) for k in node.keywords if k.arg in ("json", "params")}
            if any(v is examples._NO for v in kw.values()):
                continue
            params = kw.get("params") or (dict(p.split("=", 1) for p in query.split("&") if "=" in p) if query else None)
            item = _item(method, url, "tests", kw.get("json"), params)
            if item:
                out.append(item)
    return out


def generated(method: str, path: str, handler_params: list[dict]) -> list[dict]:
    """Path parameters from the handler's hints; for GET, each simple query parameter varied once."""
    by_name = {p["name"]: p for p in handler_params}
    pp = path_params(path)
    choices = [SAMPLES[_hint((by_name.get(n) or {}).get("annotation"), conv)] for n, conv in pp]
    out = []
    rows = max([len(c) for c in choices] or [1])
    for i in range(rows):
        values = {n: c[i % len(c)] for (n, _), c in zip(pp, choices)}
        item = _item(method, fill(path, values), "generated")
        if item:
            out.append(item)
    if method == "GET":
        names = {n for n, _ in pp}
        for p in handler_params:
            hint = _hint(p.get("annotation"), "")
            if p["name"] in names or p["name"] in ("self", "request") or not hint or "Depends" in (p.get("default") or ""):
                continue
            base = fill(path, {n: SAMPLES[_hint((by_name.get(n) or {}).get("annotation"), c)][0] for n, c in pp})
            for v in SAMPLES[hint][:2]:
                item = _item(method, base, "generated", None, {p["name"]: v})
                if item:
                    out.append(item)
    return out


def requests_for(root: str, method: str, path: str, handler_params: list[dict]) -> list[dict]:
    if method == "ANY":
        return []
    prefix = path.split("{", 1)[0].split("<", 1)[0].rstrip("/") or "/"
    files = [f for f in examples.files_naming(root, prefix) if "test" in f.lower()]
    items = from_tests(root, method, path, files) + generated(method, path, handler_params)
    seen, out = set(), []
    for item in items:
        key = json.dumps(item["args"], sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out[:MAX_REQUESTS]


def routes_for(conn, handlers: set[tuple[str, str]]) -> list[tuple[str, str, str, str]]:
    """(method, path, handler file, handler qualname) for routes whose handler is one of the (file, qualname) pairs."""
    if conn is None or not handlers:
        return []
    try:
        rows = conn.execute("SELECT method, path, handler_file, handler_qualname FROM routes").fetchall()
    except Exception:  # noqa: BLE001
        return []
    return sorted({(m, p, f, q) for m, p, f, q in rows if (f, q) in handlers and m != "ANY"})


def handler_params(conn, file: str, qualname: str) -> list[dict]:
    try:
        row = conn.execute("SELECT params FROM symbols WHERE file = ? AND qualname = ?", (file, qualname)).fetchone()
        return json.loads(row[0]) if row and row[0] else []
    except Exception:  # noqa: BLE001
        return []
