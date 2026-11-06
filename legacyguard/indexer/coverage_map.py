"""Test coverage map (FR-05) and argument harvest. Runs the suite once with coverage.py dynamic contexts."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import coverage

from guardlib import config as cfgmod
from guardlib import symbols

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def clean_env(cfg) -> dict:
    env = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in SECRET_WORDS) and not k.startswith("CLAUDE")}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"})
    env.update({str(k): str(v) for k, v in (cfg.behavioral_diff.get("env") or {}).items()})
    return env


def is_test_path(rel: str) -> bool:
    from guardlib.checks import is_test_path as check
    return check(rel) or rel.endswith("/conftest.py") or rel == "conftest.py"


def run_suite(cfg, timeout: int = 600, extra_args: list[str] | None = None) -> tuple[int | None, str, Path]:
    """Run the tests under coverage and the harvest plugin. Returns (exit code, output tail, coverage data path)."""
    py = cfgmod.target_python(cfg)
    state = cfg.state_dir
    data_file = state / "coverage.db"
    data_file.unlink(missing_ok=True)
    harvest_out = state / "harvest.json"
    with tempfile.TemporaryDirectory(prefix="lgshim") as shim:
        shim_path = Path(shim)
        os.symlink(Path(coverage.__file__).parent, shim_path / "coverage")
        for name in ("lg_codec.py", "legacyguard_harvest.py"):
            shutil.copy(PLUGIN_ROOT / "runner" / name, shim_path / name)
        scope = cfg.scope.rstrip("/") or "."
        rc = shim_path / "coveragerc"
        rc.write_text(f"[run]\ndynamic_context = test_function\nsource = {scope}\nbranch = false\n")
        env = clean_env(cfg)
        env["PYTHONPATH"] = str(shim_path) + os.pathsep + os.pathsep.join(str(Path(cfg.root) / r) for r in cfg.raw.get("source_roots") or [])
        env.update({"LG_HARVEST_OUT": str(harvest_out), "LG_HARVEST_ROOT": cfg.root})
        cmd = [py, "-m", "coverage", "run", f"--rcfile={rc}", f"--data-file={data_file}", "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "-p", "legacyguard_harvest", "--no-header", *(extra_args or [])]
        try:
            res = subprocess.run(cmd, cwd=cfg.root, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, f"test suite timed out after {timeout}s", data_file
        except OSError as exc:
            return None, f"could not start {py}: {exc}", data_file
    tail = "\n".join((res.stdout + res.stderr).strip().splitlines()[-3:])
    return res.returncode, tail, data_file


def _innermost(defs: list, line: int):
    best = None
    for d in defs:
        if d.kind != "class" and d.line_start < line <= d.line_end or (d.kind != "class" and d.line_start == d.line_end == line):
            if best is None or d.line_start > best.line_start:
                best = d
    return best


def load_map(conn, cfg, data_file: Path) -> int:
    """Fill test_cover from coverage contexts. Returns the number of rows."""
    conn.execute("DELETE FROM test_cover")
    if not data_file.exists():
        return 0
    data = coverage.CoverageData(basename=str(data_file))
    data.read()
    rows = set()
    root = os.path.realpath(cfg.root) + os.sep
    for measured in data.measured_files():
        real = os.path.realpath(measured)
        if not real.startswith(root):
            continue
        rel = real[len(root):].replace(os.sep, "/")
        if is_test_path(rel):
            continue
        defs, _ = symbols.extract(Path(real).read_bytes())
        for line, contexts in data.contexts_by_lineno(measured).items():
            ctxs = [c for c in contexts if c]
            if not ctxs:
                continue
            sym = _innermost(defs, line)
            if sym is None:
                continue
            rows.update((c.split("|")[0], rel, sym.qualname) for c in ctxs)
    conn.executemany("INSERT INTO test_cover(test_id, file, qualname) VALUES (?,?,?)", sorted(rows))
    return len(rows)


def load_harvest(cfg) -> dict:
    path = cfg.state_dir / "harvest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def build(conn, cfg, timeout: int = 600) -> dict:
    from indexer import db as idb
    code, tail, data_file = run_suite(cfg, timeout)
    rows = load_map(conn, cfg, data_file)
    status = "ok" if code == 0 else ("no-run" if code is None else f"tests exited {code}")
    idb.set_meta(conn, "coverage_status", status)
    idb.set_meta(conn, "coverage_tail", tail)
    return {"status": status, "rows": rows, "tail": tail}
