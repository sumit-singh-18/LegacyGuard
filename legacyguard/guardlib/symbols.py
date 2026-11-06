"""tree-sitter parsing: definitions, call sites, signatures, decorators (FR-04, FR-12)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import tree_sitter_python
from tree_sitter import Language, Parser

_PARSER = Parser(Language(tree_sitter_python.language()))


def parse(source: str | bytes):
    return _PARSER.parse(source.encode() if isinstance(source, str) else source)


def text(node) -> str:
    return node.text.decode("utf8", "replace")


def sha(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()[:16]


@dataclass
class Symbol:
    qualname: str
    kind: str  # function | method | class
    line_start: int
    line_end: int
    signature: str
    signature_hash: str
    body_hash: str
    decorators: list[str] = field(default_factory=list)
    params: list[dict] = field(default_factory=list)
    node_text: str = ""

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]

    @property
    def public(self) -> bool:
        return not self.name.startswith("_")


@dataclass
class Call:
    caller: str | None
    called_name: str
    line: int


def _params(func_node) -> list[dict]:
    out = []
    params = func_node.child_by_field_name("parameters")
    if params is None:
        return out
    for child in params.named_children:
        kind = child.type
        name, annotation, default = None, None, None
        if kind == "identifier":
            name = text(child)
        elif kind in ("typed_parameter", "typed_default_parameter", "default_parameter"):
            nm = child.child_by_field_name("name")
            if nm is None:
                for c in child.named_children:
                    if c.type in ("identifier", "list_splat_pattern", "dictionary_splat_pattern"):
                        nm = c
                        break
            name = text(nm) if nm is not None else None
            ty = child.child_by_field_name("type")
            annotation = text(ty) if ty is not None else None
            val = child.child_by_field_name("value")
            default = text(val) if val is not None else None
        elif kind in ("list_splat_pattern", "dictionary_splat_pattern"):
            name = text(child)
        elif kind in ("keyword_separator", "positional_separator"):
            continue
        if name:
            out.append({"name": name, "annotation": annotation, "default": default})
    return out


def _decorators(node) -> list[str]:
    if node.parent is not None and node.parent.type == "decorated_definition":
        return [text(c).lstrip("@").strip() for c in node.parent.children if c.type == "decorator"]
    return []


def extract(source: str | bytes) -> tuple[list[Symbol], list[Call]]:
    """All definitions and call sites in a file. Works on code that does not fully parse."""
    tree = parse(source)
    symbols: list[Symbol] = []
    calls: list[Call] = []

    def walk(node, scope: list[str], func: str | None, in_class: bool):
        for child in node.children:
            t = child.type
            if t in ("function_definition", "class_definition"):
                nm = child.child_by_field_name("name")
                if nm is None:
                    continue
                qual = ".".join(scope + [text(nm)])
                body = child.child_by_field_name("body")
                sig_end = body.start_byte if body is not None else child.end_byte
                sig = child.text[: sig_end - child.start_byte].decode("utf8", "replace").strip()
                kind = "class" if t == "class_definition" else ("method" if in_class else "function")
                symbols.append(Symbol(qual, kind, child.start_point[0] + 1, child.end_point[0] + 1, sig, sha(sig),
                                      sha(child.text), _decorators(child), _params(child) if t == "function_definition" else [],
                                      text(child)))
                if body is not None:
                    walk(body, scope + [text(nm)], qual if t == "function_definition" else func, t == "class_definition")
                for p in ("parameters", "superclasses"):
                    sub = child.child_by_field_name(p)
                    if sub is not None:
                        walk(sub, scope, func, False)
                continue
            if t == "call":
                fn = child.child_by_field_name("function")
                if fn is not None:
                    name = None
                    if fn.type == "identifier":
                        name = text(fn)
                    elif fn.type == "attribute":
                        attr = fn.child_by_field_name("attribute")
                        name = text(attr) if attr is not None else None
                    if name:
                        calls.append(Call(func, name, child.start_point[0] + 1))
            walk(child, scope, func, in_class)

    walk(tree.root_node, [], None, False)
    return symbols, calls


def has_errors(source: str | bytes) -> bool:
    return parse(source).root_node.has_error
