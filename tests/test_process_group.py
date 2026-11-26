"""os_sandbox.run: a timeout ends every process the command started, inside the OS sandbox or not.

Under bubblewrap, subprocess.run(timeout=) killed only bwrap; the Python inside was reparented to init and kept
spinning. On macOS sandbox-exec execs in place, so the direct child is the Python itself and the old code may also have
passed the child-only part there; the grandchild part covers what subprocess.run never killed on any platform."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from runner import os_sandbox

SPIN = r"""
import os, subprocess, sys, time
d = sys.argv[1]
with open(os.path.join(d, "child.pid"), "w") as fh:
    fh.write(str(os.getpid()))
try:
    subprocess.Popen([sys.executable, "-c",
                      "import os, sys\nopen(os.path.join(sys.argv[1], 'grandchild.pid'), 'w').write(str(os.getpid()))\nwhile True: pass\n",
                      d])
except Exception:
    pass
while True:
    pass
"""


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _pids(d: str) -> dict:
    out = {}
    for name in ("child", "grandchild"):
        p = os.path.join(d, f"{name}.pid")
        if os.path.exists(p) and open(p).read().strip():
            out[name] = int(open(p).read())
    return out


def _wait_gone(pids, limit=1.0) -> list:
    end = time.time() + limit
    while time.time() < end:
        left = [p for p in pids if alive(p)]
        if not left:
            return []
        time.sleep(0.05)
    return [p for p in pids if alive(p)]


def test_timeout_kills_the_whole_group_inside_the_sandbox(tmp_path):
    d = tempfile.mkdtemp(prefix="lgpg")
    try:
        cmd = os_sandbox.wrap([sys.executable, "-c", SPIN, d], sys.executable, writable=[tempfile.gettempdir(), d], readonly=[str(tmp_path)])
        with pytest.raises(subprocess.TimeoutExpired):
            os_sandbox.run(cmd, 2, capture_output=True, text=True)
        pids = _pids(d)
        assert "child" in pids  # the child always writes its pid; the grandchild only where the sandbox allows spawning
        left = _wait_gone(list(pids.values()))
        for p in left:  # never leave a spinner behind, even when the assertion fails
            try:
                os.kill(p, 9)
            except OSError:
                pass
        assert left == [], f"still running after the timeout: {pids}"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_keyboard_interrupt_kills_the_group(tmp_path, monkeypatch):
    d = tempfile.mkdtemp(prefix="lgpg")
    try:
        real = subprocess.Popen.communicate
        state = {"first": True}

        def communicate(self, *a, **k):
            if state["first"]:
                state["first"] = False
                time.sleep(1)  # let the child write its pid and start the grandchild
                raise KeyboardInterrupt
            return real(self, *a, **k)
        monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
        with pytest.raises(KeyboardInterrupt):
            os_sandbox.run([sys.executable, "-c", SPIN, d], 30, capture_output=True)
        monkeypatch.undo()
        pids = _pids(d)
        assert "child" in pids and _wait_gone(list(pids.values())) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_run_has_subprocess_run_semantics():
    res = os_sandbox.run([sys.executable, "-c", "import sys; d = sys.stdin.read(); print(d.upper()); sys.exit(3)"], 30,
                         input="abc", capture_output=True, text=True)
    assert isinstance(res, subprocess.CompletedProcess) and res.returncode == 3 and res.stdout.strip() == "ABC"
    res = os_sandbox.run([sys.executable, "-c", "import os; print(os.getcwd(), os.environ['LGX'])"], 30,
                         cwd=tempfile.gettempdir(), env={**os.environ, "LGX": "y"}, capture_output=True, text=True)
    assert res.stdout.split()[-1] == "y" and os.path.realpath(res.stdout.split()[0]) == os.path.realpath(tempfile.gettempdir())
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        os_sandbox.run([sys.executable, "-c", "import time; print('hi', flush=True); time.sleep(30)"], 1, capture_output=True, text=True)
    assert "hi" in (exc.value.output or "")
