"""Who depends on these functions? (FR-09) Every item carries its source. Shared by hooks and the MCP server.

Two ways to find callers (Sprint 7.1):
- "resolved": through imports (guardlib/callgraph.py), so a function called total() in another module does not lend
  its callers, and `from pkg.a import total as t; t()` is found. Used by the impact MCP tool, /legacyguard:impact and
  the session map. Sources per caller: "resolved", or "name" for a bare call of a name defined once in the project.
- "names": the index's `calls` table matched by called name. Fast (no file reads), so the hooks with a 1 s budget
  (pre_edit, prompt_context) use it. It over-matches same-named functions and misses aliases. Source "names".
If the resolved search raises, or its deadline passes, compute falls back to names and says so in `note`
(`fallback` is "error" or "budget")."""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

MAX_CHARS = 1500
HOPS = 3
CALLER_SHOWN = 8
COCHANGE_MIN = 0.3
RESOLVED_LIMIT = 100   # callers per matched definition in resolved mode; reaching it marks the result truncated
MODES = ("resolved", "names")
INTERACTIVE_BUDGET_S = 20.0  # the MCP tool and /legacyguard:impact: past this, callers are matched by name, with a note


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
    not_followed: list[str] = field(default_factory=list)  # names too common to follow callers through
    mode: str = "names"            # how callers were found: "resolved" or "names" (also "names" after a fallback)
    note: str = ""                 # why the mode differs from the one asked for, e.g. the resolved search failed
    fallback: str = ""             # "" (none), "error" (the resolved search raised) or "budget" (it ran out of time)
    truncated: bool = False        # the caller search stopped early, so callers may be missing
    truncated_reason: str = ""
    targets: list[list[str]] = field(default_factory=list)  # every matched definition as [file, qualname]

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


COMMON_NAME_DEFS = 3


def _too_common(conn, name: str) -> bool:
    """A name that says nothing about who is called: __init__, or one defined in many places (put, update, run).

    Following callers through such a name pulls in every class in the repo. On Home Assistant one light's
    __init__ calling a colour helper put the login and token classes on the map."""
    if name.startswith("__") and name.endswith("__"):
        return True
    return conn.execute("SELECT COUNT(*) FROM symbols WHERE name = ?", (name,)).fetchone()[0] > COMMON_NAME_DEFS


