"""lg_worker.py run as a subprocess on small projects: outcomes, the sandbox, state after the call, determinism."""
from __future__ import annotations

from conftest import call, write_tree

MOD = '''
import datetime, os, random, shutil, socket, subprocess, time, uuid
from datetime import datetime as DT
from uuid import uuid4

def add(a, b):
    return a + b

def boom(x):
    raise ValueError(f"bad {x}")

def fetch(host):
    return socket.create_connection((host, 80))

def write_here(name):
    with open(name, "w") as fh:
        fh.write("x")

def delete(path):
    os.remove(path)
    return "deleted"

def nuke(path):
    shutil.rmtree(path)

def move(a, b):
    os.rename(a, b)

def low_level_write(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    os.write(fd, b"x")

def spawn():
    return os.system("true")

def push_last(xs):
    xs.append(xs[-1])
    return len(xs)

def dice(n):
    return [random.randint(1, 6) for _ in range(n)]

def stamp(n):
    return [datetime.datetime.now().isoformat(), DT.now().isoformat(), time.time() > 0, str(uuid4()), str(uuid.uuid4())][:n]

def busy(n):
    total = 0
    while True:
        total += n

def same_instant():
    a = DT.now()
    b = DT.now()
    return b > a

def is_dt(v):
    return isinstance(v, DT) and isinstance(DT.now(), datetime.datetime)

class Counter:
    def __init__(self):
        self.n = 0

    def bump(self, k):
        self.n += k
        return None
'''


def project(tmp_path):
    root = write_tree(tmp_path / "proj", {"mod.py": MOD, "keep/data.txt": "keep me", "keep/sub/x.txt": "x"})
    return root


def run1(run_worker, root, qualname, *items, **extra):
    out = run_worker(root, [{"module": "mod", "qualname": qualname, "inputs": list(items)}], **extra)
    return out[f"mod:{qualname}"]


def test_return_and_raise(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "add", call(2, 3))
    assert r["status"] == "ran" and r["results"][0]["value"] == {"t": "v", "v": 5}
    e = run1(run_worker, root, "boom", call(1))["results"][0]
    assert e["outcome"] == "raise" and e["type"] == "ValueError"


def test_network_blocked(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "fetch", call("example.com"))["results"][0]
    assert r["outcome"] == "blocked" and "network" in r["blocked_kinds"]


def test_write_into_repo_blocked(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "write_here", call("new.txt"))["results"][0]
    assert r["outcome"] == "blocked" and not (root / "new.txt").exists()


def test_delete_blocked(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "delete", call(str(root / "keep" / "data.txt")))["results"][0]
    assert r["outcome"] == "blocked" and "file write" in r["blocked_kinds"]
    assert (root / "keep" / "data.txt").read_text() == "keep me"


def test_rmtree_and_rename_blocked(tmp_path, run_worker):
    root = project(tmp_path)
    run1(run_worker, root, "nuke", call(str(root / "keep")))
    run1(run_worker, root, "move", call(str(root / "keep" / "data.txt"), str(root / "moved.txt")))
    assert (root / "keep" / "sub" / "x.txt").exists() and (root / "keep" / "data.txt").exists()
    assert not (root / "moved.txt").exists()


def test_low_level_write_blocked(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "low_level_write", call(str(root / "raw.bin")))["results"][0]
    assert r["outcome"] == "blocked" and not (root / "raw.bin").exists()


def test_os_system_blocked(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "spawn", call())["results"][0]
    assert r["outcome"] == "blocked" and "subprocess" in r["blocked_kinds"]


def test_scratch_temp_writes_allowed(tmp_path, run_worker):
    import tempfile
    scratch = tempfile.mkdtemp(prefix="lgtest")
    root = project(tmp_path)
    target = f"{scratch}/ok.txt"
    r = run1(run_worker, root, "write_here", call(target))["results"][0]
    assert r["outcome"] == "return"


def test_argument_mutation_is_reported(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "push_last", call([9, 0]))["results"][0]
    assert r["outcome"] == "return"
    assert r["state"]["a0"] == {"t": "list", "v": [{"t": "v", "v": 9}, {"t": "v", "v": 0}, {"t": "v", "v": 0}]}


