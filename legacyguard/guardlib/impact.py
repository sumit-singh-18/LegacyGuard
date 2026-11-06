"""Who depends on these functions? (FR-09) Every item carries its source. Shared by hooks and the MCP server."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

MAX_CHARS = 1500
HOPS = 3
CALLER_SHOWN = 8
COCHANGE_MIN = 0.3


@dataclass
class Impact:
    symbol: str
    file: str | None
    callers: list[dict] = field(default_factory=list)
    routes: list[dict] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    cochange: list[dict] = field(default_factory=list)
    frozen: bool = False
    contract_files: list[str] = field(default_factory=list)
    coverage_known: bool = True

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def find_symbols(conn, symbol: str) -> list:
    """Accepts name, qualname, or file::qualname. All same-named matches come back (source: names)."""
    if "::" in symbol:
        file, qual = symbol.split("::", 1)
        return conn.execute("SELECT * FROM symbols WHERE file = ? AND qualname = ?", (file, qual)).fetchall()
    rows = conn.execute("SELECT * FROM symbols WHERE qualname = ? AND kind != 'class'", (symbol,)).fetchall()
    return rows or conn.execute("SELECT * FROM symbols WHERE name = ? AND kind != 'class'", (symbol.rsplit(".", 1)[-1],)).fetchall()


def _is_test(path: str) -> bool:
    from indexer.coverage_map import is_test_path
    return is_test_path(path)


def _callers_of(conn, name: str) -> list:
    return conn.execute("""SELECT DISTINCT s.qualname, s.name, s.file FROM calls c JOIN symbols s ON c.caller_symbol_id = s.id
                           WHERE c.called_name = ?""", (name,)).fetchall()


def compute(conn, cfg, symbol: str, depth: int = HOPS) -> Impact | None:
    rows = find_symbols(conn, symbol)
    if not rows:
        return None
    target = rows[0]
    imp = Impact(symbol=target["qualname"], file=target["file"])
    seen: OrderedDict[tuple, dict] = OrderedDict()
    frontier = {r["name"] for r in rows}
    done = set(frontier)
    for hop in range(1, max(1, depth) + 1):
        next_frontier = set()
        for name in sorted(frontier):
            for c in _callers_of(conn, name):
                key = (c["file"], c["qualname"])
                if key in seen or _is_test(c["file"]) or key == (target["file"], target["qualname"]):
                    continue
                seen[key] = {"qualname": c["qualname"], "file": c["file"], "hop": hop, "sources": ["names"], "via": name}
                if c["name"] not in done:
                    done.add(c["name"])
                    next_frontier.add(c["name"])
        frontier = next_frontier
    covered = {(r["file"], r["qualname"]) for r in conn.execute("SELECT DISTINCT file, qualname FROM test_cover")}
    imp.coverage_known = bool(covered)
    co = {r["file_b"]: r for r in conn.execute("SELECT file_b, support, confidence FROM cochange WHERE file_a = ?", (target["file"],))}
    for item in seen.values():
        if (item["file"], item["qualname"]) in covered:
            item["sources"].append("tests")
        elif imp.coverage_known:
            item["no_test"] = True
        if item["file"] in co:
            item["sources"].append(f"history {round(co[item['file']]['confidence'] * 100)}%")
    imp.callers = list(seen.values())

    handlers = {(target["file"], target["qualname"])} | set(seen)
    for r in conn.execute("SELECT method, path, handler_qualname, handler_file, source FROM routes"):
        if (r["handler_file"], r["handler_qualname"]) in handlers:
            imp.routes.append({"route": f"{r['method']} {r['path']}", "sources": ["routes"],
                               "handler_file": r["handler_file"], "handler_qualname": r["handler_qualname"]})
    imp.routes.sort(key=lambda r: r["route"])
    imp.tests = sorted({r["test_id"] for s in rows for r in conn.execute(
        "SELECT test_id FROM test_cover WHERE file = ? AND qualname = ?", (s["file"], s["qualname"]))})
    imp.cochange = [{"file": f, "confidence": r["confidence"], "support": r["support"], "sources": [f"history {round(r['confidence'] * 100)}%"]}
                    for f, r in sorted(co.items(), key=lambda kv: -kv[1]["confidence"]) if r["confidence"] >= COCHANGE_MIN and not _is_test(f)]
    from guardlib.paths import matches_any
    imp.frozen = matches_any(target["file"], cfg.raw.get("frozen") or [])
    imp.contract_files = sorted({r["path"] for r in conn.execute("SELECT DISTINCT path FROM contracts") if target["file"] == r["path"]})
    return imp


def render(imp: Impact, limit: int = MAX_CHARS) -> str:
    """The FR-09 text form. Stays under the limit by truncating with a count."""
    lines = []
    parts = []
    for c in imp.callers[:CALLER_SHOWN]:
        note = " no test reaches it" if c.get("no_test") else ""
        parts.append(f"{c['qualname']}() [{','.join(c['sources'])}]{note}")
    more = len(imp.callers) - CALLER_SHOWN
    lines.append(f"{imp.symbol} ({imp.file})")
    lines.append("callers  " + ("  ".join(parts) if parts else "none found by name") + (f"  +{more} more" if more > 0 else ""))
    routes = [f"{r['route']} [routes]" for r in imp.routes[:6]]
    extra = len(imp.routes) - 6
    lines.append("routes   " + ("  ".join(routes) if routes else "none") + (f"  +{extra} more" if extra > 0 else ""))
    lines.append(f"tests    {len(imp.tests)} [tests]" if imp.coverage_known else "tests    unknown: no coverage map yet, run /legacyguard:index")
    cos = [f"{c['file']} [history {round(c['confidence'] * 100)}%]" for c in imp.cochange[:4]]
    if cos:
        lines.append("co-change " + "  ".join(cos))
    if imp.frozen:
        lines.append("frozen   yes [rule]")
    text = "\n".join(lines)
    if len(text) > limit:
        suffix = f"\n… truncated, {len(imp.callers)} callers in total"
        text = text[: limit - len(suffix)] + suffix
    return text


def impact_text(conn, cfg, symbol: str, depth: int = HOPS) -> str:
    imp = compute(conn, cfg, symbol, depth)
    return render(imp) if imp else f"{symbol}: not found in the index [names]. Dynamic calls are not indexed."


def render_tree(imp: Impact, max_lines: int = 40) -> str:
    """The same radius as an indented tree: each caller sits under the function it calls. For the developer's terminal."""
    route_of: dict[tuple, list[str]] = {}
    for r in imp.routes:
        route_of.setdefault((r["handler_file"], r["handler_qualname"]), []).append(r["route"])
    children: dict[tuple, list[dict]] = {}
    for c in imp.callers:
        children.setdefault((c["hop"], c["via"]), []).append(c)

    def routes(file, qualname) -> str:
        found = route_of.get((file, qualname))
        return f"  <- {', '.join(found)}" if found else ""

    lines = [f"{imp.symbol}  {imp.file}{routes(imp.file, imp.symbol)}"]

    def walk(name: str, hop: int, prefix: str) -> None:
        kids = children.pop((hop, name), [])  # pop: a caller is listed once, under the first same-named function
        for i, c in enumerate(kids):
            last = i == len(kids) - 1
            note = "  no test reaches it" if c.get("no_test") else ""
            lines.append(f"{prefix}{'`- ' if last else '|- '}{c['qualname']}  {c['file']}  [{','.join(c['sources'])}]{note}{routes(c['file'], c['qualname'])}")
            walk(c["qualname"].rsplit(".", 1)[-1], hop + 1, prefix + ("   " if last else "|  "))

    walk(imp.symbol.rsplit(".", 1)[-1], 1, "")
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(imp.callers) + 1 - max_lines} more, see the session map"]
    return "\n".join(lines)