def _is_special(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _name_callers(conn, rows, target, depth: int) -> tuple[OrderedDict, set]:
    """Callers matched by called name in the index, hop by hop (the "names" mode)."""
    seen: OrderedDict[tuple, dict] = OrderedDict()
    frontier = {r["name"] for r in rows}
    done = set(frontier)
    not_followed: set[str] = set()
    for hop in range(1, max(1, depth) + 1):
        next_frontier = set()
        for name in sorted(frontier):
            for c in _callers_of(conn, name):
                key = (c["file"], c["qualname"])
                if key in seen or _is_test(c["file"]) or key == (target["file"], target["qualname"]):
                    continue
                seen[key] = {"qualname": c["qualname"], "file": c["file"], "hop": hop, "sources": ["names"], "via": name, "via_file": None}
                if c["name"] not in done:
                    done.add(c["name"])
                    if _too_common(conn, c["name"]):
                        not_followed.add(c["name"])
                    else:
                        next_frontier.add(c["name"])
        frontier = next_frontier
    return seen, not_followed


class _BudgetSpent(Exception):
    """The resolved search passed its deadline; its partial result is not used."""


def _resolved_callers(conn, cfg, rows, target, depth: int, limit: int, imp: Impact,
                      cache: dict | None = None, deadline: float | None = None) -> tuple[OrderedDict, set]:
    """Callers resolved through imports from each matched definition (the "resolved" mode).

    _too_common does not apply here: a common name (put, update, run) no longer pulls in every same-named method,
    because each step resolves to one definition. Special methods (__init__, __call__) are still not followed: they
    are reached through constructor calls and operators, which the call graph does not resolve, so following them
    would read every file that has a class and find almost nothing. They are listed in not_followed."""
    from guardlib import callgraph
    seen: OrderedDict[tuple, dict] = OrderedDict()
    not_followed: set[str] = set()
    stats: dict = {}
    cache = {} if cache is None else cache  # parsed files are shared between steps and matched definitions

    def follow(edge) -> bool:
        name = edge.qualname.rsplit(".", 1)[-1]
        if _is_special(name):
            not_followed.add(name)
            return False
        return True

    for r in rows:
        for e in callgraph.callers(cfg.root, r["file"], r["qualname"], conn, hops=max(1, depth), limit=limit,
                                   skip=_is_test, follow=follow, stats=stats, cache=cache, deadline=deadline):
            key = (e.file, e.qualname)
            if key in seen or key == (target["file"], target["qualname"]):
                continue
            seen[key] = {"qualname": e.qualname, "file": e.file, "hop": e.hops, "sources": [e.how], "via": e.callee, "via_file": e.callee_file,
                         "reaches": [r["file"], r["qualname"]]}  # the matched definition this caller leads to
        if stats.get("deadline_hit"):
            raise _BudgetSpent()
    reasons = []
    if stats.get("capped"):
        reasons.append(f"more than {callgraph.MAX_CANDIDATE_FILES} files mention {', '.join(sorted(stats['capped'])[:3])}, "
                       f"only {callgraph.MAX_CANDIDATE_FILES} were read")
    if stats.get("limit_hit"):
        reasons.append(f"the search stopped at {limit} callers")
    imp.truncated = bool(reasons)
    imp.truncated_reason = "; ".join(reasons)
    return seen, not_followed


BUDGET_NOTE = "the time budget for resolving callers through imports ran out, so callers are matched by name"


def compute(conn, cfg, symbol: str, depth: int = HOPS, mode: str = "names", limit: int = RESOLVED_LIMIT,
            deadline: float | None = None, cache: dict | None = None) -> Impact | None:
    """mode "resolved" (through imports; the MCP tool, the impact skill, the session map) or "names" (the index's name
    matches; the default, for the hooks with a 1 s budget, which must not read files). `limit`, `deadline` (a
    time.monotonic() value) and `cache` (parsed files, shared across calls) apply to resolved. Past the deadline, or
    when the resolved search raises, the result is the names one with `fallback` and `note` set."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    rows = find_symbols(conn, symbol)
    if not rows:
        return None
    target = rows[0]
    imp = Impact(symbol=target["qualname"], file=target["file"], mode=mode, targets=[[r["file"], r["qualname"]] for r in rows])
    if mode == "resolved":
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise _BudgetSpent()
            seen, not_followed = _resolved_callers(conn, cfg, rows, target, depth, limit, imp, cache, deadline)
        except _BudgetSpent:
            imp.mode, imp.truncated, imp.truncated_reason, imp.fallback, imp.note = "names", False, "", "budget", BUDGET_NOTE
            seen, not_followed = _name_callers(conn, rows, target, depth)
        except Exception as exc:  # noqa: BLE001 - a failed import resolution falls back to name matches, never fails
            imp.mode, imp.truncated, imp.truncated_reason, imp.fallback = "names", False, "", "error"
            imp.note = f"resolving callers through imports failed ({type(exc).__name__}), so callers are matched by name"
            seen, not_followed = _name_callers(conn, rows, target, depth)
    else:
        seen, not_followed = _name_callers(conn, rows, target, depth)
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
    imp.not_followed = sorted(not_followed)

    handlers = {(r["file"], r["qualname"]) for r in rows} | set(seen)  # every matched definition, not only the first
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
        reaches = c.get("reaches")
        other = f" -> {reaches[0]}::{reaches[1]}" if reaches and list(reaches) != [imp.file, imp.symbol] else ""
        parts.append(f"{c['qualname']}() [{','.join(c['sources'])}]{other}{note}")
    more = len(imp.callers) - CALLER_SHOWN
    lines.append(f"{imp.symbol} ({imp.file})")
    if len(imp.targets) > 1:
        others = [f"{f}::{q}" for f, q in imp.targets[1:5]]
        extra_t = len(imp.targets) - 5
        how = ("callers of each are listed; '-> file::name' marks callers of another match" if imp.mode == "resolved"
               else "callers matched by name may belong to any of them")
        lines.append("also     matched: " + ", ".join(others) + (f" +{extra_t} more" if extra_t > 0 else "") + f"; {how}")
    none = "none found through imports" if imp.mode == "resolved" else "none found by name"
    lines.append("callers  " + ("  ".join(parts) if parts else none) + (f"  +{more} more" if more > 0 else ""))
    lines += _notes(imp)
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


def _notes(imp: Impact) -> list[str]:
    out = []
    if imp.note:
        out.append(f"note     {imp.note}")
    if imp.truncated:
        out.append(f"note     callers may be incomplete: {imp.truncated_reason}")
    return out


def not_found_text(symbol: str) -> str:
    return f"{symbol}: not found in the index [names]. Dynamic calls are not indexed."


def impact_text(conn, cfg, symbol: str, depth: int = HOPS, mode: str = "names", deadline: float | None = None) -> str:
    imp = compute(conn, cfg, symbol, depth, mode=mode, deadline=deadline)
    return render(imp) if imp else not_found_text(symbol)


def render_tree(imp: Impact, max_lines: int = 40) -> str:
    """The same radius as an indented tree: each caller sits under the function it calls. For the developer's terminal.

    Resolved callers nest under the exact definition they call, (via_file, via); name-matched callers (no via_file)
    nest under the first function with the called name one hop nearer, as before."""
    route_of: dict[tuple, list[str]] = {}
    for r in imp.routes:
        route_of.setdefault((r["handler_file"], r["handler_qualname"]), []).append(r["route"])
    children: dict[tuple, list[dict]] = {}
    for c in imp.callers:
        key = ("file", c["via_file"], c["via"]) if c.get("via_file") else ("hop", c["hop"], c["via"])
        children.setdefault(key, []).append(c)

    def routes(file, qualname) -> str:
        found = route_of.get((file, qualname))
        return f"  <- {', '.join(found)}" if found else ""

    lines = [f"{imp.symbol}  {imp.file}{routes(imp.file, imp.symbol)}"]

    def walk(file: str, qualname: str, hop: int, prefix: str) -> None:
        # pop: a caller is listed once; by name, under the first same-named function
        kids = children.pop(("file", file, qualname), []) + children.pop(("hop", hop, qualname.rsplit(".", 1)[-1]), [])
        for i, c in enumerate(kids):
            last = i == len(kids) - 1
            note = "  no test reaches it" if c.get("no_test") else ""
            lines.append(f"{prefix}{'`- ' if last else '|- '}{c['qualname']}  {c['file']}  [{','.join(c['sources'])}]{note}{routes(c['file'], c['qualname'])}")
            walk(c["file"], c["qualname"], hop + 1, prefix + ("   " if last else "|  "))

    walk(imp.file, imp.symbol, 1, "")
    for file, qualname in imp.targets[1:]:  # other definitions with the same name, each with its own resolved callers
        if ("file", file, qualname) in children:
            lines.append(f"{qualname}  {file}  (same name){routes(file, qualname)}")
            walk(file, qualname, 1, "")
    lines += _notes(imp)
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... {len(imp.callers) + 1 - max_lines} more, see the session map"]
    return "\n".join(lines)
