"""Pull queries from Claude over stdio (FR-21). Reads the same SQLite index as the hooks (D-019)."""
from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from guardlib import config as cfgmod
from guardlib import impact as impact_lib
from guardlib.paths import find_root
from indexer import db

server = MCPServer("legacyguard")


def _load():
    from guardlib import projects
    return projects.for_command(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())[0]


@server.tool()
def impact(symbol: str, depth: int = 3) -> str:
    """Who depends on a function: callers up to 3 hops, routes, tests, co-changed files. Each item names its source.

    Name matches can over-match same-named functions and miss dynamic calls.
    """
    try:
        cfg = _load()
    except (cfgmod.ConfigMissing, cfgmod.ConfigError) as exc:
        return f"LegacyGuard is inactive here: {exc}"
    conn = db.connect(cfg.root)
    try:
        return impact_lib.impact_text(conn, cfg, symbol, min(max(depth, 1), impact_lib.HOPS))
    finally:
        conn.close()


@server.tool()
def contract(path: str) -> str:
    """Contract items defined by a file (openapi.yaml, a migrations folder, an event schema) from the index."""
    try:
        cfg = _load()
    except (cfgmod.ConfigMissing, cfgmod.ConfigError) as exc:
        return f"LegacyGuard is inactive here: {exc}"
    conn = db.connect(cfg.root)
    try:
        wanted = path.strip().rstrip("/")
        rows = conn.execute("SELECT kind, path, item, detail FROM contracts WHERE path = ? OR path = ? OR path LIKE ?", (wanted, wanted + "/", wanted + "/%")).fetchall()
        if not rows:
            known = sorted({r["path"] for r in conn.execute("SELECT DISTINCT path FROM contracts")})
            return f"{path}: no contract items indexed. Contract paths: {', '.join(known) or 'none'}."
        lines = [f"{r['kind']}  {r['item']}  {r['detail']}" for r in rows]
        text = "\n".join(lines[:80])
        return text + (f"\n+{len(lines) - 80} more" if len(lines) > 80 else "")
    finally:
        conn.close()


@server.tool()
def diff_report(function: str) -> str:
    """This session's behavioral diff results for a function: status, inputs compared and counterexamples. Results come from execution."""
    try:
        cfg = _load()
    except (cfgmod.ConfigMissing, cfgmod.ConfigError) as exc:
        return f"LegacyGuard is inactive here: {exc}"
    from guardlib import ledger
    data = ledger.load(cfg.state_dir)
    name = function.split("::")[-1]
    rows = [f for f in data["findings"] if f["kind"] in ("behavior_difference", "verified", "untested") and (f["item"] == name or f["item"].rsplit(".", 1)[-1] == name)]
    if not rows:
        return f"{function}: no behavioral diff has run for this function in this session."
    return "\n".join(f"[{f['id']}] {f['status']} ({f['state']}): {f['subject']}. {f['evidence'][:500]}" for f in rows[-5:])


@server.tool()
def declare_change(function: str, description: str, examples: list[dict]) -> str:
    """Declare an intended behavior change, with examples the guard checks on the current code.

    function: "path/to/file.py::qualname" (or a name that is unique in the project).
    examples: [{"call": "parse_int_list('5-8')", "returns": "[5, 6, 7]"}, {"call": "f(-1)", "raises": "ValueError"}],
    with literal arguments only. While every example holds, differences in this function are recorded as intended
    instead of waking you; if one stops holding you are told. The developer sees the declaration in the report.
    """
    try:
        cfg = _load()
    except (cfgmod.ConfigMissing, cfgmod.ConfigError) as exc:
        return f"LegacyGuard is inactive here: {exc}"
    from guardlib import intent
    try:
        return intent.declare(cfg, function, description, examples)
    except intent.IntentError as exc:
        return f"Not declared: {exc}"


def main() -> None:
    server.run("stdio")
