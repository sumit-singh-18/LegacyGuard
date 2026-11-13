"""Run the project's own tests that reach the changed functions, on the baseline and on the edited code (roadmap C4).

The coverage map from /legacyguard:index says which tests run which functions (Ekstazi-style selection). After an
edit only those tests run. A test that passed on the session baseline and fails now is a regression of something a
developer already wrote down as intended, which is the strongest signal the guard has. A test that already failed
on the baseline says nothing about this session and is left out.
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from guardlib import config as cfgmod
from indexer.coverage_map import clean_env
from runner import os_sandbox

PARAMS = re.compile(r"\[.*\]$")


def settings(cfg) -> dict:
    return {"enabled": True, "max_tests": 25, "timeout_s": 60, **(cfg.raw.get("affected_tests") or {})}


def to_nodeid(root: str, dotted: str) -> str | None:
    """'tests.test_bank.TestX.test_y' -> 'tests/test_bank.py::TestX::test_y', by finding the longest module prefix
    that is a file. None when no prefix is a file."""
    parts = [p for p in dotted.split(".") if p]
    for i in range(len(parts) - 1, 0, -1):
        rel = "/".join(parts[:i]) + ".py"
        if (Path(root) / rel).is_file():
            return rel + "::" + "::".join(parts[i:])
    return None


def select(conn, root: str, targets: list[tuple[str, str]], limit: int) -> list[str]:
    """Test node ids that reach any of the (file, qualname) targets, in a stable order, at most limit."""
    if conn is None or not targets:
        return []
    ids: set[str] = set()
    for file, qualname in targets:
        try:
            rows = conn.execute("SELECT DISTINCT test_id FROM test_cover WHERE file = ? AND qualname = ?", (file, qualname)).fetchall()
        except Exception:  # noqa: BLE001 - an old index without the table: nothing to select
            return []
        ids.update(r[0] for r in rows if r[0])
    nodeids = sorted({n for n in (to_nodeid(root, i) for i in ids) if n})
    return nodeids[:limit]


def _parse_junit(path: Path, root: str) -> dict[str, dict]:
    """{nodeid: {outcome, message}} from a pytest junit report. Parametrized cases fold into their test: it fails if
    any case fails."""
    out: dict[str, dict] = {}
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return out
    for case in tree.iter("testcase"):
        name = PARAMS.sub("", case.get("name", ""))
        nodeid = to_nodeid(root, f"{case.get('classname', '')}.{name}")
        if nodeid is None:
            continue
        outcome, message = "passed", ""
        for tag in ("failure", "error"):
            el = case.find(tag)
            if el is not None:
                outcome = "failed" if tag == "failure" else "error"
                message = (el.get("message") or el.text or "").strip()
        if case.find("skipped") is not None and outcome == "passed":
            outcome = "skipped"
        prev = out.get(nodeid)
        if prev is None or (prev["outcome"] in ("passed", "skipped") and outcome in ("failed", "error")):
            out[nodeid] = {"outcome": outcome, "message": message[:600]}
    return out


def run(cfg, tree_root: str, nodeids: list[str], timeout: float) -> dict[str, dict]:
    """Run the given tests in one tree (baseline or working tree), sandboxed like the behavioral diff."""
    if not nodeids:
        return {}
    py = cfgmod.target_python(cfg)
    with tempfile.TemporaryDirectory(prefix="lgtests") as tmp:
        report = Path(tmp) / "junit.xml"
        env = clean_env(cfg)
        extra = [str(Path(tree_root) / r) for r in cfg.raw.get("source_roots") or []]
        env["PYTHONPATH"] = os.pathsep.join([tree_root, *extra])
        cmd = [py, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", f"--junitxml={report}",
               "-o", "junit_family=xunit2", *nodeids]
        cmd = os_sandbox.wrap(cmd, py, writable=[tempfile.gettempdir(), tmp], readonly=[str(cfg.root), tree_root],
                              mode=str(cfg.behavioral_diff.get("os_sandbox", "auto")))
        try:
            subprocess.run(cmd, cwd=tree_root, env=env, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return {}
        return _parse_junit(report, tree_root)


def compare(cfg, baseline_root: str, work_root: str, nodeids: list[str], old_cache: dict) -> tuple[list[dict], dict, dict]:
    """(regressions, old results, new results). Baseline results are cached for the session in old_cache, since the
    baseline does not change; only tests not yet in the cache run there."""
    s = settings(cfg)
    missing = [n for n in nodeids if n not in old_cache]
    with cf.ThreadPoolExecutor(2) as pool:
        f_old = pool.submit(run, cfg, baseline_root, missing, s["timeout_s"]) if missing else None
        f_new = pool.submit(run, cfg, work_root, nodeids, s["timeout_s"])
        fresh_old = f_old.result() if f_old else {}
        new = f_new.result()
    for n in missing:
        old_cache[n] = fresh_old.get(n, {"outcome": "not_run", "message": ""})
    regressions = []
    for n in nodeids:
        before, after = old_cache.get(n, {}), new.get(n)
        if before.get("outcome") == "passed" and after and after["outcome"] in ("failed", "error"):
            regressions.append({"nodeid": n, "message": after["message"]})
    return regressions, old_cache, new


def format_wake(regression: dict, finding_id: str, reaches: list[str]) -> str:
    lines = [f"LegacyGuard test regression [{finding_id}]: {regression['nodeid']} passed on the session baseline and fails after this edit."]
    msg = "\n".join(regression["message"].splitlines()[:4])
    if msg:
        lines.append(msg)
    if reaches:
        lines.append(f"The test runs {', '.join(reaches[:4])}, which changed this session.")
    lines.append("This test is part of the project's own suite. If the request did not intend this, revising the code clears it; "
                 f"if it did, say so, and the developer can accept it with /legacyguard:waive {finding_id} \"<reason>\".")
    return "\n".join(lines)
