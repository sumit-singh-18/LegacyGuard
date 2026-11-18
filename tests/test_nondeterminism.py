"""Task 6.8d: old code that disagrees with itself. The cause is named in the untested reason, and common benign
sources (clocks other than time.time, thread and process ids, default thread names) are frozen for the project's own
code, so such functions can be compared. Real differences next to them must still be found."""
from runner import diff, lg_codec, noise

from conftest import call, write_tree

SRC = '''
import asyncio
import os
import threading
import time
import uuid
from time import perf_counter


class Box:
    pass


class Tracker:
    def __init__(self):
        self.started = None
        self.owner = None
        self.pid = None

    def start(self, x: int):
        n = x
        self.started = time.monotonic()
        self.owner = threading.get_ident()
        self.pid = os.getpid()
        return n


def with_address(x: int):
    n = x
    return repr(Box()), n


def object_id(x: int):
    n = x
    return id(Box()), n


def timed(x: int):
    n = x
    began = perf_counter()
    return round(perf_counter() - began, 6), time.perf_counter_ns() > 0, n


def thread_name(x: int):
    n = x
    return threading.Thread(target=print).name, threading.current_thread().ident, n


def token(x: int):
    n = x
    return uuid.uuid4().hex, n


def secret(x: int):
    n = x
    return os.urandom(8).hex(), n


def waits(x: int):
    n = x
    deadline = time.monotonic() + 0.05
    rounds = 0
    while time.monotonic() < deadline:
        time.sleep(0.01)
        rounds += 1
    return rounds, n


def loop_timer(x: int):
    n = x
    async def main():
        fired = []
        asyncio.get_running_loop().call_later(0.01, fired.append, 1)
        await asyncio.sleep(0.05)
        return fired
    return asyncio.run(main()), n
'''


def _run(tmp_path, make_cfg, old_src, new_src):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new_src})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/m.py": old_src})
    cfg = make_cfg(root, behavioral_diff={"mutants": 0, "max_inputs": 12})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_benign_noise_is_comparable(tmp_path, make_cfg):
    # A refactor (n = x -> n = x + 0) of functions that read clocks, thread and process ids, thread names, uuids and
    # object reprs: the old code agrees with itself, so each one is checked and verified.
    got = _run(tmp_path, make_cfg, SRC, SRC.replace("n = x\n", "n = x + 0\n"))
    for name in ("Tracker.start", "with_address", "timed", "thread_name", "token", "waits", "loop_timer"):
        assert got[name].status == diff.DONE, (name, got[name].reason)
        assert got[name].dropped_nondeterministic == 0, name


def test_real_non_determinism_names_its_cause(tmp_path, make_cfg):
    got = _run(tmp_path, make_cfg, SRC, SRC.replace("n = x\n", "n = x + 0\n"))
    secret, ident = got["secret"], got["object_id"]
    assert secret.status == diff.UNTESTED and secret.reason.startswith("non-deterministic: old code disagreed with itself on")
    assert "random tokens in the return value" in secret.reason, secret.reason
    assert secret.nondeterministic_causes == {noise.RANDOM_TOKENS: secret.dropped_nondeterministic}
    assert ident.status == diff.UNTESTED and "object ids in the return value" in ident.reason, ident.reason


def test_changed_value_next_to_noise_is_still_found(tmp_path, make_cfg):
    # The frozen sources and the address scrub apply to old and new alike: a value the edit changes stays a difference.
    new = (SRC.replace("    return repr(Box()), n\n", "    return repr(Box()), n + 1\n")
              .replace("        self.pid = os.getpid()\n", "        self.pid = os.getpid() + 1\n")
              .replace("    return round(perf_counter() - began, 6), time.perf_counter_ns() > 0, n\n",
                       "    perf_counter()\n    return round(perf_counter() - began, 6), time.perf_counter_ns() > 0, n\n")
              .replace("threading.Thread(target=print).name", "threading.Thread(target=print, name='w').name"))
    got = _run(tmp_path, make_cfg, SRC, new)
    for name in ("with_address", "Tracker.start", "timed", "thread_name"):
        assert got[name].status == diff.DIFFERENT, (name, got[name].reason)
    assert "self.pid=4243" in got["Tracker.start"].differences[0]["after"]
    assert "0x…" in got["with_address"].differences[0]["before"]


