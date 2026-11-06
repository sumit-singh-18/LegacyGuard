"""Read contracts into {item: detail} maps so before and after can be compared (FR-06, FR-12)."""
from __future__ import annotations

import json
import re
import sqlite3

import yaml


def _ref_name(schema: dict) -> str | None:
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    return ref.rsplit("/", 1)[-1] if ref else None


def _type_of(schema: dict) -> str:
    if not isinstance(schema, dict):
        return "?"
    if "$ref" in schema:
        return _ref_name(schema)
    if "anyOf" in schema:
        return "|".join(sorted(_type_of(s) for s in schema["anyOf"]))
    if schema.get("type") == "array":
        return f"array[{_type_of(schema.get('items', {}))}]"
    return str(schema.get("type", "?"))


def http_items(text: str) -> dict[str, str]:
    """route:METHOD /path, param:METHOD /path:name, field:Schema.prop for an OpenAPI document."""
    spec = yaml.safe_load(text) or {}
    items: dict[str, str] = {}
    for path, ops in (spec.get("paths") or {}).items():
        for method, op in (ops or {}).items():
            if not isinstance(op, dict):
                continue
            route = f"{method.upper()} {path}"
            items[f"route:{route}"] = "route"
            for p in op.get("parameters") or []:
                items[f"param:{route}:{p.get('name')}"] = _type_of(p.get("schema", {}))
    for name, schema in ((spec.get("components") or {}).get("schemas") or {}).items():
        for prop, sub in (schema.get("properties") or {}).items():
            items[f"field:{name}.{prop}"] = _type_of(sub)
    return items


def response_field_names(http: dict[str, str]) -> set[str]:
    return {k.split(".", 1)[1] for k in http if k.startswith("field:")}


def schema_names(http: dict[str, str]) -> set[str]:
    return {k[len("field:"):].split(".", 1)[0] for k in http if k.startswith("field:")}


def event_items(name: str, text: str) -> dict[str, str]:
    """field:<event>.<prop> -> type for one JSON Schema event file."""
    try:
        schema = json.loads(text)
    except ValueError:
        return {}
    event = schema.get("title") or name
    return {f"field:{event}.{p}": str((s or {}).get("type", "?")) for p, s in (schema.get("properties") or {}).items()}


_DANGEROUS = [(re.compile(r"\bDROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?[\"`]?(\w+)", re.I), "column dropped"),
              (re.compile(r"\bRENAME\s+COLUMN\s+[\"`]?(\w+)", re.I), "column renamed"),
              (re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?[\"`]?(\w+)", re.I), "table dropped"),
              (re.compile(r"\bALTER\s+COLUMN\s+[\"`]?(\w+)[\"`]?\s+(?:SET\s+DATA\s+)?TYPE", re.I), "column retyped"),
              (re.compile(r"\bALTER\s+TABLE\s+[\"`]?(\w+)[\"`]?\s+RENAME\s+TO\b", re.I), "table renamed"),
              (re.compile(r"\b(?:migrations\.)?RemoveField\s*\([^)]*name\s*=\s*['\"](\w+)", re.S), "column dropped"),
              (re.compile(r"\b(?:migrations\.)?RenameField\s*\([^)]*old_name\s*=\s*['\"](\w+)", re.S), "column renamed"),
              (re.compile(r"\b(?:migrations\.)?DeleteModel\s*\([^)]*name\s*=\s*['\"](\w+)", re.S), "table dropped"),
              (re.compile(r"\b(?:migrations\.)?RenameModel\s*\([^)]*old_name\s*=\s*['\"](\w+)", re.S), "table renamed")]


def dangerous_statements(text: str) -> dict[str, str]:
    """{statement item: description} found in one migration's text. Works for SQL and Django migrations."""
    no_comments = re.sub(r"--[^\n]*", "", text)
    out: dict[str, str] = {}
    for pattern, label in _DANGEROUS:
        for m in pattern.finditer(no_comments):
            out[f"{label}:{m.group(1)}"] = label
    return out


def sql_schema(scripts: list[str]) -> dict[str, str] | None:
    """Run SQL migrations in a private in-memory database. {column:table.col: type}, or None if they do not run."""
    conn = sqlite3.connect(":memory:")
    try:
        for script in scripts:
            conn.executescript(script)
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        out = {}
        for t in tables:
            for row in conn.execute(f'PRAGMA table_info("{t}")'):
                out[f"column:{t}.{row[1]}"] = str(row[2])
        return out
    except sqlite3.Error:
        return None
    finally:
        conn.close()
