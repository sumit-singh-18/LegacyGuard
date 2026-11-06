"""SQLite index: the four lookup tables (D-005, D-019). Schema from ARCHITECTURE.md §5."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, blob_sha TEXT, indexed_at TEXT);
CREATE TABLE IF NOT EXISTS symbols(id INTEGER PRIMARY KEY, qualname TEXT, name TEXT, kind TEXT, file TEXT, line_start INT, line_end INT,
    signature TEXT, signature_hash TEXT, decorators TEXT, params TEXT);
CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS symbols_file ON symbols(file);
CREATE TABLE IF NOT EXISTS calls(caller_symbol_id INT, called_name TEXT, file TEXT, line INT);
CREATE INDEX IF NOT EXISTS calls_name ON calls(called_name);
CREATE INDEX IF NOT EXISTS calls_file ON calls(file);
CREATE TABLE IF NOT EXISTS test_cover(test_id TEXT, file TEXT, qualname TEXT);
CREATE INDEX IF NOT EXISTS cover_symbol ON test_cover(file, qualname);
CREATE TABLE IF NOT EXISTS routes(method TEXT, path TEXT, handler_qualname TEXT, handler_file TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS contracts(kind TEXT, path TEXT, item TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS cochange(file_a TEXT, file_b TEXT, support INT, confidence REAL);
CREATE INDEX IF NOT EXISTS cochange_a ON cochange(file_a);
"""


def db_path(root: str) -> Path:
    return Path(root) / ".legacyguard" / "index.db"


def connect(root: str) -> sqlite3.Connection:
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def exists(root: str) -> bool:
    return db_path(root).exists()


def set_meta(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