def test_clock_extra_reading_is_a_difference(tmp_path, make_cfg):
    # One more reading of the frozen monotonic clock moves the stored value: the frozen clock does not hide it.
    new = SRC.replace("        self.started = time.monotonic()\n", "        time.monotonic()\n        self.started = time.monotonic()\n")
    got = _run(tmp_path, make_cfg, SRC, new)
    assert got["Tracker.start"].status == diff.DIFFERENT
    assert "self.started" in got["Tracker.start"].differences[0]["before"]


def test_library_clocks_stay_real(run_worker, tmp_path):
    # Code outside the project (here asyncio, threading) keeps the real clock and ids; the project's code gets frozen ones.
    root = write_tree(tmp_path / "p", {"m.py": '''
        import asyncio, os, threading, time
        def probe():
            loop = asyncio.new_event_loop()
            try:
                lib = loop.time()
            finally:
                loop.close()
            return time.monotonic(), lib > 1001, threading.get_ident(), os.getpid(), threading.Thread().name
    '''})
    out = run_worker(root, [{"module": "m", "qualname": "probe", "inputs": [call(), call()]}])["m:probe"]
    first, second = (r["value"] for r in out["results"])
    assert first == second
    mono, lib_real, ident, pid, name = (lg_codec.display(v) for v in first["v"])
    assert mono == "1000.001" and ident == "1" and pid == "4242" and name == "'Thread-1'"
    assert out["env"]["pid"] != 4242 and out["env"]["threads"]


def _ret(v, state=None):
    out = {"k": "return", "v": lg_codec.canon(v)}
    if state:
        out["s"] = {k: lg_codec.canon(x) for k, x in state.items()}
    return out


ENV_A = {"pid": 501, "ppid": 1, "threads": [6100000001, 77], "clocks": {"monotonic": [5000.0, 5003.0], "time": [1.7e9, 1.7e9 + 3]}, "paths": ["/tmp/lgrunA"]}
ENV_B = {"pid": 502, "ppid": 1, "threads": [6200000002, 78], "clocks": {"monotonic": [5010.0, 5012.0], "time": [1.7e9 + 10, 1.7e9 + 12]}, "paths": ["/tmp/lgrunB"]}


