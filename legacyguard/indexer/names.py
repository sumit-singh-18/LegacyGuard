"""Names table (FR-04). Files are parsed independently; incremental by git blob SHA (FR-08)."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from guardlib import paths, symbols


def list_python_files(root: str, scope: str) -> list[str]:
    """Tracked and untracked (not ignored) .py files in scope, repo-relative."""
    res = subprocess.run(["git", "-C", root, "ls-files", "-co", "--exclude-standard", "*.py"], capture_output=True, text=True, check=True)
    return sorted({f for f in res.stdout.splitlines() if paths.in_scope(f, scope) and not f.startswith(".legacyguard/")
                   and (Path(root) / f).is_file()})


def blob_shas(root: str, files: list[str]) -> dict[str, str]:
    if not files:
        return {}
    res = subprocess.run(["git", "-C", root, "hash-object", "--stdin-paths"], input="\n".join(files), capture_output=True, text=True, check=True)
    return dict(zip(files, res.stdout.split()))


def index_file(conn, root: str, rel: str) -> None:
    source = (Path(root) / rel).read_bytes()
    defs, calls = symbols.extract(source)
    conn.execute("DELETE FROM calls WHERE file = ?", (rel,))
    conn.execute("DELETE FROM symbols WHERE file = ?", (rel,))
    ids: dict[str, int] = {}
    for s in defs:
        cur = conn.execute(
            "INSERT INTO symbols(qualname, name, kind, file, line_start, line_end, signature, signature_hash, decorators, params)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s.qualname, s.name, s.kind, rel, s.line_start, s.line_end, s.signature, s.signature_hash,
             json.dumps(s.decorators), json.dumps(s.params)))
        ids[s.qualname] = cur.lastrowid
    conn.executemany("INSERT INTO calls(caller_symbol_id, called_name, file, line) VALUES (?,?,?,?)",
                     [(ids.get(c.caller), c.called_name, rel, c.line) for c in calls])


def update(conn, root: str, scope: str) -> dict:
    """Re-parse only files whose blob SHA changed. Returns counts."""
    files = list_python_files(root, scope)
    current = blob_shas(root, files)
    known = {r["path"]: r["blob_sha"] for r in conn.execute("SELECT path, blob_sha FROM files")}
    changed = [f for f, sha in current.items() if known.get(f) != sha]
    removed = [f for f in known if f not in current]
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for f in changed:
        index_file(conn, root, f)
        conn.execute("INSERT OR REPLACE INTO files(path, blob_sha, indexed_at) VALUES (?,?,?)", (f, current[f], stamp))
    for f in removed:
        conn.execute("DELETE FROM calls WHERE file = ?", (f,))
        conn.execute("DELETE FROM symbols WHERE file = ?", (f,))
        conn.execute("DELETE FROM files WHERE path = ?", (f,))
    return {"files": len(files), "parsed": len(changed), "removed": len(removed)}
