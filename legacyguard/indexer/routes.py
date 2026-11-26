"""Routes table (FR-06, D-030): ask the framework itself; fall back to reading decorators."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from guardlib import config as cfgmod, route_decorators
from indexer.coverage_map import clean_env

RUNNER = Path(__file__).resolve().parent.parent / "runner"

# Sprint 7.2e: the app is loaded by lg_worker.load_app (`module:name` or `module:factory(literal args)`), with the same
# set-up as the worker: lg_worker.prepare (blocks, a run scratch folder, the tree protected) and setup_phase(), so a
# cache-like write while the factory or the URL conf runs is redirected, and the process runs under the OS sandbox.
# Any failure prints no LGROUTES line, and the routes table falls back to the decorators.
INTROSPECT = r"""
import inspect, json, os, sys
sys.path.insert(0, RUNNER_DIR)
import lg_worker
with open(REQUEST) as fh:
    request = json.load(fh)
root_real, django_error = lg_worker.prepare(request, with_django=bool(DJANGO) and not APP)
out = []
def where(fn):
    try: return os.path.realpath(inspect.getsourcefile(inspect.unwrap(fn)))
    except Exception: return ""
with lg_worker.setup_phase():
    if APP:
        app = lg_worker.load_app(APP)
        def add(path, methods, ep, in_schema):
            if ep is None or not in_schema: return
            for m in sorted(methods or []):
                if m not in ("HEAD", "OPTIONS"):
                    out.append([m, path, ep.__qualname__, where(ep)])
        def walk(routes):
            for r in routes:
                contexts = getattr(r, "effective_route_contexts", None)  # FastAPI 0.16x keeps included routers nested
                if contexts is not None:
                    for c in contexts():
                        add(c.path, c.methods, c.endpoint, getattr(c, "include_in_schema", True))
                elif getattr(r, "routes", None):
                    walk(r.routes)
                else:
                    add(getattr(r, "path", ""), getattr(r, "methods", None), getattr(r, "endpoint", None), getattr(r, "include_in_schema", True))
        if hasattr(app, "url_map") and hasattr(app, "view_functions"):  # Flask
            for rule in app.url_map.iter_rules():
                view = app.view_functions.get(rule.endpoint)
                if view is None or rule.endpoint == "static":
                    continue
                for m in sorted(rule.methods or []):
                    if m not in ("HEAD", "OPTIONS"):
                        out.append([m, rule.rule, view.__qualname__, where(view)])
        else:
            walk(app.routes)
    elif DJANGO:
        if django_error:
            raise SystemExit(1)
        from django.urls import get_resolver
        def walk(patterns, prefix):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    walk(p.url_patterns, prefix + str(p.pattern))
                else:
                    cb = p.callback
                    out.append(["ANY", "/" + prefix + str(p.pattern), getattr(cb, "__qualname__", str(cb)), where(cb)])
        walk(get_resolver().url_patterns, "")
