"""Contracts table (FR-06): HTTP, DB and event items named in config."""
from __future__ import annotations

from pathlib import Path

from guardlib import contracts_parse as cp


def _migration_files(root: str, rel_dir: str) -> list[Path]:
    d = Path(root) / rel_dir
    return sorted(p for p in d.iterdir() if p.suffix in (".sql", ".py") and p.name != "__init__.py") if d.is_dir() else []


def rebuild(conn, cfg) -> dict:
    conn.execute("DELETE FROM contracts")
    rows: list[tuple] = []
    http = cfg.contract_path("http")
    if http and (Path(cfg.root) / http).exists():
        rows += [("http", http, k, v) for k, v in cp.http_items((Path(cfg.root) / http).read_text()).items()]
    db = cfg.contract_path("db")
    if db:
        files = _migration_files(cfg.root, db)
        schema = cp.sql_schema([f.read_text() for f in files if f.suffix == ".sql"]) or {}
        rows += [("db", db, k, v) for k, v in schema.items()]
    msg = cfg.contract_path("messages")
    if msg and (Path(cfg.root) / msg).is_dir():
        for f in sorted((Path(cfg.root) / msg).glob("*.json")):
            rel = f"{msg.rstrip('/')}/{f.name}"
            rows += [("messages", rel, k, v) for k, v in cp.event_items(f.stem, f.read_text()).items()]
    conn.executemany("INSERT INTO contracts(kind, path, item, detail) VALUES (?,?,?,?)", rows)
    return {k: sum(1 for r in rows if r[0] == k) for k in ("http", "db", "messages")}


def http_map(conn) -> dict[str, str]:
    return {r["item"]: r["detail"] for r in conn.execute("SELECT item, detail FROM contracts WHERE kind = 'http'")}