def test_causes_by_kind():
    c = noise.causes
    assert c(_ret("<Foo at 0x1f>"), _ret("<Foo at 0x2e>")) == [(noise.ADDRESSES, "the return value")]
    assert c(_ret(140230000000016), _ret(140230000004096)) == [(noise.IDS, "the return value")]
    assert c(_ret(None, {"self": {"owner": 6100000001}}), _ret(None, {"self": {"owner": 6200000002}}), ENV_A, ENV_B) == [(noise.THREAD_IDS, "self")]
    assert c(_ret(f"pid 501"), _ret(f"pid 502"), ENV_A, ENV_B) == [(noise.PROCESS_IDS, "the return value")]
    assert c(_ret(5001.25), _ret(5011.5), ENV_A, ENV_B) == [(noise.TIME, "the return value")]
    assert c(_ret(int(5001.25e9)), _ret(int(5011.5e9)), ENV_A, ENV_B) == [(noise.TIME, "the return value")]
    assert c(_ret("at 2026-11-16 10:00:01"), _ret("at 2026-11-16 10:00:09")) == [(noise.TIME, "the return value")]
    assert c(_ret("id 9b2f3c0e-1d2a-4c55-9e0a-0123456789ab"), _ret("id 0c1d2e3f-4a5b-4c6d-8e7f-fedcba987654")) == [(noise.UUIDS, "the return value")]
    assert c(_ret(["b", "a", "c"]), _ret(["a", "c", "b"])) == [(noise.ORDER, "the return value")]
    assert c(_ret("a,b,c"), _ret("c,a,b")) == [(noise.ORDER, "the return value")]
    assert c(_ret("/tmp/lgrunA/x.txt"), _ret("/tmp/lgrunB/x.txt"), ENV_A, ENV_B) == [(noise.TEMP_NAMES, "the return value")]
    assert c(_ret("/var/tmpab12cd34/f"), _ret("/var/tmpzz98yy76/f")) == [(noise.TEMP_NAMES, "the return value")]
    assert c(_ret("Thread-3 (run)"), _ret("Thread-4 (run)")) == [(noise.THREAD_NAMES, "the return value")]
    assert c(_ret(1.0000000000001), _ret(1.0000000000002)) == [(noise.FLOAT_NOISE, "the return value")]
    assert c(_ret(0.8444218515250481), _ret(0.7579544029403025)) == [(noise.RANDOM_NUMBERS, "the return value")]
    assert c(_ret(b"\x01\x02\x03\x04\x05\x06\x07\x08\x09"), _ret(b"\x13\x14\x15\x16\x17\x18\x19\x1a\x1b")) == [(noise.RANDOM_BYTES, "the return value")]
    assert c(_ret("3f9a1c22d0e4b7aa"), _ret("0b7e5d11c2a4f9e3")) == [(noise.RANDOM_TOKENS, "the return value")]
    assert c(_ret("yes"), _ret("no")) == [(noise.OTHER, "the return value")]
    assert c(_ret(1), None) == [(noise.UNFINISHED, "the outcome")]
    raised = {"k": "raise", "type": "ValueError", "message": "worker 501 busy"}
    assert c(raised, {**raised, "message": "worker 502 busy"}, ENV_A, ENV_B) == [(noise.PROCESS_IDS, "the exception")]


def test_reason_names_the_cause():
    t = diff.Target("m.py", "f", "function", [{"name": "a0"}], "h")
    items = [call(1), call(2), call(3)]

    def ran(*values):
        return {"status": "ran", "results": [{"outcome": "return", "value": lg_codec.canon(v)} for v in values], "lines": None}
    a = ran("<X at 0x1f>", "<X at 0x2f>", 7)
    b = ran("<X at 0x3f>", "<X at 0x4f>", 7)
    r = diff.compare_one(t, items, a, b, ran("<X at 0x1f>", "<X at 0x2f>", 7), True)
    assert r.status == diff.UNTESTED
    assert r.reason == "non-deterministic: old code disagreed with itself on 2 of 3 inputs: object addresses in the return value (2)"
    assert r.nondeterministic_causes == {noise.ADDRESSES: 2}


def test_plain_values_get_no_benign_label():
    # Heuristic labels must not excuse ordinary values that differ: words, round numbers, sizes.
    c = noise.causes
    assert c(_ret("approved"), _ret("rejected")) == [(noise.OTHER, "the return value")]
    assert c(_ret(0.25), _ret(0.75)) == [(noise.OTHER, "the return value")]
    assert c(_ret(3600000000), _ret(3600000008)) == [(noise.OTHER, "the return value")]
    assert c(_ret(b"ok"), _ret(b"no")) == [(noise.OTHER, "the return value")]
    for label in (noise.IDS, noise.RANDOM_NUMBERS, noise.RANDOM_TOKENS, noise.RANDOM_BYTES):
        assert label.startswith(("values that look like", "bytes that look"))