print("LGROUTES" + json.dumps(out))
lg_worker.drop_scratch()
"""


def introspect(cfg, timeout: int = 60, root: str | None = None) -> list[list] | None:
    """[[METHOD, path, handler qualname, handler file (real path)]] from the framework, or None. `root` is the tree to
    ask (Sprint 7.2b: ci.run asks the baseline tree for baseline_routes); it defaults to the project root."""
    if not (cfg.raw.get("app") or cfg.raw.get("django_settings")):
        return None
    from runner import os_sandbox
    bd = cfg.behavioral_diff
    py = cfgmod.target_python(cfg)
    root = str(root or cfg.root)
    roots = [str(Path(root) / r) for r in (cfg.raw.get("source_roots") or [])] + [root]
    try:
        with tempfile.TemporaryDirectory(prefix="lgroutes") as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps({"root": root, "sys_path": roots, "protect": [root], "cwd": root,
                                       "scratch": str(Path(tmp) / "scratch"), "django_settings": cfg.raw.get("django_settings"),
                                       "deterministic": False, "io": str(bd.get("io", "stub"))}))
            script = (f"RUNNER_DIR={str(RUNNER)!r}\nREQUEST={str(req)!r}\nAPP={cfg.raw.get('app')!r}\n"
                      f"DJANGO={cfg.raw.get('django_settings')!r}\n" + INTROSPECT)
            cmd = os_sandbox.wrap([py, "-B", "-c", script], py, writable=[tempfile.gettempdir(), tmp], readonly=[root],
                                  mode=str(bd.get("os_sandbox", "auto")))
            res = os_sandbox.run(cmd, timeout, cwd=root, env=clean_env(cfg), capture_output=True, text=True)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    for line in res.stdout.splitlines():
        if line.startswith("LGROUTES"):
            try:
                return json.loads(line[len("LGROUTES"):])
            except ValueError:
                return None
    return None


def from_decorators(conn) -> list[tuple]:
    rows = []
    for s in conn.execute("SELECT qualname, file, decorators FROM symbols WHERE decorators != '[]'"):
        for dec in json.loads(s["decorators"]):
            for _, method, path in route_decorators.parse(dec):
                rows.append((method, path, s["qualname"], s["file"], "decorator"))
    return rows


def _framework_rows(live: list[list], root: str) -> list[tuple]:
    """Introspected rows with handler files relative to the tree they came from."""
    prefix = str(Path(root).resolve()) + "/"
    return [(m, p, q, f[len(prefix):] if f.startswith(prefix) else f, "framework") for m, p, q, f in live]


def rebuild(conn, cfg) -> dict:
    conn.execute("DELETE FROM routes")
    live = introspect(cfg)
    if live is not None:
        rows = _framework_rows(live, cfg.root)
        source = "framework"
    else:
        rows = from_decorators(conn)
        source = "decorators"
    conn.executemany("INSERT INTO routes(method, path, handler_qualname, handler_file, source) VALUES (?,?,?,?,?)", rows)
    return {"routes": len(rows), "source": source}


# ---- Sprint 7.2b: the routes of the code before the edit, for deleted and renamed handlers ----
# The routes table describes different trees in different places: a session builds it at SessionStart, when the
# working tree equals the baseline, while ci.run (and tools/validate.py) refresh it on the head tree. baseline_routes
# always describes the baseline, so a handler the edit deleted or renamed still has its old METHOD and path.
INSERT_BASELINE = ("INSERT INTO baseline_routes(method, path, handler_qualname, handler_file, source) VALUES (?,?,?,?,?)")


def snapshot_baseline(conn) -> int:
    """SessionStart, right after the index refresh: the working tree equals the baseline, so the routes table is the
    baseline's. Copied as it is (framework or decorator rows)."""
    conn.execute("DELETE FROM baseline_routes")
    conn.execute("INSERT INTO baseline_routes(method, path, handler_qualname, handler_file, source) "
                 "SELECT method, path, handler_qualname, handler_file, source FROM routes")
    return conn.execute("SELECT COUNT(*) FROM baseline_routes").fetchone()[0]


def rebuild_baseline(conn, cfg, base_dir: str) -> dict:
    """ci.run: ask the framework in the baseline tree. When introspection gives nothing the table stays empty, and the
    lookup for an old handler falls back to the routes table, then to the old function's own decorators."""
    conn.execute("DELETE FROM baseline_routes")
    live = introspect(cfg, root=base_dir)
    rows = _framework_rows(live, base_dir) if live is not None else []
    conn.executemany(INSERT_BASELINE, rows)
    return {"routes": len(rows), "source": "framework" if live is not None else "none"}


def old_handler_routes(conn, file: str, qualname: str, decorators: list[str]) -> list[tuple[str, str, str]]:
    """[(METHOD, path, source)] a handler served in the code before the edit, looked up in this order:
    1. baseline_routes; 2. routes (a session's index is the baseline's); 3. the old function's own route decorators, read
    by the shared parser (source "decorator": no router or blueprint prefix is known, so the path may lack one).
    ANY rows (Django) give no requests and are left out.
    In ci.run the routes table describes the head tree, so step 2 matters only when baseline_routes is empty there (the
    baseline could not be introspected). A handler that is truly gone has no head row and falls through to step 3; a
    head row under the same file and qualname belongs to a function that still exists, which symbol_diff would not
    report as deleted or renamed in the first place."""
    for table in ("baseline_routes", "routes"):
        if conn is None:
            break
        try:
            rows = conn.execute(f"SELECT method, path, source FROM {table} WHERE handler_file = ? AND handler_qualname = ?",
                                (file, qualname)).fetchall()
        except Exception:  # noqa: BLE001 - an index without the table falls through to the next source
            rows = []
        rows = sorted({(m, p, s or "framework") for m, p, s in rows if m != "ANY"})
        if rows:
            return rows
    out = []
    for dec in decorators or []:
        for _, method, path in route_decorators.parse(dec):
            out.append((method, path, "decorator"))
    return sorted(set(out))
