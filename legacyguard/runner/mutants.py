"""Planted bugs (mutants) on the changed lines of the old function, to measure how strong a pass is (roadmap B6).

A pass says "old and new agreed on these inputs". Whether that means anything depends on the inputs: if they cannot
tell `x < limit` from `x <= limit`, they would not have caught that bug in the edit either. So after a pass the old
function is run again with small bugs planted in exactly the lines the edit changed. The share of planted bugs the
same inputs catch is the confidence reported with the pass. Mutation score predicts fault detection far better than
coverage (Meta's ACH; MutGen found suites with 100% coverage and a 4% mutation score).

Only plain functions and methods are mutated: the source is compiled again in the module's namespace, which
decorators, closures and zero-argument super() do not survive. The worker first runs the unmutated recompiled source
(the identity) and gives up if that alone behaves differently from the real function.
"""
from __future__ import annotations

import ast
import copy
import textwrap

MAX_MUTANTS = 8
_CMP_SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
             ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
_BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv, ast.Div: ast.Mult, ast.FloorDiv: ast.Mult,
             ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult}
_BOOL_SWAP = {ast.And: ast.Or, ast.Or: ast.And}


def _in_gate(node, start: int, gate: list[list[int]]) -> bool:
    line = getattr(node, "lineno", None)
    if line is None:
        return False
    absolute = line + start - 1
    return any(a <= absolute <= b for a, b in gate)


def _sites(fn: ast.AST, start: int, gate: list[list[int]]) -> list[tuple]:
    """(kind, path) for every mutable spot on a changed line, in source order. path finds the node in a fresh copy."""
    sites = []
    for path, node in _walk(fn, ()):
        if not _in_gate(node, start, gate):
            continue
        if isinstance(node, ast.Compare):
            sites += [("cmp", path, i) for i, op in enumerate(node.ops) if type(op) in _CMP_SWAP]
        elif isinstance(node, ast.BinOp) and type(node.op) in _BIN_SWAP:
            sites.append(("bin", path, None))
        elif isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
            sites.append(("bool", path, None))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            sites.append(("not", path, None))
        elif isinstance(node, ast.Constant) and type(node.value) in (int, float) and not isinstance(node.value, bool):
            sites.append(("const", path, None))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            sites.append(("flipbool", path, None))
        elif isinstance(node, ast.Return) and node.value is not None:
            sites.append(("return", path, None))
    return sites


def _walk(node, path):
    yield path, node
    for field, value in ast.iter_fields(node):
        if isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, ast.AST):
                    yield from _walk(item, path + ((field, i),))
        elif isinstance(value, ast.AST):
            yield from _walk(value, path + ((field, None),))


def _get(root, path):
    node = root
    for field, i in path:
        node = getattr(node, field) if i is None else getattr(node, field)[i]
    return node


def _set(root, path, new):
    parent = _get(root, path[:-1])
    field, i = path[-1]
    if i is None:
        setattr(parent, field, new)
    else:
        getattr(parent, field)[i] = new


def _apply(fn: ast.AST, site) -> tuple[ast.AST, str] | None:
    kind, path, extra = site
    tree = copy.deepcopy(fn)
    node = _get(tree, path)
    if kind == "cmp":
        old = node.ops[extra]
        node.ops[extra] = _CMP_SWAP[type(old)]()
        return tree, f"{type(old).__name__} -> {type(node.ops[extra]).__name__}"
    if kind == "bin":
        old = node.op
        node.op = _BIN_SWAP[type(old)]()
        return tree, f"{type(old).__name__} -> {type(node.op).__name__}"
    if kind == "bool":
        old = node.op
        node.op = _BOOL_SWAP[type(old)]()
        return tree, f"{type(old).__name__} -> {type(node.op).__name__}"
    if kind == "not":
        _set(tree, path, node.operand)
        return tree, "not removed"
    if kind == "const":
        node.value = node.value + 1
        return tree, f"{node.value - 1!r} -> {node.value!r}"
    if kind == "flipbool":
        node.value = not node.value
        return tree, f"{not node.value} -> {node.value}"
    if kind == "return":
        node.value = ast.Constant(value=None)
        return tree, "returns None"
    return None


def generate(source: str, start_line: int, gate: list[list[int]], limit: int = MAX_MUTANTS) -> dict | None:
    """{"name", "identity": source, "mutants": [{"source", "change"}]} for the old function's text, or None when it
    cannot be mutated (decorated, nested, unparseable, or no mutable spot on a changed line).

    start_line is the function's first line in the old file; gate holds the changed statements as absolute lines."""
    if not gate:
        return None
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    fn = tree.body[0] if tree.body else None
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.decorator_list:
        return None
    # tree-sitter's text starts at the def line, so line 1 of the parsed text is start_line in the file.
    sites = _sites(fn, start_line, gate)
    if not sites:
        return None
    # Spread the picks over the changed lines instead of taking the first few spots of the first line.
    step = max(1, len(sites) // limit)
    picked = sites[::step][:limit]
    out = []
    for site in picked:
        made = _apply(fn, site)
        if made is None:
            continue
        mutated, change = made
        try:
            out.append({"source": ast.unparse(ast.fix_missing_locations(mutated)), "change": change})
        except Exception:  # noqa: BLE001 - a mutant we cannot print is simply left out
            continue
    if not out:
        return None
    return {"name": fn.name, "identity": ast.unparse(fn), "mutants": out}
