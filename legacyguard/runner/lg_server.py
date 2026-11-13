"""Warm worker for one tree (roadmap A3). Runs in the target project's interpreter. Standard library and lg_worker only.

Usage: python -B lg_server.py <socket path> <init request.json> <baseline|work>

A cold check starts a fresh interpreter and imports the project's libraries (Django, pandas, ...) for every edit.
This server does that once, then fork()s a child per check: the child inherits everything already imported and
dies after the check, so nothing one check does leaks into the next.

- baseline: the session baseline never changes, so the server also imports the project's own modules (and runs
  Django setup) once, and every later check of the baseline starts from them.
- work: the working tree changes with every edit, so the server imports only third-party libraries. Each child
  imports the project fresh, and always sees the current code.

Requests are one JSON line on a Unix socket: {"cmd": "run", "request": {...}, "result": path, "timeout": s} or
{"cmd": "ping"} / {"cmd": "stop"}. The reply is one JSON line. The server exits after IDLE_S without requests.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lg_worker as W  # noqa: E402

IDLE_S = 20 * 60
STATE = {"warm": set(), "runs": 0, "mode": "work", "root_real": "", "setup_error": None}


def _warm(names: list[str]) -> None:
    """Import modules in the server so children inherit them. Failures are ignored: the child will import or fail
    by itself. Imports run with the blocks on, like the target code."""
    for name in names:
        if name in STATE["warm"]:
            continue
        STATE["warm"].add(name)
        W.GUARD_WRITES["allowed"] = False
        try:
            __import__(name)
        except BaseException:  # noqa: BLE001
            pass
        finally:
            W.GUARD_WRITES["allowed"] = True


def _run(msg: dict) -> dict:
    request = msg["request"]
    W.add_paths(request)
    warm = list(request.get("warm") or [])
    if STATE["mode"] == "baseline":
        warm += [spec["module"] for spec in request["functions"]]
    _warm(warm)
    pid = os.fork()
    if pid == 0:  # the child: one check, then exit without returning into the server loop
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            setup_error = STATE["setup_error"]
            if STATE["mode"] == "work" and request.get("django_settings"):
                W.GUARD_WRITES["allowed"] = False
                setup_error = W.setup_django(request)
            W.GUARD_WRITES["allowed"] = False
            out = W.execute(request, STATE["root_real"], setup_error)
            W.write_result(out, msg["result"])
        finally:
            os._exit(0)
    deadline = time.monotonic() + float(msg.get("timeout", 60))
    while True:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            return {"ok": False, "error": "timed out"}
        time.sleep(0.01)
    STATE["runs"] += 1
    return {"ok": True}


def serve(sock_path: str, init_path: str, mode: str) -> None:
    with open(init_path) as fh:
        init = json.load(fh)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)  # before the blocks go in
    server.listen(8)
    server.settimeout(IDLE_S)
    STATE["mode"] = mode
    STATE["root_real"], STATE["setup_error"] = W.prepare(init, with_django=(mode == "baseline"))
    W.GUARD_WRITES["allowed"] = True  # the server's own bookkeeping; children turn the blocks back on
    _warm(list(init.get("warm") or []))
    while True:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            break
        with conn:
            conn.settimeout(None)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            try:
                msg = json.loads(buf.decode() or "{}")
            except ValueError:
                msg = {}
            cmd = msg.get("cmd")
            if cmd == "stop":
                conn.sendall(b'{"ok": true}\n')
                break
            reply = {"ok": True, "runs": STATE["runs"], "warm": sorted(STATE["warm"])} if cmd == "ping" else _run(msg) if cmd == "run" else {"ok": False}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass
    try:
        os.unlink(sock_path)
    except OSError:
        pass


if __name__ == "__main__":
    serve(sys.argv[1], sys.argv[2], sys.argv[3])
    os._exit(0)
