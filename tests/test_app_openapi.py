"""Sprint 7.6 Part B, target side: the app's generated OpenAPI spec (`runner/lg_openapi.py`) on real FastAPI apps
(tests 1-12, 15-17, 19 of the plan; fake oasdiff from tests/oasdiff_fake.py).

Skipped as a module when the target interpreter cannot import FastAPI (requirements-dev.txt has it)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import PLUGIN, TARGET_PY, write_tree

if subprocess.run([TARGET_PY, "-c", "import fastapi"], capture_output=True).returncode != 0:
    pytest.skip("FastAPI is not importable in the target interpreter", allow_module_level=True)

import oasdiff_fake as fake  # noqa: E402
from guardlib import app_openapi, audit, checks, ci, contract_tools, engine, ledger, symbols, waivers  # noqa: E402

MODELS = "from pydantic import BaseModel\n\n\nclass Note(BaseModel):\n    body: str\n    n: int = 0\n"
RENAMED = MODELS.replace("body: str", "text: str")
APP = ('from fastapi import FastAPI\n\nfrom svc.models import Note\n\napp = FastAPI(title="t", version="1")\n\n\n'
       '@app.post("/notes")\ndef create(note: Note) -> Note:\n    return note\n')
FILES = {"svc/__init__.py": "", "svc/models.py": MODELS, "svc/app.py": APP, "tools/script.py": "X = 1\n"}
# Import-time defaults that differ per process: the clock, and the process id so two builds never collide in time.
NOW = "import datetime\nimport os\n" + MODELS + ("    when: datetime.datetime = datetime.datetime.now()\n"
                                                "    pid: int = os.getpid()\n")
OTHER = "\n\nclass Other(BaseModel):\n    x: int\n    y: int\n"
OTHER_ROUTE = '\n\n@app.get("/other")\ndef other() -> Other:\n    return Other(x=1, y=2)\n'


def _listing(root: Path) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".legacyguard"]
        for n in filenames:
            st = os.stat(os.path.join(dirpath, n))
            out.append((os.path.relpath(os.path.join(dirpath, n), root), st.st_size, st.st_mtime_ns))
    return sorted(out)


def _no_oasdiff(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")


class Project:
    def __init__(self, tmp_path, make_cfg, monkeypatch, base=None, work=None, mode="diff", on_path=True, app="svc.app:app",
                 **overrides):
        base = dict(FILES if base is None else base)
        self.root = write_tree(tmp_path / "proj", dict(base if work is None else work))
        self.cfg = make_cfg(self.root, app=app, **overrides)
        self.base = write_tree(self.cfg.state_dir / "baseline", base)
        with ledger.locked(self.cfg.state_dir) as data:
            data.update(baseline_ok=True, baseline_commit="c1", session_id="s")
        self.tmp = tmp_path
        bin_dir = fake.install(tmp_path, mode)
        if on_path:
            monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
        else:
            _no_oasdiff(monkeypatch)
        monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
        import post_edit
        monkeypatch.setattr(post_edit, "load_guard", lambda event, fp=None: (str(self.root), self.cfg))
        self.post_edit = post_edit
        self.monkeypatch = monkeypatch

    def edit(self, rel, text=None):
        if text is not None:
            write_tree(self.root, {rel: text})
        return self.post_edit.handle({"tool_input": {"file_path": str(self.root / rel)}, "cwd": str(self.root)})

    def data(self):
        return ledger.load(self.cfg.state_dir)

    def changed(self):
        out = []
        for p in sorted(self.root.rglob("*.py")):
            rel = p.relative_to(self.root).as_posix()
            b = self.base / rel
            if not b.exists() or b.read_text() != p.read_text():
                out.append(rel)
        return out

    def audit(self):
        self.monkeypatch.setattr(audit.baseline, "changed_files", lambda cfg: self.changed())
        with ledger.locked(self.cfg.state_dir) as data:
            blocking = audit.run(self.cfg, data)
        return ledger.load(self.cfg.state_dir), blocking


def _wake():
    import _common  # on sys.path once a Project has prepended the scripts folder
    return _common.Wake


def _ctx(out) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def _oas(data):
    return [f for f in data["findings"] if f["kind"] == "contract_break" and f.get("source") == "app-openapi"]


def _spy(monkeypatch):
    calls = []
    real = app_openapi.run_tree

    def spy(cfg, tree, timeout):
        calls.append(os.path.realpath(tree))
        return real(cfg, tree, timeout)
    monkeypatch.setattr(app_openapi, "run_tree", spy)
    return calls


# 1
def test_run_tree_builds_the_spec_and_leaves_the_tree_alone(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch)
    before = _listing(p.root)
    first = app_openapi.run_tree(p.cfg, str(p.root), 60)
    assert app_openapi.complete(first), first
    assert "/notes" in json.loads(first["text"])["paths"] and first["kind"] == "fastapi" and first["blocks"] == []
    files = app_openapi._rel_files(first["files"], str(p.root))
    assert "svc/models.py" in files and "tools/script.py" not in files
    assert app_openapi._rel_files([first["app_file"]], str(p.root)) == ["svc/app.py"]
    assert first["versions"]["fastapi"] and first["versions"]["pydantic"]
    second = app_openapi.run_tree(p.cfg, str(p.root), 60)
    assert second["sha"] == first["sha"] and second["text"] == first["text"]
    assert _listing(p.root) == before


# 2
def test_hash_seed_is_fixed(tmp_path, make_cfg, monkeypatch):
    multi = APP + '\n\n@app.api_route("/multi", methods=["GET", "POST", "PUT"])\ndef multi() -> Note:\n    return Note(body="x")\n'
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/app.py": multi})
    texts = {app_openapi.run_tree(p.cfg, str(p.root), 60)["text"] for _ in range(2)}
    assert len(texts) == 1


# 2b
def test_random_hash_seed_never_gives_a_break(tmp_path, make_cfg, monkeypatch):
    multi = APP + '\n\n@app.api_route("/multi", methods=["GET", "POST"])\ndef multi(note: Note) -> Note:\n    return note\n'
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/app.py": multi},
                behavioral_diff={"env": {"PYTHONHASHSEED": "random"}})
    for _ in range(3):
        out = app_openapi.compute(p.cfg, str(p.base), str(p.root), None, {"baseline_commit": "c1"})
        assert out.state == "checked", out.notes
        assert out.breaks == [] and out.hedged == []


# 2c
def test_methods_reorder_is_not_a_break(tmp_path, make_cfg, monkeypatch):
    multi = APP + '\n\n@app.api_route("/multi", methods=["GET", "POST"])\ndef multi(note: Note) -> Note:\n    return note\n'
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/app.py": multi})
    out = p.edit("svc/app.py", multi.replace('["GET", "POST"]', '["POST", "GET"]'))
    assert "breaking" not in _ctx(out)
    assert p.data()["wakeups"] == 0


# 3
def test_model_rename_wakes_once_with_no_function_targets(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch)
    write_tree(p.root, {"svc/models.py": RENAMED})
    with ledger.locked(p.cfg.state_dir) as data:
        assert engine.targets_for(p.cfg, "svc/models.py", data) == []
    with pytest.raises(_wake()) as woke:
        p.edit("svc/models.py")
    text = str(woke.value)
    assert "property 'body' removed from Note" in text and "app: svc.app:app" in text
    assert text.startswith("LegacyGuard: the OpenAPI spec this app generates (app: svc.app:app, built in the baseline "
                           "and in the edited code) changed in ways oasdiff reports as breaking: ")
    assert app_openapi.REL not in text.replace("openapi-from-app", "")  # never the temp file name
    data = p.data()
    assert data["wakeups"] == 1 and len(data["app_openapi_seen"]) == 1 and data["app_openapi_seen"][0].startswith("app-oas:")
    (call,) = fake.calls(tmp_path)
    assert "--allow-external-refs=false" in call["argv"]
    assert [a for a in call["argv"] if a.endswith("openapi-from-app.json")].__len__() == 2
    out = p.edit("svc/models.py", RENAMED + "# a comment\n")
    assert "as reported earlier" in _ctx(out)
    assert p.edit("tools/script.py", "X = 2\n") is None
    assert p.data()["wakeups"] == 1


# 4
def test_byte_equal_specs_run_no_tool(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch)
    assert p.edit("svc/models.py", MODELS + "# 1\n") is None  # builds the cache
    assert p.edit("svc/models.py", MODELS + "# 2\n") is None
    assert fake.calls(tmp_path) == []
    assert p.data()["app_openapi"]["stable"] is True


# 5a
def test_import_time_default_noise_does_not_hide_the_rename(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/models.py": NOW},
                work={**FILES, "svc/models.py": NOW.replace("body: str", "text: str")})
    with pytest.raises(_wake()) as woke:
        p.edit("svc/models.py")
    assert "property 'body' removed from Note" in str(woke.value)
    cache = json.loads((p.cfg.state_dir / app_openapi.CACHE_FILE).read_text())
    assert cache["stable"] is False and cache["noise"] == {"changes": [], "via": "oasdiff"}
    data, blocking = p.audit()
    (f,) = _oas(data)
    assert f["status"] == ledger.FAILURE and f["state"] == "open" and [b["id"] for b in blocking] == [f["id"]]


# 5b
def test_noise_at_the_same_operation_hedges(tmp_path, make_cfg, monkeypatch):
    models = NOW + OTHER
    app = APP.replace("import Note", "import Note, Other") + OTHER_ROUTE
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/models.py": models, "svc/app.py": app}, mode="diff-loud")
    out = p.edit("svc/models.py", models.replace("body: str", "text: str"))
    ctx = _ctx(out)
    assert "property 'body' removed from Note" in ctx and "also changes between two builds at POST /notes" in ctx
    assert p.data()["wakeups"] == 0
    with pytest.raises(_wake()) as woke:
        p.edit("svc/models.py", models.replace("body: str", "text: str").replace("    y: int\n", ""))
    first = str(woke.value).split("\n\n")[0]
    assert "GET /other: property 'y' removed from Other" in first and "Note" not in first
    data, blocking = p.audit()
    by = {f["subject"]: f for f in _oas(data)}
    note = next(f for s, f in by.items() if "'body'" in s)
    other = next(f for s, f in by.items() if "'y'" in s)
    assert (note["status"], note["state"], note["hedged"]) == (ledger.RISK, "recorded", True)
    assert (other["status"], other["state"]) == (ledger.FAILURE, "open") and [b["id"] for b in blocking] == [other["id"]]


# 5c
def test_noise_run_once_per_session(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/models.py": NOW})
    for i in range(3):
        p.edit("svc/models.py", NOW + f"# {i}\n")
    # check 1: old-A vs old-B (the noise list) and old vs new; checks 2 and 3: old vs new only
    assert len(fake.calls(tmp_path)) == 4
    assert json.loads((p.cfg.state_dir / app_openapi.CACHE_FILE).read_text())["noise_pending"] is False


# 6
def test_parser_fallback_without_oasdiff(tmp_path, make_cfg, monkeypatch):
    spec = "openapi: 3.0.0\ninfo: {title: t, version: '1'}\npaths:\n  /a:\n    get:\n      responses: {'200': {description: ok}}\n"
    p = Project(tmp_path, make_cfg, monkeypatch, on_path=False, base={**FILES, "openapi.yaml": spec},
                contracts={"http": "openapi.yaml"})
    with ledger.locked(p.cfg.state_dir) as data:  # the static check says "oasdiff was not found" first
        notes = []
        checks.run_checks(p.cfg, "openapi.yaml", spec, spec.replace("/a:", "/b:"), {}, trees=(str(p.root), str(p.root)), notes=notes)
        shown = contract_tools.take_notes(data, notes)
    assert any("oasdiff was not found" in t for t in shown)
    with pytest.raises(_wake()) as woke:
        p.edit("svc/models.py", RENAMED)
    text = str(woke.value)
    assert "response field dropped: Note.body" in text and "the built-in parser reports as breaking (oasdiff not available)" in text
    assert "oasdiff was not found" not in text  # once per session across the static and code-first checks
    assert p.data()["tool_notes"].count("oasdiff") == 1
    data, _ = p.audit()
    (f,) = _oas(data)
    assert f["item"] == "app-oas-parser:field:Note.body" and f["source"] == "app-openapi" and f["file"] == "svc/app.py"


def test_parser_fallback_names_renamed_schemas(tmp_path, make_cfg, monkeypatch):
    other = "from pydantic import BaseModel\n\n\nclass Note(BaseModel):\n    title: str\n"
    app = APP.replace("from svc.models import Note\n", "from svc.models import Note\nfrom svc.other import Note as OtherNote\n") \
        + '\n\n@app.post("/other")\ndef make_other(note: OtherNote) -> OtherNote:\n    return note\n'
    p = Project(tmp_path, make_cfg, monkeypatch, on_path=False)
    write_tree(p.root, {"svc/other.py": other})
    out = p.edit("svc/app.py", app)
    ctx = _ctx(out)
    assert "dropped" not in ctx
    assert "did not compare the fields of 1 schemas whose names changed in the generated spec (Note -> svc__models__Note" in ctx


# 7
def test_baseline_that_does_not_import(tmp_path, make_cfg, monkeypatch):
    bad = {**FILES, "svc/models.py": MODELS + "raise RuntimeError('baseline models exploded')\n"}
    p = Project(tmp_path, make_cfg, monkeypatch, base=bad, work={**FILES, "svc/models.py": RENAMED})
    out = app_openapi.compute(p.cfg, str(p.base), str(p.root), None, {"baseline_commit": "c1"})
    assert out.state == "not_checked" and out.breaks == [] and out.cache_write is None
    assert "could not be built in the baseline" in out.note and "baseline models exploded" in out.note
    with ledger.locked(p.cfg.state_dir) as data:
        ledger.add_finding(data, kind="contract_break", status=ledger.FAILURE, source="app-openapi", subject="s",
                           file="svc/app.py", evidence="e", item="app-oas:x", code_hash=symbols.sha("app-oas:x"))
    ctx = _ctx(p.edit("svc/models.py"))
    assert "could not be built in the baseline" in ctx and p.data()["wakeups"] == 0
    data, _ = p.audit()
    assert [(f["item"], f["state"]) for f in _oas(data)] == [("app-oas:x", "open")]


# 8
def test_working_tree_that_does_not_import(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch, work={**FILES, "svc/models.py": RENAMED + "raise RuntimeError('edit broke it')\n"})
    out = app_openapi.compute(p.cfg, str(p.base), str(p.root), None, {"baseline_commit": "c1"})
    assert out.state == "not_checked" and "could not be built in the edited code" in out.note and "edit broke it" in out.note
    assert out.cache_write is not None  # the baseline was built and is cached
    ctx = _ctx(p.edit("svc/models.py"))
    assert "in the edited code" in ctx and p.data()["wakeups"] == 0


# 9
def test_flask_app_costs_one_run_and_no_note(tmp_path, make_cfg, monkeypatch):
    if subprocess.run([TARGET_PY, "-c", "import flask"], capture_output=True).returncode != 0:
        pytest.skip("Flask is not importable in the target interpreter")
    flask_app = "from flask import Flask\n\napp = Flask(__name__)\n\n\n@app.get('/x')\ndef x():\n    return {}\n"
    p = Project(tmp_path, make_cfg, monkeypatch, base={"svc/__init__.py": "", "svc/app.py": flask_app, "svc/util.py": "Y = 1\n"})
    calls = _spy(monkeypatch)
    assert p.edit("svc/util.py", "Y = 2\n") is None
    assert len(calls) == 3
    data = p.data()
    assert data["app_openapi"]["kind"] == "none" and not any(k.startswith("app-openapi") for k in data["tool_notes"])
    p.edit("svc/app.py", flask_app + "# c\n")
    assert len(calls) == 3 and fake.calls(tmp_path) == []
    assert "app OpenAPI: not available for this app (no openapi() method)" in contract_tools.report_line(p.cfg, data)


# 10c, 11 and 12 use a git repository
def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_repo(tmp_path, files, config: str) -> Path:
    root = write_tree(tmp_path / "proj", files)
    (root / ".gitignore").write_text(".legacyguard/\n")
    (root / ".legacyguard").mkdir()
    (root / ".legacyguard/config.yaml").write_text(config)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return root


def _fake_on_path(tmp_path, monkeypatch, mode="diff"):
    bin_dir = fake.install(tmp_path, mode)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


# 10c
def test_ci_top_level_module_app(tmp_path, monkeypatch):
    _fake_on_path(tmp_path, monkeypatch)
    files = {"models.py": MODELS, "main.py": APP.replace("from svc.models", "from models")}
    root = _git_repo(tmp_path, files, "app: main:app\n")
    (root / "models.py").write_text(RENAMED)
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    oas = [(f, b) for f, b in out.breaks if b.source == "app-openapi"]
    assert [(f, b.subject) for f, b in oas] == [("main.py", "POST /notes: property 'body' removed from Note")]


# 12
def test_ci_reports_breaks_and_honours_waivers(tmp_path, monkeypatch):
    _fake_on_path(tmp_path, monkeypatch)
    root = _git_repo(tmp_path, FILES, "app: svc.app:app\n")
    (root / "svc/models.py").write_text(RENAMED)
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    oas = [(f, b) for f, b in out.breaks if b.source == "app-openapi"]
    assert [f for f, _ in oas] == ["svc/app.py"] and isinstance(oas[0][1], checks.Break)
    assert any(a.startswith("::warning file=svc/app.py,title=LegacyGuard contract::POST /notes: property 'body' removed")
               for a in ci.annotations(cfg, out))
    assert ci.exit_code(cfg, out, "difference") == 0 and ci.exit_code(cfg, out, "any") == 1  # M4
    item = oas[0][1].item
    (root / ".legacyguard/waivers.json").write_text(json.dumps([{"kind": "contract_break", "file": "svc/app.py", "item": item,
                                                                 "code_hash": symbols.sha(item)}]))
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    assert not [b for _, b in out.breaks if b.source == "app-openapi"]
    (root / ".legacyguard/waivers.json").unlink()
    monkeypatch.setattr(app_openapi, "run_tree", lambda *a, **k: None)
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    assert not [b for _, b in out.breaks if b.source == "app-openapi"]
    assert any("could not be built in the baseline" in n for n in out.notes)


# 11
def test_stop_audit_findings_lifecycle(tmp_path, make_cfg, monkeypatch):
    from guardlib import baseline
    _fake_on_path(tmp_path, monkeypatch)
    root = _git_repo(tmp_path, FILES, "app: svc.app:app\n")
    cfg = make_cfg(root, app="svc.app:app")
    info = baseline.create(cfg)
    with ledger.locked(cfg.state_dir) as data:
        data.update(baseline_ok=True, baseline_commit=info["commit"], session_id="s")

    def run_audit():
        with ledger.locked(cfg.state_dir) as data:
            blocking = audit.run(cfg, data)
            if blocking:
                data["stop_blocked_for"] += [f["id"] for f in blocking]
        return ledger.load(cfg.state_dir), blocking

    (root / "svc/models.py").write_text(RENAMED)
    data, blocking = run_audit()
    (f,) = _oas(data)
    assert f["status"] == ledger.FAILURE and f["state"] == "open" and f["file"] == "svc/app.py"
    assert f["code_hash"] == symbols.sha(f["item"]) and f["contract_kind"] == "http"
    assert "found by comparing the OpenAPI spec the app generates in the baseline and now (oasdiff)" in f["evidence"]
    assert [b["id"] for b in blocking] == [f["id"]]  # blocks once (M1)
    assert "app OpenAPI: FastAPI" in contract_tools.report_line(cfg, data)
    data, blocking = run_audit()
    assert [x["state"] for x in _oas(data)] == ["open"] and blocking == []

    with monkeypatch.context() as m:
        m.setattr(app_openapi, "run_tree", lambda *a, **k: None)
        (cfg.state_dir / app_openapi.CACHE_FILE).unlink()
        data, _ = run_audit()
        assert [x["state"] for x in _oas(data)] == ["open"]

    (root / "svc/models.py").write_text(MODELS)
    data, _ = run_audit()
    assert [x["state"] for x in _oas(data)] == ["not_applied"]

    (root / "svc/models.py").write_text(RENAMED)
    data, _ = run_audit()
    (f,) = _oas(data)
    assert f["state"] == "open"
    waivers.add(cfg, f, "intended rename")
    with ledger.locked(cfg.state_dir) as d:
        d["findings"] = []
    (root / "tools/script.py").write_text("X = 2\n")
    data, _ = run_audit()
    assert [x["state"] for x in _oas(data)] == ["waived"]


# 15
def test_external_ref_in_a_replaced_openapi(tmp_path, make_cfg, monkeypatch):
    custom = APP + ('\n\ndef custom():\n    props = {f: {"type": "string"} for f in Note.model_fields}\n'
                    '    return {"openapi": "3.1.0", "info": {"title": "t", "version": "1"},\n'
                    '            "paths": {"/notes": {"post": {"responses": {"200": {"description": "ok"}}}}},\n'
                    '            "components": {"schemas": {"Note": {"properties": props},\n'
                    '                                       "Ext": {"$ref": "other.json#/X"}}}}\n\n\napp.openapi = custom\n')
    p = Project(tmp_path, make_cfg, monkeypatch, base={**FILES, "svc/app.py": custom}, work={**FILES, "svc/app.py": custom,
                                                                                            "svc/models.py": RENAMED})
    out = app_openapi.compute(p.cfg, str(p.base), str(p.root), None, {"baseline_commit": "c1"})
    assert fake.calls(tmp_path) == []
    assert out.via == "parser" and [b.subject for _, b in out.breaks] == ["response field dropped: Note.body"]
    refused = [t for k, t in out.notes if k == "oasdiff-refused"]
    assert refused and "the app's generated spec refers to other.json#/X" in refused[0] and app_openapi.REL not in refused[0]


# 16
def test_encoding_of_replaced_openapi_results(tmp_path, make_cfg, monkeypatch):
    def app_with(value: str, keys: str = '"200"') -> str:
        return ("from decimal import Decimal\nfrom fastapi import FastAPI\n\napp = FastAPI()\n\n\ndef custom():\n"
                f'    return {{"openapi": "3.1.0", "info": {{"title": "t", "version": "1"}}, "paths": {{"/a": {{"get": '
                f'{{"responses": {{{keys}: {{"description": "ok"}}}}, "x-value": {value}}}}}}}}}\n\n\napp.openapi = custom\n')
    p = Project(tmp_path, make_cfg, monkeypatch, base={"svc/__init__.py": "", "svc/app.py": app_with('Decimal("1.5")')})
    res = app_openapi.run_tree(p.cfg, str(p.root), 60)
    assert app_openapi.complete(res) and json.loads(res["text"])["paths"]["/a"]["get"]["x-value"] == 1.5
    write_tree(p.root, {"svc/app.py": app_with('float("nan")')})
    out = app_openapi.compute(p.cfg, str(p.base), str(p.root), None, {"baseline_commit": "c1"})
    assert out.state == "not_checked" and "encode" in out.note and "in the edited code" in out.note
    write_tree(p.root, {"svc/app.py": app_with("1", keys="200")})
    res = app_openapi.run_tree(p.cfg, str(p.root), 60)
    assert app_openapi.complete(res) and "200" in json.loads(res["text"])["paths"]["/a"]["get"]["responses"]


# 17
def test_hanging_import_times_out(tmp_path, make_cfg, monkeypatch):
    import tempfile
    piddir = tempfile.mkdtemp(prefix="lgpids")  # the temp folder is writable for the worker at import
    try:
        hang = {**FILES, "svc/models.py": (f"import os\nopen(os.path.join({piddir!r}, str(os.getpid())), 'w').write('x')\n"
                                           "while True:\n    pass\n")}
        p = Project(tmp_path, make_cfg, monkeypatch, base=hang, contract_tools={"timeout_s": 2})
        started = time.perf_counter()
        out = app_openapi.compute(p.cfg, str(p.base), str(p.root), None, {"baseline_commit": "c1"})
        took = time.perf_counter() - started
        assert took < 5
        assert out.state == "not_checked" and "timed out" in out.note and app_openapi.REL not in out.note
        assert "lgoas" not in out.note and str(tmp_path) not in out.note
        # Every lg_openapi process it started is gone (under bubblewrap the timed-out Python used to outlive bwrap).
        pids = [int(n) for n in os.listdir(piddir)]
        assert len(pids) == 3  # base A, base B and the working tree all reached the loop
        end, left = time.time() + 1.0, pids
        while left and time.time() < end:
            left = [x for x in left if _alive(x)]
            time.sleep(0.05)
        for x in left:
            try:
                os.kill(x, 9)
            except OSError:
                pass
        assert left == []
    finally:
        shutil.rmtree(piddir, ignore_errors=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


# 19
def test_timing(tmp_path, make_cfg, monkeypatch):
    p = Project(tmp_path, make_cfg, monkeypatch)
    for tree in (p.base, p.root):
        started = time.perf_counter()
        res = app_openapi.run_tree(p.cfg, str(tree), 60)
        took = time.perf_counter() - started
        print(f"app.openapi() spec build on {tree.name}: {took:.3f} s (inside {res['seconds']:.3f} s, {res['bytes']} bytes)")
        assert took < 10
    write_tree(p.root, {"svc/models.py": RENAMED})
    started = time.perf_counter()
    with pytest.raises(_wake()):
        p.edit("svc/models.py")
    first = time.perf_counter() - started
    started = time.perf_counter()
    p.edit("svc/models.py", RENAMED + "# c\n")
    later = time.perf_counter() - started
    print(f"PostToolUse with the spec check: first (baseline built) {first:.3f} s, later (cached) {later:.3f} s")
    assert first < 10 and later < 10
