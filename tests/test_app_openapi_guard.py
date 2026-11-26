"""Sprint 7.6 Part B, guard side: the code-first OpenAPI check with `run_tree` replaced by a canned builder, so no
FastAPI is needed (tests 7b, 7c, 10a, 10b, 10d, 11b, 11c, 13, 14, 18, 20, 21 of the plan).

The canned builder reads the tree the way the app would import it: from the `app:` module it follows `import` lines to
files in the tree, takes `class X(BaseModel):` fields as schemas and `@app.<method>("<path>")` handlers as operations,
and returns the same result shape as `runner/lg_openapi.py`. A module containing a top-level `raise` fails to import.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

import oasdiff_fake as fake
from conftest import PLUGIN, write_tree
from guardlib import app_openapi, audit, ci, contract_tools, engine, ledger, symbols, waivers
from guardlib.checks import Break

MODELS = "from pydantic import BaseModel\n\n\nclass Note(BaseModel):\n    body: str\n    n: int = 0\n"
RENAMED = MODELS.replace("body: str", "text: str")
APP = ('from fastapi import FastAPI\nfrom svc.models import Note\n\napp = FastAPI(title="t", version="1")\n\n\n'
       '@app.post("/notes")\ndef create(note: Note) -> Note:\n    return note\n')
FILES = {"svc/__init__.py": "", "svc/models.py": MODELS, "svc/app.py": APP, "tools/script.py": "X = 1\n"}

CLASS = re.compile(r"^class (\w+)\(BaseModel\):\n((?:    .*\n?)*)", re.M)
FIELD = re.compile(r"^    (\w+): (\w+)(?: = (.+))?$", re.M)
ROUTE = re.compile(r'^@app\.(\w+)\("([^"]+)"\)\ndef (\w+)\(([^)]*)\)(?: -> (\w+))?:', re.M)
IMPORT = re.compile(r"^(?:from ([\w.]+) import|import ([\w.]+))", re.M)
VERSIONS = {"fastapi": "0.0-canned", "pydantic": "2.0-canned"}


def canned_build(tree, ref: str, versions=None) -> dict:
    tree = Path(tree)
    module = ref.split(":")[0]
    seen, todo = [], [module]
    while todo:
        m = todo.pop()
        parts = m.split(".")
        for i in range(1, len(parts)):
            init = tree.joinpath(*parts[:i], "__init__.py")
            if init.exists() and init not in seen:
                seen.append(init)
        f = tree.joinpath(*parts).with_suffix(".py")
        if not f.exists():
            f = tree.joinpath(*parts, "__init__.py")
        if not f.exists() or f in seen:
            continue
        seen.append(f)
        text = f.read_text()
        if re.search(r"^raise ", text, re.M):
            return {"kind": None, "text": None, "error": "import", "detail": "RuntimeError: boom", "blocks": [],
                    "files": [str(p) for p in seen], "versions": dict(versions or VERSIONS)}
        todo += [a or b for a, b in IMPORT.findall(text)]
    schemas, paths = {}, {}
    for f in seen:
        text = f.read_text()
        for name, body in CLASS.findall(text):
            props, required = {}, []
            for fname, ftype, default in FIELD.findall(body):
                props[fname] = {"type": {"str": "string", "int": "integer"}.get(ftype, ftype)}
                if default:
                    props[fname]["default"] = default
                else:
                    required.append(fname)
            schemas[name] = {"properties": props, "required": required, "title": name, "type": "object"}
    for f in seen:
        for method, path, fn, params, ret in ROUTE.findall(f.read_text()):
            op = {"operationId": f"{fn}_{path.strip('/').replace('/', '_')}_{method}", "responses": {"200": {"description": "ok"}}}
            body = next((p.split(":")[1].strip() for p in params.split(",") if ":" in p and p.split(":")[1].strip() in schemas), None)
            if body:
                op["requestBody"] = {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{body}"}}}}
            if ret in schemas:
                op["responses"]["200"]["content"] = {"application/json": {"schema": {"$ref": f"#/components/schemas/{ret}"}}}
            paths.setdefault(path, {})[method] = op
    spec = {"openapi": "3.1.0", "info": {"title": "t", "version": "1"}, "paths": paths, "components": {"schemas": schemas}}
    text = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    app_file = tree.joinpath(*module.split(".")).with_suffix(".py")
    return {"kind": "fastapi", "text": text, "sha": hashlib.sha256(text.encode()).hexdigest()[:16], "bytes": len(text),
            "files": sorted(str(os.path.realpath(p)) for p in seen), "app_file": os.path.realpath(app_file),
            "versions": dict(versions or VERSIONS), "blocks": [], "error": None, "seconds": 0.0}


class Builds:
    """Replaces app_openapi.run_tree. calls: roles in order; plan: per-role queue of "ok" | "fail" | a dict."""

    def __init__(self, cfg, base: Path, lock_check: bool = False):
        self.cfg, self.base = cfg, os.path.realpath(base)
        self.calls: list[str] = []
        self.plan: dict[str, list] = {"base": [], "work": []}
        self.versions = {"base": None, "work": None}
        self.fail_always = {"base": False, "work": False}
        self.lock_check = lock_check
        self.mu = threading.Lock()

    def role(self, tree) -> str:
        return "base" if os.path.realpath(tree) == self.base else "work"

    def count(self, role: str) -> int:
        return self.calls.count(role)

    def __call__(self, cfg, tree, timeout):
        if self.lock_check:  # the ledger lock must be free while project code runs (test 18)
            # A shared probe conflicts with the ledger's exclusive lock but not with the other parallel builds' probes.
            with open(self.cfg.state_dir / ".session.lock", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(fh, fcntl.LOCK_UN)
        role = self.role(tree)
        with self.mu:
            self.calls.append(role)
            step = self.plan[role].pop(0) if self.plan[role] else "ok"
        if self.fail_always[role] or step == "fail":
            return {"kind": None, "text": None, "error": "import", "detail": "RuntimeError: the baseline exploded",
                    "blocks": [], "files": [], "versions": dict(VERSIONS)}
        if isinstance(step, dict):
            return step
        return canned_build(tree, str(cfg.raw.get("app")), self.versions[role])


class Session:
    def __init__(self, tmp_path, make_cfg, monkeypatch, base_files=None, work_files=None, mode="diff", app="svc.app:app",
                 lock_check=False, **overrides):
        base_files = dict(FILES if base_files is None else base_files)
        self.root = write_tree(tmp_path / "proj", dict(base_files if work_files is None else work_files))
        self.cfg = make_cfg(self.root, app=app, **overrides)
        self.base = write_tree(self.cfg.state_dir / "baseline", base_files)
        with ledger.locked(self.cfg.state_dir) as data:
            data.update(baseline_ok=True, baseline_commit="c1", session_id="s")
        self.tmp = tmp_path
        bin_dir = fake.install(tmp_path, mode)
        monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        self.builds = Builds(self.cfg, self.base, lock_check)
        monkeypatch.setattr(app_openapi, "run_tree", self.builds)
        monkeypatch.setattr(engine, "targets_for", lambda *a, **k: [])
        monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
        import post_edit
        monkeypatch.setattr(post_edit, "load_guard", lambda event, fp=None: (str(self.root), self.cfg))
        self.post_edit = post_edit
        monkeypatch.setattr(audit.baseline, "changed_files", lambda cfg: self.changed())

    def write(self, rel: str, text: str) -> None:
        write_tree(self.root, {rel: text})

    def changed(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root).as_posix()
            if p.is_file() and not rel.startswith(".legacyguard/"):
                b = self.base / rel
                if not b.exists() or b.read_text() != p.read_text():
                    out.append(rel)
        return out

    def edit(self, rel: str, text: str | None = None):
        if text is not None:
            self.write(rel, text)
        return self.post_edit.handle({"tool_input": {"file_path": str(self.root / rel)}, "cwd": str(self.root)})

    def data(self) -> dict:
        return ledger.load(self.cfg.state_dir)

    def audit(self) -> tuple[dict, list]:
        with ledger.locked(self.cfg.state_dir) as data:
            blocking = audit.run(self.cfg, data)
        return ledger.load(self.cfg.state_dir), blocking


def _ctx(out) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def _oas(data):
    return [f for f in data["findings"] if f["kind"] == "contract_break" and f.get("source") == "app-openapi"]


# 10a
def test_trigger_first_edit_runs_once_then_only_imported_files(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch)
    assert s.edit("tools/script.py", "X = 2\n") is None
    assert s.builds.calls.count("base") == 2 and s.builds.count("work") == 1
    state = s.data()["app_openapi"]
    assert state["base_files"] == ["svc/__init__.py", "svc/app.py", "svc/models.py"] and state["app_file"] == "svc/app.py"
    assert (s.cfg.state_dir / app_openapi.CACHE_FILE).exists()
    s.edit("tools/script.py", "X = 3\n")
    assert len(s.builds.calls) == 3  # not imported by the app: nothing runs once the cache exists
    s.edit("svc/models.py", MODELS + "# c\n")
    assert len(s.builds.calls) == 4 and s.builds.calls[-1] == "work"


def test_trigger_never_for_tests_or_when_switched_off(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch)
    s.edit("tests/test_x.py", "def test_x():\n    assert 1\n")
    assert s.builds.calls == []
    s.cfg.raw["contract_tools"]["openapi_from_app"] = False
    s.edit("svc/models.py", RENAMED)
    assert s.builds.calls == []
    assert not app_openapi.applies(s.cfg, "svc/models.py", None, {"baseline_commit": "c1"})
    s.cfg.raw["contract_tools"]["openapi_from_app"] = "auto"
    s.cfg.raw["app"] = None
    assert not app_openapi.applies(s.cfg, "svc/models.py", None, {"baseline_commit": "c1"})


# 10b
def test_trigger_top_level_module_app(tmp_path, make_cfg, monkeypatch):
    files = {"models.py": MODELS, "main.py": APP.replace("from svc.models", "from models"), "other.py": "Y = 1\n"}
    s = Session(tmp_path, make_cfg, monkeypatch, base_files=files, app="main:app")
    s.edit("models.py", MODELS + "# c\n")
    assert len(s.builds.calls) == 3
    state = s.data()["app_openapi"]
    assert "models.py" in state["base_files"] and state["app_file"] == "main.py"
    s.edit("other.py", "Y = 2\n")
    assert len(s.builds.calls) == 3
    with pytest.raises(Exception) as woke:
        s.edit("models.py", RENAMED)
    assert "property 'body' removed from Note" in str(woke.value) and "app: main:app" in str(woke.value)
    assert len(s.builds.calls) == 4


# 10d
def test_trigger_file_created_in_the_session(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch)
    s.edit("svc/models.py", MODELS + "# c\n")
    assert len(s.builds.calls) == 3
    s.edit("svc/schemas.py", "from pydantic import BaseModel\n\n\nclass Extra(BaseModel):\n    a: int\n")
    assert len(s.builds.calls) == 3  # not imported yet
    s.edit("svc/models.py", "from svc.schemas import Extra\n" + MODELS)
    assert len(s.builds.calls) == 4 and "svc/schemas.py" in s.data()["app_openapi"]["work_files"]
    s.edit("svc/schemas.py", "from pydantic import BaseModel\n\n\nclass Extra(BaseModel):\n    a: int\n    b: int = 0\n")
    assert len(s.builds.calls) == 5


# 13
def test_cache_reuse_and_rebuild(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch)
    s.edit("svc/models.py", MODELS + "# 1\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (2, 1)
    s.edit("svc/models.py", MODELS + "# 2\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (2, 2)  # only the working tree
    raw = json.dumps(s.data())
    assert '"openapi"' not in raw and "components" not in raw  # session.json holds no spec text
    cache = json.loads((s.cfg.state_dir / app_openapi.CACHE_FILE).read_text())
    assert cache["a"]["text"].startswith("{") and cache["b"] is None and cache["stable"] is True

    s.builds.versions["work"] = {"fastapi": "9.9", "pydantic": "2.0-canned"}
    s.builds.versions["base"] = {"fastapi": "9.9", "pydantic": "2.0-canned"}
    s.edit("svc/models.py", MODELS + "# 3\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (4, 3)  # another FastAPI: the baseline is rebuilt
    s.edit("svc/models.py", MODELS + "# 4\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (4, 4)

    (s.cfg.state_dir / app_openapi.CACHE_FILE).write_text("{not json")
    s.edit("svc/models.py", MODELS + "# 5\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (6, 5)
    (s.cfg.state_dir / app_openapi.CACHE_FILE).unlink()
    s.edit("svc/models.py", MODELS + "# 6\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (8, 6)

    s.cfg.raw["app"] = "svc.app:application"
    s.edit("svc/models.py", MODELS + "# 7\n")
    assert (s.builds.count("base"), s.builds.count("work")) == (10, 7)  # a new key


# 7b
def test_half_built_baseline_is_retried_once_and_never_cached(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch, work_files={**FILES, "svc/models.py": RENAMED})
    s.builds.plan["base"] = ["ok", "fail", "fail"]
    out = s.edit("svc/models.py")
    assert s.builds.count("base") == 3  # two builds and one retry
    ctx = _ctx(out)
    assert "property 'body' removed from Note" in ctx and app_openapi.HEDGE_ONCE in ctx
    assert not (s.cfg.state_dir / app_openapi.CACHE_FILE).exists()
    data = s.data()
    assert data["app_openapi"]["failed"]["attempts"] == 1 and not data["app_openapi"]["failed"]["paused"]
    assert data["wakeups"] == 0 and data["app_openapi_seen"] == []
    with pytest.raises(Exception) as woke:
        s.edit("svc/models.py", RENAMED + "# again\n")
    assert "property 'body' removed from Note" in str(woke.value)
    assert (s.cfg.state_dir / app_openapi.CACHE_FILE).exists() and s.data()["app_openapi"]["failed"] is None


# 7c
def test_baseline_that_cannot_be_built_pauses_after_two_checks(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch, work_files={**FILES, "svc/models.py": RENAMED})
    s.builds.fail_always["base"] = True
    outs = [s.edit("svc/models.py"), s.edit("tools/script.py", "X = 2\n"), s.edit("svc/models.py", RENAMED + "#\n"),
            s.edit("tools/script.py", "X = 3\n")]
    assert s.builds.count("base") == 4 and s.builds.count("work") == 2  # the first two checks only
    texts = [_ctx(o) for o in outs]
    assert "could not be built in the baseline" in texts[0] and "the baseline exploded" in texts[0]
    assert sum("is paused for this session" in t for t in texts) == 1 and "is paused" in texts[1]
    data = s.data()
    assert data["app_openapi"]["failed"] == {"attempts": 2, "detail": "the app did not import: RuntimeError: the baseline exploded",
                                             "paused": True, "one_sha": None}
    assert data["wakeups"] == 0 and not _oas(data)

    with ledger.locked(s.cfg.state_dir) as d:  # existing findings stay as they are under a paused check
        ledger.add_finding(d, kind="contract_break", status=ledger.FAILURE, source="app-openapi", subject="x", file="svc/app.py",
                           evidence="e", item="app-oas:a", code_hash=symbols.sha("app-oas:a"))
        ledger.add_finding(d, kind="contract_break", status=ledger.RISK, source="app-openapi", subject="y", file="svc/app.py",
                           evidence="e", item="app-oas:b", code_hash=symbols.sha("app-oas:b"), state="recorded")
    calls = len(s.builds.calls)
    data, _ = s.audit()
    assert len(s.builds.calls) == calls
    assert [(f["item"], f["state"]) for f in _oas(data)] == [("app-oas:a", "open"), ("app-oas:b", "recorded")]
    assert "app OpenAPI: paused (the app did not import" in contract_tools.report_line(s.cfg, data)

    s.cfg.raw["behavioral_diff"]["io"] = "block"  # a new cache key: the check runs again
    s.edit("svc/models.py", RENAMED + "# io\n")
    assert s.builds.count("base") == 6


def test_same_single_build_twice_is_cached_with_stable_none(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch, work_files={**FILES, "svc/models.py": RENAMED})
    s.builds.plan["base"] = ["ok", "fail", "fail", "ok", "fail", "fail"]
    s.edit("svc/models.py")
    assert s.data()["app_openapi"]["failed"]["attempts"] == 1
    out = s.edit("svc/models.py", RENAMED + "#\n")
    assert s.builds.count("base") == 6
    data = s.data()
    assert data["app_openapi"]["failed"] is None and data["app_openapi"]["stable"] is None
    assert app_openapi.HEDGE_ONCE in _ctx(out) and data["wakeups"] == 0
    out = s.edit("svc/models.py", RENAMED + "##\n")
    assert s.builds.count("base") == 6 and app_openapi.HEDGE_ONCE in _ctx(out)  # cached: every later break hedged
    data, blocking = s.audit()
    (f,) = _oas(data)
    assert f["status"] == ledger.RISK and f["state"] == "recorded" and f["hedged"] is True and not blocking


# 11b
def _outcome(breaks=(), hedged=(), state="checked"):
    out = app_openapi.SpecOutcome(state, via="oasdiff", app="svc.app:app")
    for item, subject, hedge in hedged:
        out.hedged.append(("svc/app.py", app_openapi.SpecBreak("http", item, subject, "app-openapi", "svc/app.py", hedge=hedge)))
    for item, subject in breaks:
        out.breaks.append(("svc/app.py", app_openapi.SpecBreak("http", item, subject, "app-openapi", "svc/app.py")))
    return out


def _stop(cfg, data, oas):
    audit._contract_findings(cfg, data, [], oas=oas)
    return [f for f in data["findings"] if f["status"] in audit.BLOCKING_STATUS and f["state"] == "open"
            and f["id"] not in data["stop_blocked_for"]]


def test_hedged_finding_lifecycle(tmp_path, make_cfg):
    cfg = make_cfg(tmp_path, app="svc.app:app")
    data = ledger.fresh("s")
    hedged = _outcome(hedged=[("app-oas:x", "POST /notes: property 'body' removed from Note", "H")])
    assert _stop(cfg, data, hedged) == []
    (f,) = _oas(data)
    assert (f["status"], f["state"], f["hedged"]) == (ledger.RISK, "recorded", True) and f["evidence"].endswith("; H")
    _stop(cfg, data, _outcome(state="not_checked"))
    assert f["state"] == "recorded"
    _stop(cfg, data, None)  # no changed file applies any more (the rename was reverted)
    assert f["state"] == "not_applied"


# 11c
def test_status_follows_the_hedge(tmp_path, make_cfg):
    cfg = make_cfg(tmp_path, app="svc.app:app")
    data = ledger.fresh("s")
    item, subject = "app-oas:x", "POST /notes: property 'body' removed from Note"
    _stop(cfg, data, _outcome(hedged=[(item, subject, "H")]))
    (f,) = _oas(data)
    assert f["evidence"].endswith("(oasdiff); H")
    blocking = _stop(cfg, data, _outcome(breaks=[(item, subject)]))
    assert (f["status"], f["state"]) == (ledger.FAILURE, "open") and [b["id"] for b in blocking] == [f["id"]]
    assert f["evidence"] == (f"{subject}; found by comparing the OpenAPI spec the app generates in the baseline and now "
                             "(oasdiff)") and f["hedged"] is False
    data["stop_blocked_for"].append(f["id"])
    blocking = _stop(cfg, data, _outcome(hedged=[(item, subject, "H2")]))
    assert (f["status"], f["state"]) == (ledger.RISK, "recorded") and blocking == []
    assert f["evidence"].endswith("(oasdiff); H2") and f["hedged"] is True

    f["state"] = "waived"
    _stop(cfg, data, _outcome(breaks=[(item, subject)]))
    assert f["state"] == "waived"
    _stop(cfg, data, _outcome(hedged=[(item, subject, "H")]))
    assert f["state"] == "waived"


# 14
def test_static_and_code_first_dedupe(tmp_path, make_cfg, monkeypatch):
    from guardlib import checks
    cfg = make_cfg(tmp_path, app="svc.app:app", contracts={"http": "openapi.yaml"})
    same = {"id": "response-property-removed", "operation": "POST", "path": "/notes", "text": "property 'body' removed",
            "fingerprint": "f1"}
    other = dict(same, text="property 'n' removed", fingerprint="f2")
    s_item, s_subject = contract_tools.break_fields("openapi.yaml", same)
    static = Break("http", s_item, s_subject, "oasdiff", "openapi.yaml")
    monkeypatch.setattr(checks, "run_checks", lambda cfg_, rel, *a, **k: [static] if rel == "openapi.yaml" else [])
    oas = app_openapi.SpecOutcome("checked", via="oasdiff", app="svc.app:app")
    for c in (same, other):
        item, subject = contract_tools.break_fields(app_openapi.REL, dict(c, fingerprint="g" + c["fingerprint"]))
        oas.breaks.append(("svc/app.py", app_openapi.SpecBreak("http", "app-oas:" + item, subject, "app-openapi", "svc/app.py",
                                                               change=(c["id"], c["operation"], c["path"], c["text"]))))
    data = ledger.fresh("s")
    audit._contract_findings(cfg, data, ["openapi.yaml"], oas=oas)
    static_f = next(f for f in data["findings"] if f.get("source") == "oasdiff")
    assert static_f["evidence"].endswith("; also in the app's generated spec")
    assert [f["subject"] for f in _oas(data)] == ["POST /notes: property 'n' removed"]
    audit._contract_findings(cfg, data, ["openapi.yaml"], oas=oas)
    assert static_f["evidence"].count("also in the app's generated spec") == 1

    out = ci.Outcome(base="c1", breaks=[("openapi.yaml", static)])
    monkeypatch.setattr(app_openapi, "compute", lambda *a, **k: oas)
    ci._app_openapi(cfg, out, str(tmp_path), str(tmp_path), ["svc/models.py"])
    assert [b.subject for _, b in out.breaks] == [s_subject, "POST /notes: property 'n' removed"]


# 18
def test_project_code_never_runs_under_the_ledger_lock(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch, work_files={**FILES, "svc/models.py": RENAMED}, lock_check=True)
    with pytest.raises(Exception):
        s.edit("svc/models.py")
    assert len(s.builds.calls) == 3


# 20
def test_overlapping_hooks_wake_once(tmp_path, make_cfg, monkeypatch):
    import _common
    s = Session(tmp_path, make_cfg, monkeypatch, work_files={**FILES, "svc/models.py": RENAMED})
    barrier = threading.Barrier(2, timeout=20)
    canned = _outcome(breaks=[("app-oas:x", "POST /notes: property 'body' removed from Note")])

    def compute(*a, **k):
        barrier.wait()  # both results are ready before either hook stores
        return canned
    monkeypatch.setattr(app_openapi, "compute", compute)
    results = []

    def run():
        try:
            results.append(("ok", s.edit("svc/models.py")))
        except _common.Wake as exc:
            results.append(("wake", str(exc)))
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    kinds = sorted(k for k, _ in results)
    assert kinds == ["ok", "wake"]
    ok = next(v for k, v in results if k == "ok")
    assert "as reported earlier: POST /notes: property 'body' removed from Note" in _ctx(ok)
    data = s.data()
    assert data["app_openapi_seen"] == ["app-oas:x"] and data["wakeups"] == 1


# 21
def test_spec_stage_deadline(tmp_path, make_cfg, monkeypatch):
    s = Session(tmp_path, make_cfg, monkeypatch, work_files={**FILES, "svc/models.py": RENAMED})
    monkeypatch.setattr(app_openapi, "SPEC_BUDGET_S", 2)

    def slow(cfg, tree, timeout):
        time.sleep(4)
        return canned_build(tree, "svc.app:app")
    monkeypatch.setattr(app_openapi, "run_tree", slow)
    started = time.perf_counter()
    out = app_openapi.compute(s.cfg, str(s.base), str(s.root), None, {"baseline_commit": "c1"})
    took = time.perf_counter() - started
    assert took < 3
    assert out.state == "not_checked" and "time budget" in out.note and out.cache_write is None and out.summary is None
    assert out.breaks == [] and out.hedged == []
    assert out.failed == {"detail": app_openapi.BUDGET, "one_sha": None}  # counts as one failed attempt (D10)


def test_noise_list_hedges_only_the_shared_operation(tmp_path, make_cfg, monkeypatch):
    """The noise filter on canned builds: a WARN old-vs-old change at POST /notes hedges the break there; a break at
    another operation stands; a change old-vs-old also reports is dropped. Computed once and cached."""
    other = "\n\nclass Other(BaseModel):\n    x: int\n    y: int\n"
    routes = '\n\n@app.get("/other")\ndef other() -> Other:\n    return None\n'
    base_files = {**FILES, "svc/models.py": MODELS + other, "svc/app.py": APP.replace("import Note", "import Note, Other") + routes}
    work_files = {**base_files, "svc/models.py": RENAMED + other.replace("    y: int\n", "")}
    s = Session(tmp_path, make_cfg, monkeypatch, base_files=base_files, work_files=work_files, mode="diff-loud")
    a = canned_build(s.base, "svc.app:app")
    spec = json.loads(a["text"])
    spec["components"]["schemas"]["Note"]["properties"]["n"]["default"] = "1"  # the second build differs at POST /notes
    b_text = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    b = dict(a, text=b_text, sha=hashlib.sha256(b_text.encode()).hexdigest()[:16])
    s.builds.plan["base"] = [a, b]
    with pytest.raises(Exception) as woke:
        s.edit("svc/models.py")
    text = str(woke.value)
    assert "GET /other: property 'y' removed from Other" in text
    assert "property 'body' removed from Note" not in text.split("\n\n")[0]
    assert "also changes between two builds at POST /notes" in text
    cache = json.loads((s.cfg.state_dir / app_openapi.CACHE_FILE).read_text())
    assert cache["stable"] is False and cache["noise_pending"] is False
    assert [c["id"] for c in cache["noise"]["changes"]] == ["fake-default-changed"]
    noise_calls = len(fake.calls(tmp_path))
    s.edit("svc/models.py", s.root.joinpath("svc/models.py").read_text() + "# c\n")
    assert len(fake.calls(tmp_path)) == noise_calls + 1  # old-vs-new only; the noise list is cached


def test_wake_text_is_exact(tmp_path, make_cfg):
    out = _outcome(breaks=[("app-oas:x", "POST /notes: property 'body' removed from Note")])
    assert app_openapi.wake_text(out, ["app-oas:x"]) == (
        "LegacyGuard: the OpenAPI spec this app generates (app: svc.app:app, built in the baseline and in the edited code) "
        "changed in ways oasdiff reports as breaking: POST /notes: property 'body' removed from Note. Clients written "
        "against the old spec may fail. If the change was intended, say so; the developer can waive it with /legacyguard:waive.")
    out.via, out.parser_why = "parser", "missing"
    assert "in ways the built-in parser reports as breaking (oasdiff not available):" in app_openapi.wake_text(out, ["app-oas:x"])


def test_internal_only_refuses_file_refs_before_running(tmp_path, make_cfg, monkeypatch):
    cfg = make_cfg(tmp_path)
    bin_dir = fake.install(tmp_path, "diff")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    a = {"openapi": "3.1.0", "paths": {}, "components": {"schemas": {"X": {"$ref": "other.json#/X"}}}}
    res = contract_tools.oasdiff_breaking(cfg, app_openapi.REL, json.dumps(a), json.dumps(a), ("/", "/"), 5,
                                          specs=(a, a), internal_only=True, label=app_openapi.LABEL)
    assert isinstance(res, contract_tools.Refusal) and res.kind == "refused"
    assert "the app's generated spec refers to other.json#/X" in res.reason and app_openapi.REL not in res.reason
    assert fake.calls(tmp_path) == []
    b = {"openapi": "3.1.0", "paths": {}, "components": {"schemas": {"X": {"properties": {"a": {}}}}}}
    c = {"openapi": "3.1.0", "paths": {}, "components": {"schemas": {"X": {"properties": {}}}}}
    res = contract_tools.oasdiff_breaking(cfg, app_openapi.REL, json.dumps(b), json.dumps(c), ("/", "/"), 5,
                                          specs=(b, c), internal_only=True, label=app_openapi.LABEL)
    assert isinstance(res, contract_tools.OasResult) and [e["text"] for e in res.errs] == ["property 'a' removed from X"]
    (call,) = fake.calls(tmp_path)
    assert "--allow-external-refs=false" in call["argv"] and call["argv"][1].endswith(app_openapi.REL)


def test_waiver_on_a_code_first_break(tmp_path, make_cfg):
    cfg = make_cfg(tmp_path, app="svc.app:app")
    data = ledger.fresh("s")
    out = _outcome(breaks=[("app-oas:x", "POST /notes: property 'body' removed from Note")])
    _stop(cfg, data, out)
    (f,) = _oas(data)
    waivers.add(cfg, f, "intended")
    data2 = ledger.fresh("s")
    assert _stop(cfg, data2, out) == [] and _oas(data2)[0]["state"] == "waived"
