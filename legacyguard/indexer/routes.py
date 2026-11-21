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


def introspect(cfg, timeout: int = 60) -> list[list] | None:
    if not (cfg.raw.get("app") or cfg.raw.get("django_settings")):
        return None
    from runner import os_sandbox
    bd = cfg.behavioral_diff
    py = cfgmod.target_python(cfg)
    roots = [str(Path(cfg.root) / r) for r in (cfg.raw.get("source_roots") or [])] + [cfg.root]
    try:
        with tempfile.TemporaryDirectory(prefix="lgroutes") as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps({"root": cfg.root, "sys_path": roots, "protect": [str(cfg.root)], "cwd": cfg.root,
                                       "scratch": str(Path(tmp) / "scratch"), "django_settings": cfg.raw.get("django_settings"),
                                       "deterministic": False, "io": str(bd.get("io", "stub"))}))
            script = (f"RUNNER_DIR={str(RUNNER)!r}\nREQUEST={str(req)!r}\nAPP={cfg.raw.get('app')!r}\n"
                      f"DJANGO={cfg.raw.get('django_settings')!r}\n" + INTROSPECT)
            cmd = os_sandbox.wrap([py, "-B", "-c", script], py, writable=[tempfile.gettempdir(), tmp], readonly=[str(cfg.root)],
                                  mode=str(bd.get("os_sandbox", "auto")))
            res = subprocess.run(cmd, cwd=cfg.root, env=clean_env(cfg), capture_output=True, text=True, timeout=timeout)
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


def rebuild(conn, cfg) -> dict:
    conn.execute("DELETE FROM routes")
    live = introspect(cfg)
    if live is not None:
        root = str(Path(cfg.root).resolve()) + "/"
        rows = [(m, p, q, f[len(root):] if f.startswith(root) else f, "framework") for m, p, q, f in live]
        source = "framework"
    else:
        rows = from_decorators(conn)
        source = "decorators"
    conn.executemany("INSERT INTO routes(method, path, handler_qualname, handler_file, source) VALUES (?,?,?,?,?)", rows)
    return {"routes": len(rows), "source": source}
