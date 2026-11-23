"""Sprint 7.6 Part A: guardlib/contract_tools.py and oasdiff in the OpenAPI check, with a fake oasdiff (tests 1-10)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import oasdiff_fake as fake
from guardlib import checks, contract_tools, ledger
from runner import os_sandbox

from conftest import PLUGIN, write_tree

SPEC = """\
openapi: 3.0.0
info: {title: pets, version: "1"}
paths:
  /pets:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {$ref: "#/components/schemas/Pet"}
  /pets/{id}:
    get:
      parameters:
        - {name: id, in: path, required: true, schema: {type: string}}
      responses:
        "200": {description: ok}
components:
  schemas:
    Pet:
      type: object
      properties:
        name: {type: string}
"""
NO_PETS = SPEC.replace("""  /pets:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema: {$ref: "#/components/schemas/Pet"}
""", "")


def _project(tmp_path, make_cfg, monkeypatch, mode="ok", on_path=True, spec=SPEC, extra=None, **overrides):
    root = write_tree(tmp_path / "proj", {"openapi.yaml": spec, "app.py": "def f(x):\n    return x\n", **(extra or {})})
    cfg = make_cfg(root, contracts={"http": "openapi.yaml"}, **overrides)
    bin_dir = fake.install(tmp_path, mode)
    path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", (str(bin_dir) + os.pathsep + path) if on_path else path)
    return cfg, root


def _check(cfg, root, before=SPEC, after=NO_PETS, trees=None, notes=None, **kw):
    notes = [] if notes is None else notes
    out = checks.run_checks(cfg, "openapi.yaml", before, after, {}, trees=trees or (str(root), str(root)), notes=notes, **kw)
    return out, notes


def _texts(notes):
    return [t for _, t in notes]


# 1
def test_find_config_path_and_cache(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, contract_tools={"oasdiff": False})
    assert contract_tools.find("oasdiff", cfg) is None and contract_tools.find_reason("oasdiff", cfg) == "switched off"
    out, notes = _check(cfg, root)
    assert notes == [] and fake.calls(tmp_path) == [] and out[0].source == "schema"

    cfg.raw["contract_tools"]["oasdiff"] = "tools/missing-oasdiff"
    assert contract_tools.find("oasdiff", cfg) is None
    assert "tools/missing-oasdiff" in contract_tools.find_reason("oasdiff", cfg)
    out, notes = _check(cfg, root)
    assert "tools/missing-oasdiff" in _texts(notes)[0] and out[0].source == "schema"

    cfg.raw["contract_tools"]["oasdiff"] = "auto"
    tool = contract_tools.find("oasdiff", cfg)
    assert tool is not None and tool.source == "path" and tool.path == str(tmp_path / "bin" / "oasdiff")
    state = json.loads((cfg.state_dir / "tools.json").read_text())["oasdiff"]
    assert state["path"] == tool.path and state["config_value"] == "auto" and len(state["st"]) == 3

    def boom(*a, **k):
        raise AssertionError("cache not used")
    with monkeypatch.context() as m:
        m.setattr(contract_tools.shutil, "which", boom)
        assert contract_tools.find("oasdiff", cfg).path == tool.path  # cached: no PATH lookup
    data = json.loads((cfg.state_dir / "tools.json").read_text())
    data["oasdiff"]["pre_edit_timeouts"] = 1
    (cfg.state_dir / "tools.json").write_text(json.dumps(data))
    with (tmp_path / "bin" / "oasdiff").open("a") as fh:  # touching the binary (size and mtime) re-resolves
        fh.write("\n# touched\n")
    assert contract_tools.find("oasdiff", cfg).path == tool.path
    state2 = json.loads((cfg.state_dir / "tools.json").read_text())["oasdiff"]
    assert state2["st"] != state["st"] and state2["pre_edit_timeouts"] == 0


# 2
def test_run_timeout_and_missing_file(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, mode="slow")
    contract_tools.find("oasdiff", cfg)
    assert contract_tools.run([str(tmp_path / "bin" / "oasdiff"), "breaking", "a", "b"], 0.3, str(tmp_path), cfg=cfg, name="oasdiff") is None
    assert json.loads((cfg.state_dir / "tools.json").read_text())["oasdiff"]["last_error"].startswith("timeout")
    assert contract_tools.run([str(tmp_path / "nope")], 1, str(tmp_path)) is None


# 3
def test_err_break_warn_note_info_ignored(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    out, notes = _check(cfg, root)
    assert len(out) == 1
    b = out[0]
    assert b.kind == "http" and b.source == "oasdiff" and b.contract == "openapi.yaml"
    assert b.item == "oasdiff:api-path-removed-without-deprecation:GET /pets:80a8c624dc40"
    assert b.subject == "GET /pets: api path removed without deprecation"
    assert _texts(notes) == ["LegacyGuard: oasdiff also reports 1 possible breaking change it cannot confirm in openapi.yaml: "
                             "the response property 'name' became optional."]
    assert "endpoint added" not in " ".join(_texts(notes))

    fake.set_mode(tmp_path, "string-levels")
    out2, notes2 = _check(cfg, root)
    assert [x.item for x in out2] == [b.item] and _texts(notes2) == _texts(notes)

    fake.set_mode(tmp_path, "no-id")
    out3, _ = _check(cfg, root)
    assert len(out3) == 1 and out3[0].item.startswith("oasdiff:change:GET /pets:80a8c624dc40")


# 4
def test_argv_cwd_and_env(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    monkeypatch.setenv("OASDIFF_CONFIG", str(root / ".oasdiff.yaml"))
    _check(cfg, root)
    (call,) = fake.calls(tmp_path)
    argv = call["argv"]
    assert argv[0] == "breaking" and argv[1].endswith("/base/openapi.yaml") and argv[2].endswith("/rev/openapi.yaml")
    assert argv[3:5] == ["-f", "json"] and "--allow-external-refs=false" in argv
    assert not call["cwd"].startswith(str(root)) and os.path.basename(call["cwd"]).startswith("lg-oas-")
    assert call["env"] == []
    assert not os.path.exists(call["cwd"])  # the temp folder is removed


SIBLING = SPEC.replace('schema: {$ref: "#/components/schemas/Pet"}', "schema: {$ref: './schemas.yaml#/Pet'}")


# 5
def test_sibling_ref_runs_in_sandbox_with_right_side_files(tmp_path, make_cfg, monkeypatch):
    if not os_sandbox.available(sys.executable):
        pytest.skip("no OS sandbox here")
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, spec=SIBLING, extra={"schemas.yaml": "Pet: {type: object}\n# work\n"})
    base = write_tree(tmp_path / "base", {"openapi.yaml": SIBLING, "schemas.yaml": "Pet: {type: object}\n# base\n"})
    seen = {}
    real = contract_tools._run

    def spy(cmd, timeout, cwd, env):
        seen["cmd"] = cmd
        seen["base"] = (Path(cwd) / "base" / "schemas.yaml").read_text()
        seen["rev"] = (Path(cwd) / "rev" / "schemas.yaml").read_text()
        return real(cmd, timeout, cwd, env)
    monkeypatch.setattr(contract_tools, "_run", spy)
    out, notes = _check(cfg, root, before=SIBLING, after=SIBLING.replace("  /pets:\n    get:", "  /cats:\n    get:"),
                        trees=(str(base), str(root)))
    assert seen["base"].endswith("# base\n") and seen["rev"].endswith("# work\n")
    assert seen["cmd"][0] in ("sandbox-exec", "bwrap") and "--allow-external-refs=false" not in seen["cmd"]
    assert [b.source for b in out] == ["oasdiff"]

    monkeypatch.setattr(os_sandbox, "active", lambda *a, **k: False)
    seen.clear()
    out, notes = _check(cfg, root, before=SIBLING, after=SIBLING.replace("  /pets:\n    get:", "  /cats:\n    get:"),
                        trees=(str(base), str(root)))
    assert seen == {} and [b.source for b in out] == ["schema"]
    assert notes[0][0] == "oasdiff-refused" and "no OS sandbox is available here" in notes[0][1]


def _ref_spec(ref: str) -> str:
    return SPEC.replace('schema: {$ref: "#/components/schemas/Pet"}', "schema: {$ref: '" + ref + "'}")


# 6
@pytest.mark.parametrize("ref", ["https://example.com/x.yaml", "HTTP://EXAMPLE.COM/x.yaml", "file:///etc/x.yaml", "/etc/x.yaml",
                                 "../../outside.yaml", "link.yaml#/Pet", "nested.yaml#/Pet", "many0.yaml", "locked.yaml", "binary.yaml"])
def test_refused_refs_never_call_the_tool(tmp_path, make_cfg, monkeypatch, ref):
    extra = {"nested.yaml": "Pet: {$ref: 'https://evil.example/p.yaml'}\n"}
    extra.update({f"many{i}.yaml": f"x: {{$ref: 'many{i + 1}.yaml'}}\n" for i in range(5)})
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, spec=_ref_spec(ref), extra=extra)
    (tmp_path / "outside.yaml").write_text("Pet: {}\n")
    os.symlink(tmp_path / "outside.yaml", root / "link.yaml")
    (root / "locked.yaml").write_text("Pet: {}\n")
    (root / "binary.yaml").write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setattr(contract_tools, "MAX_REF_FILES", 3)
    if ref == "locked.yaml":
        os.chmod(root / "locked.yaml", 0)
        if os.access(root / "locked.yaml", os.R_OK):
            pytest.skip("running as a user who can read mode-000 files")
    try:
        out, notes = _check(cfg, root, before=_ref_spec(ref), after=_ref_spec(ref).replace("  /pets:\n    get:", "  /cats:\n    get:"))
    finally:
        os.chmod(root / "locked.yaml", 0o644)
    assert fake.calls(tmp_path) == []
    assert [b.source for b in out] == ["schema"] and out[0].subject == "route removed: GET /pets"
    assert notes and notes[0][0] == "oasdiff-refused", notes
    text = notes[0][1]
    named = {"nested.yaml#/Pet": "https://evil.example/p.yaml", "many0.yaml": "more than 3 files", "locked.yaml": "locked.yaml",
             "binary.yaml": "binary.yaml", "link.yaml#/Pet": "link.yaml"}.get(ref, ref)
    assert named in text, text


# 7
@pytest.mark.parametrize("mode,reason", [("fail", "could not load the spec: Error: failed to load revision spec"),
                                         ("garbage", "could not read"), ("object", "could not read")])
def test_failed_or_wrong_shape_falls_back(tmp_path, make_cfg, monkeypatch, mode, reason):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, mode=mode)
    out, notes = _check(cfg, root)
    assert len(fake.calls(tmp_path)) == 1
    assert [b.source for b in out] == ["schema"] and out[0].subject == "route removed: GET /pets"
    assert notes[0][0] == "oasdiff-failed" and reason in notes[0][1]


def test_parse_changes_shape_rules():
    assert contract_tools.parse_changes("[]") == ([], [])
    assert contract_tools.parse_changes('[{"text": "x"}]') is None  # level is the one field never omitted
    assert contract_tools.parse_changes('[{"level": 2}]') == ([], [{"id": "", "text": "a change oasdiff did not describe",
                                                                  "operation": "", "path": "", "fingerprint": ""}])
    assert contract_tools.parse_changes('[1, 2]') is None and contract_tools.parse_changes("null") is None


# 8 and 9 go through the hook itself
def _pre_edit(monkeypatch, cfg, root):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import pre_edit
    monkeypatch.setattr(pre_edit, "load_guard", lambda event, fp=None: (str(root), cfg))
    return pre_edit


def _event(root, rel, content):
    return {"tool_name": "Write", "tool_input": {"file_path": str(root / rel), "content": content}, "cwd": str(root), "session_id": "s"}


def test_missing_tool_note_once_per_session(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, on_path=False)
    real_which = contract_tools.shutil.which
    monkeypatch.setattr(contract_tools.shutil, "which", lambda name, *a, **k: None if name == "oasdiff" else real_which(name, *a, **k))
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    first = pre_edit.handle(_event(root, "openapi.yaml", NO_PETS))
    ctx = first["hookSpecificOutput"]["additionalContext"]
    assert "oasdiff was not found" in ctx and "route removed: GET /pets" in ctx
    second = pre_edit.handle(_event(root, "openapi.yaml", NO_PETS.replace("version: \"1\"", "version: \"2\"")))
    assert "oasdiff was not found" not in str(second)
    assert ledger.load(cfg.state_dir)["tool_notes"] == ["oasdiff"]


def test_other_files_and_new_contract_never_call_the_tool(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    pre_edit.handle(_event(root, "app.py", "def f(x):\n    return x + 1\n"))
    assert fake.calls(tmp_path) == []
    out, notes = _check(cfg, root, before="", after=SPEC)
    assert out == [] and notes == [] and fake.calls(tmp_path) == []
    out, notes = checks.run_checks(cfg, "openapi.yaml", None, SPEC, {}, trees=(str(root), str(root)), notes=[]), []
    assert out == [] and fake.calls(tmp_path) == []


# 10
def test_fail_open(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    out, notes = _check(cfg, root, after="paths: [unclosed\n  - : :")
    assert out == [] and fake.calls(tmp_path) == []

    def boom(*a, **k):
        raise RuntimeError("tool path exploded")
    monkeypatch.setattr(contract_tools, "oasdiff_breaking", boom)
    out, notes = _check(cfg, root)
    assert [b.subject for b in out] == ["route removed: GET /pets"]
    assert notes == [("oasdiff-failed", "LegacyGuard: oasdiff step failed: RuntimeError; the built-in parser was used.")]


def test_tools_none_is_the_parser_exactly(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    plain = checks.check_http_file("openapi.yaml", SPEC, NO_PETS)
    assert plain == checks.run_checks(cfg, "openapi.yaml", SPEC, NO_PETS, {})
    assert [b.subject for b in plain] == ["route removed: GET /pets"] and fake.calls(tmp_path) == []


# 14, 15: only where a real oasdiff is installed (none in CI, decision D4)
REAL = __import__("shutil").which("oasdiff")


@pytest.mark.skipif(REAL is None, reason="oasdiff is not installed")
def test_real_oasdiff_levels(tmp_path):
    import subprocess
    import time
    (tmp_path / "base.yaml").write_text(SPEC)
    (tmp_path / "gone.yaml").write_text(NO_PETS)
    optional = SPEC.replace("        - {name: id, in: path, required: true, schema: {type: string}}",
                            "        - {name: id, in: path, required: true, schema: {type: string}}\n"
                            "        - {name: q, in: query, required: false, schema: {type: string}}")
    (tmp_path / "optional.yaml").write_text(optional)
    version = subprocess.run([REAL, "--version"], capture_output=True, text=True).stdout.strip()
    started = time.perf_counter()
    gone = subprocess.run([REAL, "breaking", "base.yaml", "gone.yaml", "-f", "json", "--allow-external-refs=false"],
                          capture_output=True, text=True, cwd=tmp_path)
    print(f"{version}: {time.perf_counter() - started:.3f} s")
    errs, _ = contract_tools.parse_changes(gone.stdout)
    assert any(e["path"] == "/pets" for e in errs)
    added = subprocess.run([REAL, "breaking", "base.yaml", "optional.yaml", "-f", "json", "--allow-external-refs=false"],
                           capture_output=True, text=True, cwd=tmp_path)
    assert contract_tools.parse_changes(added.stdout)[0] == []


@pytest.mark.skipif(REAL is None, reason="oasdiff is not installed")
def test_real_oasdiff_schema_pins_level_as_integer():
    import subprocess
    schema = json.loads(subprocess.run([REAL, "schema"], capture_output=True, text=True).stdout)

    def props(node):
        if isinstance(node, dict):
            if "properties" in node and "level" in node["properties"]:
                return node["properties"]
            for v in node.values():
                found = props(v)
                if found:
                    return found
        elif isinstance(node, list):
            for v in node:
                found = props(v)
                if found:
                    return found
        return None
    p = props(schema)
    assert p is not None and {"id", "text", "level", "operation", "path"} <= set(p)
    assert p["level"].get("type") == "integer"
