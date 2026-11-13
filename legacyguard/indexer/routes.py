"""Routes table (FR-06, D-030): ask the framework itself; fall back to reading decorators."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from guardlib import config as cfgmod
from indexer.coverage_map import clean_env

RUNNER = Path(__file__).resolve().parent.parent / "runner"
ROUTE_DECORATOR = re.compile(r"""^[\w.]*\.(get|post|put|patch|delete)\(\s*["'](/[^"']*|)["']""", re.I)

INTROSPECT = r"""
import inspect, json, os, sys
sys.path.insert(0, RUNNER_DIR)
import lg_worker
lg_worker.install_blocks()
for p in ROOTS: sys.path.insert(0, p)
out = []
def where(fn):
    try: return os.path.realpath(inspect.getsourcefile(inspect.unwrap(fn)))
    except Exception: return ""
if APP:
    import importlib
    mod, attr = APP.split(":")
    app = getattr(importlib.import_module(mod), attr)
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
    import django
    os.environ["DJANGO_SETTINGS_MODULE"] = DJANGO
    django.setup()
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
"""


def introspect(cfg, timeout: int = 60) -> list[list] | None:
    if not (cfg.raw.get("app") or cfg.raw.get("django_settings")):
        return None
    roots = [str(Path(cfg.root) / r) for r in (cfg.raw.get("source_roots") or [])] + [cfg.root]
    script = (f"RUNNER_DIR={str(RUNNER)!r}\nROOTS={roots!r}\nAPP={cfg.raw.get('app')!r}\nDJANGO={cfg.raw.get('django_settings')!r}\n" + INTROSPECT)
    try:
        res = subprocess.run([cfgmod.target_python(cfg), "-c", script], cwd=cfg.root, env=clean_env(cfg), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in res.stdout.splitlines():
        if line.startswith("LGROUTES"):
            return json.loads(line[len("LGROUTES"):])
    return None


def from_decorators(conn) -> list[tuple]:
    rows = []
    for s in conn.execute("SELECT qualname, file, decorators FROM symbols WHERE decorators != '[]'"):
        for dec in json.loads(s["decorators"]):
            m = ROUTE_DECORATOR.match(dec)
            if m:
                rows.append((m.group(1).upper(), m.group(2), s["qualname"], s["file"], "decorator"))
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
