"""Shared fixtures. The guard's packages live in legacyguard/, which is what the hooks put on sys.path too."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "legacyguard"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from guardlib import config as cfgmod  # noqa: E402
from runner import lg_codec  # noqa: E402

WORKER = PLUGIN / "runner" / "lg_worker.py"
# The interpreter that runs target code. The worker is standard-library only, so it can differ from the one running
# pytest: LG_TEST_TARGET_PY=/path/to/python3.9 checks the worker on an older or newer Python.
TARGET_PY = os.environ.get("LG_TEST_TARGET_PY") or sys.executable


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"))
    return root


@pytest.fixture
def make_cfg(tmp_path):
    """A Config for a project at root, running target code in this interpreter (the worker is standard-library only)."""
    def make(root: Path, **overrides) -> cfgmod.Config:
        raw = copy.deepcopy(cfgmod.DEFAULTS)
        raw["python"] = TARGET_PY
        for key, value in overrides.items():
            if isinstance(raw.get(key), dict) and isinstance(value, dict):
                raw[key].update(value)
            else:
                raw[key] = value
        (root / ".legacyguard").mkdir(parents=True, exist_ok=True)
        return cfgmod.Config(root=str(root), raw=raw)
    return make


def call(*args, **kwargs) -> dict:
    """One input item in the worker's request format."""
    out = [{"name": f"a{i}", "kind": "pos", "value": lg_codec.encode_input(v)} for i, v in enumerate(args)]
    out += [{"name": k, "kind": "kw", "value": lg_codec.encode_input(v)} for k, v in kwargs.items()]
    return {"args": out, "source": "generated"}


@pytest.fixture
def run_worker(tmp_path):
    """Run lg_worker.py in a subprocess on a project folder and return its parsed result."""
    def run(root: Path, functions: list[dict], timeout: float = 60, **request_extra) -> dict:
        req, res = tmp_path / "req.json", tmp_path / "res.json"
        req.write_text(json.dumps({"root": str(root), "sys_path": [str(root)], "protect": [str(root)],
                                   "functions": functions, **request_extra}))
        done = subprocess.run([TARGET_PY, "-B", str(WORKER), str(req), str(res)], cwd=root,
                              capture_output=True, text=True, timeout=timeout)
        assert res.exists(), f"worker wrote no result: {done.stderr[-2000:]}"
        return json.loads(res.read_text())
    return run

