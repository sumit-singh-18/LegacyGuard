"""Client for the warm worker servers (runner/lg_server.py), one per tree, roadmap A3.

The hooks are short-lived processes, so the servers outlive them: the first check of a session starts a server per
tree, and later checks reuse it. Everything here fails soft: any problem returns None and the caller runs the cold
worker instead, so a broken server can make checks slower but never wrong.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from runner import os_sandbox

HERE = Path(__file__).resolve().parent
SERVER = HERE / "lg_server.py"
START_TIMEOUT_S = 20
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.Lock())


def _code_version() -> str:
    h = hashlib.sha256()
    for name in ("lg_server.py", "lg_worker.py", "lg_codec.py"):
        h.update((HERE / name).read_bytes())
    return h.hexdigest()[:12]


def _generation(cfg) -> str:
    """Changes when SessionStart rebuilds the baseline, so a new session never reuses an old baseline's server."""
    try:
        st = (Path(cfg.state_dir) / "baseline").stat()
        return f"{st.st_ino}-{st.st_mtime_ns}"
    except OSError:
        return "none"


def socket_path(cfg, py: str, tree_root: str, mode: str) -> str:
    key = "|".join([os.path.realpath(cfg.root), os.path.realpath(tree_root), mode, py, _generation(cfg), _code_version()])
    # Unix socket paths are short (104 bytes on macOS), so the name is a hash in the temp folder.
    return os.path.join(tempfile.gettempdir(), f"lg-{hashlib.sha256(key.encode()).hexdigest()[:20]}.sock")


def _send(path: str, msg: dict, timeout: float) -> dict | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps(msg) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode())
    except (OSError, ValueError):
        return None


def third_party_imports(root: str, files: list[str], source_roots: list[str]) -> list[str]:
    """Top-level names the files import that are not the project's own modules: what is safe to keep warm for the
    working tree, since it does not change when Claude edits."""
    names: set[str] = set()
    for rel in files:
        try:
            tree = ast.parse((Path(root) / rel).read_text(errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module.split(".")[0])
    bases = [Path(root)] + [Path(root) / r for r in source_roots] + [Path(root) / Path(f).parent for f in files]
    own = {n for n in names if any((b / n).exists() or (b / f"{n}.py").exists() for b in bases)}
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    return sorted(n for n in names - own if n and n not in stdlib and n != "__future__")


def ensure(cfg, py: str, tree_root: str, mode: str, init: dict, env: dict) -> str | None:
    """The socket of a running server for this tree, starting one if needed. None if it cannot be started."""
    path = socket_path(cfg, py, tree_root, mode)
    with _lock(path):  # OLD-A and OLD-B ask for the same server at the same moment: start it once
        return _ensure_locked(cfg, py, tree_root, mode, init, env, path)


def _ensure_locked(cfg, py, tree_root, mode, init, env, path) -> str | None:
    if os.path.exists(path) and (_send(path, {"cmd": "ping"}, 2) or {}).get("ok"):
        return path
    with tempfile.NamedTemporaryFile("w", prefix="lginit", suffix=".json", delete=False) as fh:
        json.dump(init, fh)
        init_path = fh.name
    cmd = os_sandbox.wrap([py, "-B", str(SERVER), path, init_path, mode], py, writable=[tempfile.gettempdir()],
                          readonly=[str(cfg.root), tree_root], mode=str(cfg.behavioral_diff.get("os_sandbox", "auto")), long_lived=True)
    try:
        subprocess.Popen(cmd, cwd=tree_root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return None
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if os.path.exists(path) and (_send(path, {"cmd": "ping"}, 2) or {}).get("ok"):
            return path
        time.sleep(0.05)
    return None


def run(cfg, py: str, tree_root: str, mode: str, request: dict, result_path: str, timeout: float, env: dict) -> dict | None:
    """Results for one request through the warm server, or None to fall back to the cold worker."""
    if not hasattr(os, "fork") or not bool(cfg.behavioral_diff.get("warm", True)):
        return None
    path = ensure(cfg, py, tree_root, mode, request, env)
    if path is None:
        return None
    reply = _send(path, {"cmd": "run", "request": request, "result": result_path, "timeout": timeout}, timeout + 10)
    if not reply:
        return None
    if not reply.get("ok"):
        return {"__failed__": reply.get("error") or "the warm worker failed"}
    try:
        return json.loads(Path(result_path).read_text())
    except (OSError, ValueError):
        return None


def stop_all(cfg, py: str, trees: list[tuple[str, str]]) -> None:
    """Stop the servers for these (tree, mode) pairs, e.g. when a session starts with a new baseline."""
    for tree, mode in trees:
        path = socket_path(cfg, py, tree, mode)
        if os.path.exists(path):
            _send(path, {"cmd": "stop"}, 2)
