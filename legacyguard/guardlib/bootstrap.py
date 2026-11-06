"""Create and find the guard's own virtual environment. Standard library only (FR-03)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

REQUIRED_IMPORTS = ("yaml", "tree_sitter", "tree_sitter_python")


class BootstrapError(Exception):
    pass


def data_dir() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    return Path(env) if env else Path.home() / ".cache" / "legacyguard"


def venv_python(base: Path | None = None) -> Path:
    v = (base or data_dir()) / "venv"
    return v / "Scripts" / "python.exe" if os.name == "nt" else v / "bin" / "python"


def _req_hash(plugin_root: str | Path) -> str:
    return hashlib.sha256((Path(plugin_root) / "requirements.txt").read_bytes()).hexdigest()


def is_ready(plugin_root: str | Path, base: Path | None = None) -> bool:
    base = base or data_dir()
    marker = base / "venv" / ".legacyguard-ready"
    return marker.exists() and marker.read_text().strip() == _req_hash(plugin_root) and venv_python(base).exists()


def manual_command(plugin_root: str | Path, base: Path | None = None) -> str:
    base = base or data_dir()
    return f'python3 -m venv "{base / "venv"}" && "{venv_python(base)}" -m pip install -r "{Path(plugin_root) / "requirements.txt"}"'


MIN_PYTHON = (3, 10)
PYTHON_NAMES = ("python3.12", "python3.13", "python3.11", "python3.10", "python3.14", "python3")
PYTHON_MINORS = ("12", "13", "11", "10", "14")


def _python_version(path: str) -> tuple | None:
    try:
        res = subprocess.run([path, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=10)
        return tuple(int(x) for x in res.stdout.strip().split(".")) if res.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def find_python() -> str | None:
    """A Python 3.10+ already on this machine, for when the python3 command is older (a Mac ships 3.9).

    Claude Code may start hooks with a shorter PATH than the developer's shell, so the usual install folders are tried too.
    """
    home = Path.home()
    folders = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    folders += ["/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin"), str(home / ".pyenv" / "shims")]
    folders += [f"/opt/homebrew/opt/python@3.{m}/libexec/bin" for m in PYTHON_MINORS]
    folders += [f"/Library/Frameworks/Python.framework/Versions/3.{m}/bin" for m in PYTHON_MINORS]
    seen = set()
    for name in PYTHON_NAMES:
        for folder in folders:
            cand = os.path.join(folder, name)
            if cand in seen or not os.path.isfile(cand):
                continue
            seen.add(cand)
            version = _python_version(cand)
            if version and version >= MIN_PYTHON:
                return cand
    return None


def ensure_venv(plugin_root: str | Path, base: Path | None = None, run=subprocess.run, version=None, finder=find_python) -> bool:
    """Create the venv once. Returns True if it installed, False if it was already ready."""
    base = base or data_dir()
    if is_ready(plugin_root, base):
        return False
    version = tuple(version or sys.version_info[:3])
    creator = sys.executable
    if version < MIN_PYTHON:
        creator = finder()
        if not creator:
            raise BootstrapError(f"LegacyGuard needs Python 3.10 or newer and found only {'.'.join(map(str, version))} on this machine. "
                                 "Install one (from python.org, or with: brew install python@3.12), then start Claude Code again")
    base.mkdir(parents=True, exist_ok=True)
    if is_ready(plugin_root, base):
        return False
    with open(base / ".bootstrap.lock", "w") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        if is_ready(plugin_root, base):  # another process finished while we waited
            return False
        venv = base / "venv"
        steps = [[creator, "-m", "venv", "--clear", str(venv)],
                 [str(venv_python(base)), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                  "-r", str(Path(plugin_root) / "requirements.txt")]]
        for cmd in steps:
            try:
                res = run(cmd, capture_output=True, text=True, timeout=240)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BootstrapError(str(exc)) from exc
            if res.returncode:
                tail = (res.stderr or res.stdout or "").strip().splitlines()[-1:] or ["unknown error"]
                raise BootstrapError(tail[0])
        (venv / ".legacyguard-ready").write_text(_req_hash(plugin_root))
    return True


def deps_available() -> bool:
    import importlib.util
    return all(importlib.util.find_spec(m) for m in REQUIRED_IMPORTS)


def reexec_in_venv(plugin_root: str | Path, ensure: bool = False) -> None:
    """Replace this process with the venv's Python if the current one lacks the guard's dependencies."""
    if deps_available():
        return
    if ensure:
        ensure_venv(plugin_root)
    py = venv_python()
    # Compare prefixes: a venv's python is a symlink to the system interpreter, so resolved paths match.
    if py.exists() and Path(sys.prefix).resolve() != (data_dir() / "venv").resolve():
        os.execv(str(py), [str(py), *sys.argv])
    raise BootstrapError("guard dependencies are not installed")
