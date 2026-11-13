"""The OS layer: no network, no writes into the project, temp folder still writable. Skipped where there is no OS sandbox."""
import subprocess
import sys
import tempfile

import pytest

from runner import os_sandbox

pytestmark = pytest.mark.skipif(not os_sandbox.available(sys.executable), reason="no OS sandbox on this machine")


def run(code, root):
    cmd = os_sandbox.wrap([sys.executable, "-c", code], sys.executable, writable=[tempfile.gettempdir()], readonly=[str(root)])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_project_is_read_only_even_inside_temp(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("keep")
    r = run(f"import os; os.remove({str(root / 'a.txt')!r})", root)
    assert r.returncode != 0 and (root / "a.txt").read_text() == "keep"
    r = run(f"open({str(root / 'b.txt')!r}, 'w').write('x')", root)
    assert r.returncode != 0 and not (root / "b.txt").exists()


def test_temp_folder_writable(tmp_path):
    scratch = tempfile.mkdtemp(prefix="lgsb")
    r = run(f"open({scratch + '/ok.txt'!r}, 'w').write('x')", tmp_path / "elsewhere")
    assert r.returncode == 0, r.stderr


def test_network_refused(tmp_path):
    r = run("import socket; s = socket.socket(); s.settimeout(3); s.connect(('1.1.1.1', 80))", tmp_path)
    assert r.returncode != 0


def test_off_mode_does_not_wrap():
    cmd = [sys.executable, "-c", "pass"]
    assert os_sandbox.wrap(cmd, sys.executable, [], [], mode="off") == cmd
    assert os_sandbox.describe() == "switched off in config"
