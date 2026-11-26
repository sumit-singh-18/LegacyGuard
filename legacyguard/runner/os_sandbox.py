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
import signal
import subprocess
import sys
import tempfile

_PROBED: dict[str, bool] = {}
_LAST = {"label": "not used"}


def _real(path: str) -> str:
    return os.path.realpath(path)


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos(cmd: list[str], writable: list[str], readonly: list[str], long_lived: bool = False) -> list[str]:
    allow = " ".join(f"(subpath {_sb_quote(_real(p))})" for p in writable)
    deny = " ".join(f"(subpath {_sb_quote(_real(p))})" for p in readonly)
    profile = ["(version 1)", "(allow default)", '(deny network-outbound (remote ip "*:*"))', "(deny network-inbound)",
               "(deny file-write*)", f'(allow file-write* (literal "/dev/null") (subpath "/dev/fd") {allow})']
    unix = " ".join(f"(local unix-socket (subpath {_sb_quote(_real(p))}))" for p in writable)
    if unix:
        # Unix sockets in the temp folder only: the warm worker listens on one. Not the network.
        profile.append(f"(allow network-bind network-inbound {unix})")
    if deny:
        profile.append(f"(deny file-write* {deny})")  # later rules win: the project stays read-only inside the temp folder
    return ["sandbox-exec", "-p", "\n".join(profile), *cmd]


def _linux(cmd: list[str], writable: list[str], readonly: list[str], long_lived: bool = False) -> list[str]:
    # A long-lived warm server must outlive the hook process that started it.
    out = ["bwrap", *([] if long_lived else ["--die-with-parent"]), "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
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
            res = run(build([python, "-c", "pass"], [tmp], []), timeout=20, capture_output=True)
            _PROBED[python] = res.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _PROBED[python] = False
    return _PROBED[python]


def active(python: str, mode: str = "auto") -> bool:
    """Whether wrap() would put an OS sandbox around this interpreter's processes, known before wrap runs."""
    return mode != "off" and available(python)


def wrap(cmd: list[str], python: str, writable: list[str], readonly: list[str], mode: str = "auto", long_lived: bool = False) -> list[str]:
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
    return build(cmd, writable, readonly, long_lived)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group. The child is not reaped yet, so its pid (= the group id, from
    start_new_session) cannot have been reused."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # no process groups (Windows): the direct child only
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _reap(proc: subprocess.Popen):
    """Collect what the killed child wrote and its exit status. A grandchild that left the group (its own setsid) and
    still holds the pipes would block communicate(), so this waits at most 5 s for the output."""
    try:
        return proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001 - TimeoutExpired, or pipes already closed
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return None, None


def run(cmd: list[str], timeout: float | None = None, *, input=None, capture_output: bool = False, text: bool = False,
        cwd=None, env=None, stdout=None, stderr=None) -> subprocess.CompletedProcess:
    """subprocess.run for a command that runs project code (wrapped by wrap() or not), with a timeout that ends every
    process it started. The child starts in its own session and process group; on a timeout, or on any exception while
    waiting (KeyboardInterrupt included), the whole group gets SIGKILL and is reaped before the exception propagates.
    subprocess.run kills only the direct child: under bubblewrap that is bwrap, and the Python inside it was reparented
    to init and kept running (`--die-with-parent` does not cover bwrap itself being SIGKILLed).
    Returns a CompletedProcess; raises subprocess.TimeoutExpired (with any output) on a timeout. A child in its own
    session gets no terminal signals (Ctrl-C) from the hook's group; the kill here replaces that."""
    if capture_output:
        stdout = stderr = subprocess.PIPE
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if input is not None else None, stdout=stdout, stderr=stderr,
                            cwd=cwd, env=env, text=text, start_new_session=True)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = _reap(proc)
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err) from None
    except BaseException:
        _kill_group(proc)
        _reap(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def describe() -> str:
    """What the last run used, for the report: 'sandbox-exec', 'bubblewrap', or why there was no OS layer."""
    return _LAST["label"]