WAITS = '''
import queue
import select
import threading
import time

LIMIT = 0.1


def on_event(x: int):
    n = x
    event = threading.Event()
    rounds = 0
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        event.wait(0.01)
        rounds += 1
    return rounds, n


def on_condition(x: int):
    n = x
    cond = threading.Condition()
    rounds = 0
    deadline = time.perf_counter() + LIMIT
    with cond:
        while time.perf_counter() < deadline:
            cond.wait(0.01)
            rounds += 1
    return rounds, n


def on_queue(x: int):
    n = x
    q = queue.Queue()
    rounds = 0
    deadline = time.time() + LIMIT
    while time.time() < deadline:
        try:
            q.get(timeout=0.01)
        except queue.Empty:
            rounds += 1
    return rounds, n


def on_join(x: int):
    # The thread stays alive until the loop is over (it waits for `done`, set after the loop), so every join(0.01)
    # times out and the round count depends on the frozen clocks only. A thread that ends on its own after a real
    # delay would race the loop's real duration, which is not repeatable on a slow machine.
    n = x
    done = threading.Event()
    t = threading.Thread(target=done.wait, daemon=True)
    t.start()
    rounds = 0
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline and t.is_alive():
        t.join(0.01)
        rounds += 1
    done.set()
    t.join()
    return rounds, n


def on_select(x: int):
    n = x
    rounds = 0
    deadline = time.monotonic() + LIMIT
    while time.monotonic() < deadline:
        select.select([], [], [], 0.01)
        rounds += 1
    return rounds, n
'''
WAIT_NAMES = ("on_event", "on_condition", "on_queue", "on_join", "on_select")


def test_library_wait_deadline_loops_finish(tmp_path, make_cfg):
    # A loop that waits in Event/Condition/Queue/Thread.join/select until a deadline read from a frozen clock: the
    # frozen clocks move on by each timed-out wait, so it ends after as many rounds as with the real clock.
    got = _run(tmp_path, make_cfg, WAITS, WAITS.replace("n = x\n", "n = x + 0\n"))
    for name in WAIT_NAMES:
        assert got[name].status == diff.DONE, (name, got[name].reason)


def test_deadline_edit_is_detected(tmp_path, make_cfg):
    got = _run(tmp_path, make_cfg, WAITS, WAITS.replace("LIMIT = 0.1", "LIMIT = 0.05"))
    for name in WAIT_NAMES:
        assert got[name].status == diff.DIFFERENT, (name, got[name].reason)


def test_uncovered_wait_on_frozen_clock_says_so(run_worker, tmp_path):
    # Lock.acquire(timeout=...) is not one of the covered waits: the loop runs out of time, and the outcome says it did
    # so after many frozen clock readings.
    root = write_tree(tmp_path / "p", {"m.py": '''
        import threading, time
        def spin():
            lock = threading.Lock()
            lock.acquire()
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                lock.acquire(timeout=0.001)
            return 1
    '''})
    out = run_worker(root, [{"module": "m", "qualname": "spin", "inputs": [call()]}])["m:spin"]
    assert out["results"][0]["outcome"] == "timeout" and out["results"][0]["clock_loop"] >= 200


def test_reason_for_a_clock_loop():
    t = diff.Target("m.py", "f", "function", [{"name": "a0"}], "h")
    timed_out = {"status": "ran", "results": [{"outcome": "timeout", "clock_loop": 900}], "lines": None}
    r = diff.compare_one(t, [call(1)], timed_out, timed_out, timed_out, True)
    assert r.status == diff.UNTESTED
    assert "1 inputs ran past the time limit; 1 of them after many frozen clock readings (a loop waiting for a deadline on the frozen clock" in r.reason


CLOCKED = '''
import logging
import time

log = logging.getLogger(__name__)


def measure(x: int):
    t0 = time.monotonic()
    total = sum(range(x % 50))
    return round(time.monotonic() - t0, 6), total
'''


def test_added_clock_read_is_a_hedged_difference(tmp_path, make_cfg):
    # An edit that only adds a clock read shifts the frozen clock by 1 ms: it stays a difference (as for time.time),
    # with a note that it may be the shift alone.
    new = CLOCKED.replace("    return round(", "    log.debug('t %s', time.monotonic())\n    return round(")
    r = _run(tmp_path, make_cfg, CLOCKED, new)["measure"]
    assert r.status == diff.DIFFERENT
    assert "read the frozen clock a different number of times" in r.clock_note
    assert r.clock_note in diff.format_wake(r, "LG-1")
    # A value change that does not touch the clock gets no such note.
    plain = _run(tmp_path / "plain", make_cfg, CLOCKED, CLOCKED.replace("range(x % 50)", "range(x % 49)"))["measure"]
    assert plain.status == diff.DIFFERENT and not plain.clock_note
