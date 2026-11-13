"""OS-level sandbox for the worker process (roadmap A2, layer 1).

The worker's own blocks (monkeypatches and a PEP 578 audit hook) live inside the process they guard, so code
that goes around Python (a C extension, ctypes) is not stopped by them. Where the operating system offers a
sandbox, the worker runs inside one as well: no network, and no writes except to the temp folder. The project
and the baseline stay read-only even inside the temp folder.

- macOS: sandbox-exec with a generated profile (deprecated by Apple but present and working).
- Linux: bubblewrap (bwrap) with a read-only root, a private network namespace and a writable temp folder.
- Anything else, or when the probe fails (no bwrap, user namespaces disabled): no OS layer, and describe() says so.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

_PROBED: dict[str, bool] = {}
_LAST = {"label": "not used"}


def _real(path: str) -> str:
    return os.path.realpath(path)


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos(cmd: list[str], writable: list[str], readonly: list[str]) -> list[str]:
    allow = " ".join(f"(subpath {_sb_quote(_real(p))})" for p in writable)
    deny = " ".join(f"(subpath {_sb_quote(_real(p))})" for p in readonly)
    profile = ["(version 1)", "(allow default)", '(deny network-outbound (remote ip "*:*"))', "(deny network-inbound)",
               "(deny file-write*)", f'(allow file-write* (literal "/dev/null") (subpath "/dev/fd") {allow})']
    if deny:
        profile.append(f"(deny file-write* {deny})")  # later rules win: the project stays read-only inside the temp folder
    return ["sandbox-exec", "-p", "\n".join(profile), *cmd]


def _linux(cmd: list[str], writable: list[str], readonly: list[str]) -> list[str]:
    out = ["bwrap", "--die-with-parent", "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for p in writable:
        out += ["--bind", _real(p), _real(p)]
    for p in readonly:
        out += ["--ro-bind", _real(p), _real(p)]  # after the writable binds, so it wins
    return out + cmd


def _builder():
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "sandbox-exec", _macos
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "bubblewrap", _linux
    return None, None


def available(python: str) -> bool:
    """Whether the OS sandbox starts on this machine with this interpreter. Probed once per interpreter."""
    name, build = _builder()
    if build is None:
        return False
    if python not in _PROBED:
        tmp = tempfile.gettempdir()
        try:
            res = subprocess.run(build([python, "-c", "pass"], [tmp], []), capture_output=True, timeout=20)
            _PROBED[python] = res.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _PROBED[python] = False
    return _PROBED[python]


def wrap(cmd: list[str], python: str, writable: list[str], readonly: list[str], mode: str = "auto") -> list[str]:
    """cmd inside the OS sandbox when one is available and mode is not "off"; otherwise cmd unchanged."""
    name, build = _builder()
    if mode == "off":
        _LAST["label"] = "switched off in config"
        return cmd
    if build is None or not available(python):
        _LAST["label"] = "not available on this machine" if build is None else f"{name} could not start here"
        return cmd
    _LAST["label"] = name
    writable = [p for p in dict.fromkeys(writable) if p and os.path.exists(p)]
    readonly = [p for p in dict.fromkeys(readonly) if p and os.path.exists(p)]
    return build(cmd, writable, readonly)


def describe() -> str:
    """What the last run used, for the report: 'sandbox-exec', 'bubblewrap', or why there was no OS layer."""
    return _LAST["label"]
