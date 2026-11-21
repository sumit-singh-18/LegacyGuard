"""Read HTTP routes from decorator text: one parser for the static route check and the index fallback (Sprint 7.2d).

Accepts `owner.get/post/put/patch/delete(path, ...)` (Flask 2.0+, FastAPI, Starlette-style routers) and
`owner.route(path, methods=[...])` / `owner.api_route(...)`. `route` and `api_route` with no `methods` are GET, the
documented default of both frameworks; HEAD and OPTIONS, which Flask adds by itself, are left out. A `methods` value that
is not a literal list, tuple or set of strings gives no rows: the methods are unknown, and nothing is guessed.
"""
from __future__ import annotations

import ast

SHORTCUTS = ("get", "post", "put", "patch", "delete")
GENERAL = ("route", "api_route")
IMPLICIT = ("HEAD", "OPTIONS")


def _owner(node) -> str | None:
    """'app', 'bp', 'api.router' for a plain name or attribute chain; None for anything else (calls, subscripts)."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _string(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def parse(dec: str) -> list[tuple[str, str, str]]:
    """[(owner, METHOD, path)] for one decorator's text (without the '@'); [] when it is not a route decorator."""
    try:
        node = ast.parse(dec.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return []
    attr = node.func.attr.lower()
    if attr not in SHORTCUTS and attr not in GENERAL:
        return []
    owner = _owner(node.func.value)
    if owner is None:
        return []
    keywords = {k.arg: k.value for k in node.keywords if k.arg}
    path_node = node.args[0] if node.args else keywords.get("rule", keywords.get("path"))
    path = _string(path_node) if path_node is not None else None
    if path is None or not (path == "" or path.startswith("/")):
        return []
    if attr in SHORTCUTS:
        methods = [attr.upper()]
    elif "methods" not in keywords:
        methods = ["GET"]
    else:
        given = keywords["methods"]
        if not isinstance(given, (ast.List, ast.Tuple, ast.Set)):
            return []
        values = [_string(e) for e in given.elts]
        if any(v is None for v in values):
            return []
        methods = [v.upper() for v in values]
    out = []
    for m in dict.fromkeys(methods):
        if m not in IMPLICIT:
            out.append((owner, m, path))
    return out