def test_self_state_is_reported(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "Counter.bump", call(5))["results"][0]
    assert r["state"]["self"]["state"]["v"] == [[{"t": "v", "v": "n"}, {"t": "v", "v": 5}]]


def test_immutable_arguments_add_no_state(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "add", call(2, 3))["results"][0]
    assert "state" not in r


def test_random_time_uuid_are_repeatable(tmp_path, run_worker):
    root = project(tmp_path)
    first = run1(run_worker, root, "dice", call(5), call(5))
    second = run1(run_worker, root, "dice", call(5), call(5))
    assert first["results"] == second["results"]
    assert first["results"][0] == first["results"][1]  # seeded per call, so the same input gives the same answer
    a = run1(run_worker, root, "stamp", call(5))
    b = run1(run_worker, root, "stamp", call(5))
    assert a["results"] == b["results"]


def test_clock_still_moves_forward(tmp_path, run_worker):
    root = project(tmp_path)
    r = run1(run_worker, root, "same_instant", call())["results"][0]
    assert r["value"] == {"t": "v", "v": True}


def test_frozen_datetime_passes_isinstance(tmp_path, run_worker):
    import datetime as dt
    root = project(tmp_path)
    r = run1(run_worker, root, "is_dt", call(dt.datetime(2020, 1, 1)))["results"][0]
    assert r["value"] == {"t": "v", "v": True}


def test_determinism_can_be_switched_off(tmp_path, run_worker):
    root = project(tmp_path)
    a = run1(run_worker, root, "dice", *[call(20)] * 3, deterministic=False)
    values = [str(x["value"]) for x in a["results"]]
    assert len(set(values)) > 1


def test_slow_function_stops_early(tmp_path, run_worker):
    import time
    root = project(tmp_path)
    start = time.monotonic()
    out = run1(run_worker, root, "busy", *[call(i) for i in range(30)])
    outcomes = [r["outcome"] for r in out["results"]]
    assert outcomes.count("timeout") <= 10 and outcomes.count("skipped_slow") >= 20
    assert time.monotonic() - start < 20


def test_library_subclassing_datetime_at_import_still_works(tmp_path, run_worker):
    # pydantic and others subclass datetime with their own metaclass at import time. The frozen clock must not be
    # in place then, or the import fails with a metaclass conflict (found with FastAPI in Sprint 3).
    src = (
        "import abc, datetime\n\n"
        "class Stamp(datetime.datetime, metaclass=abc.ABCMeta):\n    pass\n\n"
        "def now_year():\n    return datetime.datetime.now().year\n"
    )
    root = write_tree(tmp_path / "dtlib", {"lib.py": src})
    out = run_worker(root, [{"module": "lib", "qualname": "now_year", "inputs": [call()]}])["lib:now_year"]
    assert out["status"] == "ran" and out["results"][0]["value"] == {"t": "v", "v": 2024}  # the frozen clock still applies


def test_pathlib_read_after_blocks(tmp_path, run_worker):
    # Python 3.10's pathlib stores io.open as a class attribute; a plain-function replacement got bound as a method
    # and every Path.read_text() failed (found when FastAPI read package metadata at import on 3.10 in CI).
    src = "from pathlib import Path\n\ndef read(p):\n    return Path(p).read_text()\n"
    root = write_tree(tmp_path / "pl", {"pl.py": src, "data.txt": "hello"})
    out = run_worker(root, [{"module": "pl", "qualname": "read", "inputs": [call(str(root / "data.txt"))]}])["pl:read"]
    assert out["results"][0] == {"outcome": "return", "value": {"t": "v", "v": "hello"}}


def test_bind_only_on_loopback(tmp_path, run_worker):
    src = ("import socket\n\ndef serve(host):\n    s = socket.socket()\n    s.bind((host, 0))\n    s.close()\n    return 'bound'\n")
    root = write_tree(tmp_path / "b", {"b.py": src})
    out = run_worker(root, [{"module": "b", "qualname": "serve", "inputs": [call("127.0.0.1"), call("0.0.0.0")]}])["b:serve"]
    assert out["results"][0]["value"] == {"t": "v", "v": "bound"}
    assert out["results"][1]["outcome"] == "blocked"
