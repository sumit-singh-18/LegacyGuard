"""Sprint 7.6 Part D: `buf breaking` for .proto contracts under contracts.proto (tests 1-35 of the plan).

Tests 1-29c use tests/buf_fake.py; 30-35 run the real buf and skip without it. The real binary is found through the
LEGACYGUARD_TEST_BUF environment variable (a path), then PATH; CI downloads buf v1.75.0 with a checksum check
(.github/workflows/tests.yml) and sets the variable. Never `pip install buf`."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import buf_fake as fake
from guardlib import audit, autosetup, baseline, ci, contract_tools, ledger, proto_contract, waivers
from runner import os_sandbox
from test_contract_tools import _event, _pre_edit
from test_process_group import alive

from conftest import PLUGIN, TARGET_PY, write_tree

USER = """syntax = "proto3";

package acme.user.v1;

message User {
  string id = 1;
  string email = 2;
}

message GetRequest {
  string id = 1;
}

service UserService {
  rpc Get(GetRequest) returns (User);
  rpc List(GetRequest) returns (User);
}
"""
NO_EMAIL = USER.replace("  string email = 2;\n", "")
OLD = 'syntax = "proto3";\n\npackage acme.user.v1;\n\nmessage Old {\n  string x = 1;\n}\n'
REL = "proto/acme/user/v1/user.proto"
MOD = "acme/user/v1/user.proto"
MSG_EMAIL = 'Previously present field "2" with name "email" on message "User" was deleted.'


def _sha12(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _key(rule, rel, message):
    m = " ".join(message.split())
    return f"buf:{rule}:{rel}:{m[:80]}:{_sha12(rule + chr(0) + rel + chr(0) + m)}"


def _write(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
    return root


def _gitinit(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


def _install(tmp_path, monkeypatch, mode="breaking", on_path=True):
    base = tmp_path / "fake"
    bin_dir = fake.install(base, mode)
    path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", (str(bin_dir) + os.pathsep + path) if on_path else path)
    return base


def _pair(tmp_path, make_cfg, monkeypatch, base_files=None, rev_files=None, mode="breaking", folder="proto", on_path=True,
          sandbox="off", **overrides):
    """A baseline tree and a working tree (untracked files in two git repos) plus the fake. The OS sandbox is off unless
    asked for: inside it the fake could not write its call log outside the temp folder (test 21 widens it)."""
    base_dir = _write(tmp_path / "base", base_files if base_files is not None else {REL: USER, "proto/acme/user/v1/old.proto": OLD})
    root = _write(tmp_path / "proj", rev_files if rev_files is not None else {REL: NO_EMAIL, "proto/acme/user/v1/old.proto": OLD})
    _gitinit(base_dir)
    _gitinit(root)
    overrides.setdefault("contracts", {"proto": folder})
    overrides.setdefault("behavioral_diff", {"os_sandbox": sandbox})
    cfg = make_cfg(root, **overrides)
    fb = _install(tmp_path, monkeypatch, mode, on_path)
    return cfg, base_dir, root, fb


def _compute(cfg, base_dir, root, timeout=20):
    return proto_contract.compute(cfg, str(base_dir), str(root), timeout)


def _items(out):
    return [b.item for _, b in out.breaks]


def _no_buf_on_path(monkeypatch):
    real_which = contract_tools.shutil.which
    monkeypatch.setattr(contract_tools.shutil, "which", lambda name, *a, **k: None if name == "buf" else real_which(name, *a, **k))


# ---- 6.2: units ----

# 1
def test_parse_buf_shapes():
    good = ('{"start_line":1,"start_column":1,"end_line":1,"end_column":1,"type":"FILE_NO_DELETE","message":"m"}\n'
            '{"path":"rev/a.proto","start_line":5,"start_column":1,"end_line":7,"end_column":2,"type":"FIELD_NO_DELETE","message":"n","plugin":"p","extra":3}\n\n')
    out = contract_tools.parse_buf(good)
    assert out == [{"start_line": 1, "start_column": 1, "end_line": 1, "end_column": 1, "type": "FILE_NO_DELETE", "message": "m"},
                   {"path": "rev/a.proto", "start_line": 5, "start_column": 1, "end_line": 7, "end_column": 2,
                    "type": "FIELD_NO_DELETE", "message": "n", "plugin": "p"}]
    assert contract_tools.parse_buf("") == []
    for bad in ('[{"type":"X","message":"m"}]', '"x"', '{"message":"m"}', '{"type":"","message":"m"}',
                '{"type":"X","message":"m","start_line":"1"}', '{"type":"X","message":"m","start_line":0}',
                '{"type":"X","message":"m","start_line":true}', '{"type":"X","message":null}',
                '{"type":"X","message":"m","path":3}', '{"type":"X","message":"m","plugin":1}', 'not json'):
        assert contract_tools.parse_buf(bad) is None, bad


# 2
def test_item_keys():
    key = proto_contract.item_key("FIELD_NO_DELETE", REL, MSG_EMAIL)
    assert key == f"buf:FIELD_NO_DELETE:{REL}:{MSG_EMAIL[:80]}:{_sha12('FIELD_NO_DELETE' + chr(0) + REL + chr(0) + MSG_EMAIL)}"
    assert proto_contract.item_key("FIELD_NO_DELETE", "proto/other.proto", MSG_EMAIL) != key
    assert proto_contract.item_key("FIELD_NO_DELETE", REL, MSG_EMAIL.replace(" ", "  \t")) == key
    long_a, long_b = "x" * 90 + "a", "x" * 90 + "b"
    a, b = proto_contract.item_key("R", REL, long_a), proto_contract.item_key("R", REL, long_b)
    assert a != b and a.split(":")[3] == b.split(":")[3] == "x" * 80


# 3
def test_collect_rules(tmp_path, monkeypatch):
    root = _write(tmp_path / "t", {"proto/a.proto": "a", "proto/buf.yaml": "version: v2\n", "proto/buf.lock": "x",
                                   "proto/buf.work.yaml": "x", "proto/n.txt": "t", "proto/sub/b.proto": "b",
                                   "proto/ignored.proto": "i", ".gitignore": "proto/ignored.proto\n",
                                   "other/c.proto": "c", "outside.proto": "o"})
    _gitinit(root)
    subprocess.run(["git", "-C", str(root), "add", "proto/a.proto"], check=True)  # tracked and untracked both count
    files, refusal = proto_contract.collect(str(root), "proto/")
    assert refusal is None and files == {"a.proto": b"a", "sub/b.proto": b"b"}
    os.symlink(str(root / "proto/a.proto"), str(root / "proto/link.proto"))
    files, refusal = proto_contract.collect(str(root), "proto/")
    assert refusal is None and files["link.proto"] == b"a"
    os.symlink(str(root / "outside.proto"), str(root / "proto/in.proto"))
    files, refusal = proto_contract.collect(str(root), "proto/")
    assert refusal is None  # a link to another file inside the tree is fine
    os.unlink(str(root / "proto/in.proto"))
    (tmp_path / "x.proto").write_text("outside the tree")
    os.symlink(str(tmp_path / "x.proto"), str(root / "proto/out.proto"))
    files, refusal = proto_contract.collect(str(root), "proto/")
    assert files == {} and refusal[0] == "refused" and "proto/out.proto is a symlink" in refusal[1]
    os.unlink(str(root / "proto/out.proto"))
    monkeypatch.setattr(proto_contract, "MAX_PROTO_FILES", 2)
    assert proto_contract.collect(str(root), "proto/")[1][0] == "too_large"
    monkeypatch.setattr(proto_contract, "MAX_PROTO_FILES", 2000)
    monkeypatch.setattr(proto_contract, "MAX_PROTO_BYTES", 2)
    assert proto_contract.collect(str(root), "proto/")[1][0] == "too_large"


@pytest.mark.parametrize("rel,name", [("proto/buf.yaml/x.proto", "buf.yaml"), ("proto/acme/buf.lock/y.proto", "buf.lock"),
                                      ("proto/buf.work.yaml/z.proto", "buf.work.yaml"),
                                      ("proto/Buf.yaml/w.proto", "Buf.yaml")])
def test_collect_refuses_buf_named_folders(tmp_path, make_cfg, monkeypatch, rel, name):
    files = {REL: USER, rel: 'syntax = "proto3";\n'}
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, files, {**files, REL: NO_EMAIL})
    out = _compute(cfg, base_dir, root)
    assert out.state == "not_checked" and out.kind == "refused" and fake.calls(fb) == []
    (kind, text), = out.notes
    assert kind == "buf-failed" and rel in text and f"a folder named {name}" in text


# 4
def test_bytes_are_copied_as_they_are(tmp_path, make_cfg, monkeypatch):
    crlf = b"\xef\xbb\xbf" + NO_EMAIL.replace("\n", "\r\n").encode()
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER}, {REL: crlf})
    out = _compute(cfg, base_dir, root)
    (call,) = fake.calls(fb)
    assert bytes.fromhex(call["bytes"]["rev/" + MOD]) == crlf and bytes.fromhex(call["bytes"]["base/" + MOD]) == USER.encode()
    assert _items(out) == [_key("FIELD_NO_DELETE", REL, MSG_EMAIL)]


# 5
def test_buf_category(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    assert contract_tools.buf_category(cfg) == ("FILE", None)
    for value, want in (("file", "FILE"), (" WIRE_JSON ", "WIRE_JSON"), ("package", "PACKAGE"), ("wire", "WIRE")):
        cfg.raw["contract_tools"]["buf_category"] = value
        assert contract_tools.buf_category(cfg) == (want, None)
    for value in ("CSR", ["FILE"], 3, None):
        cfg.raw["contract_tools"]["buf_category"] = value
        cat, note = contract_tools.buf_category(cfg)
        assert cat == "FILE" and note == ("buf-category", contract_tools.BUF_CATEGORY_NOTE)


def test_bad_category_and_bad_folder_give_two_notes(tmp_path, make_cfg, monkeypatch):
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch)
    cfg.raw["contract_tools"]["buf_category"] = "CSR"
    (root / REL).write_text(NO_EMAIL)
    data = _stop(cfg)
    assert "buf-category" in data["tool_notes"]
    assert [f["state"] for f in _bufs(data)] == ["open"]
    cfg.raw["contracts"]["proto"] = "proto/user.proto"
    data = _stop(cfg)
    assert "buf-category" in data["tool_notes"] and "buf-folder" in data["tool_notes"]


# ---- 6.3: isolation and command ----

# 6
def test_command_cwd_config_env_stdin(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch)
    for k, v in (("BUF_TOKEN", "x"), ("BUF_DEBUG", "1"), ("BUF_TIMEOUT", "1s"), ("BUF_CACHE_DIR", "/somewhere")):
        monkeypatch.setenv(k, v)
    out = _compute(cfg, base_dir, root)
    (call,) = fake.calls(fb)
    assert fake.strip_prefix(call["argv"], fb) == ["breaking", "rev#format=dir", "--against", "base#format=dir",
                                                    "--error-format", "json", "--disable-symlinks"]
    cwd = call["cwd"]
    assert os.path.basename(cwd).startswith("lg-buf-")
    assert call["configs"]["rev/buf.yaml"] == call["configs"]["base/buf.yaml"] == "version: v2\nbreaking:\n  use:\n    - FILE\n"
    assert call["configs"]["lg-buf.yaml"] is None and "deps" not in call["configs"]["rev/buf.yaml"]
    assert call["stdin"] == ""
    assert call["env"] == ["BUF_CACHE_DIR", "BUF_CONFIG_DIR", "BUF_DATA_DIR"]
    real_cwd = os.path.realpath(cwd)
    for k in ("BUF_CACHE_DIR", "BUF_CONFIG_DIR", "BUF_DATA_DIR", "HOME"):
        assert os.path.realpath(call["values"][k]).startswith(real_cwd + os.sep), k
    assert call["listing"] == ["base/acme/user/v1/old.proto", "base/acme/user/v1/user.proto", "base/buf.yaml",
                               "rev/acme/user/v1/old.proto", "rev/acme/user/v1/user.proto", "rev/buf.yaml"]
    assert not os.path.exists(cwd)
    assert out.state == "checked" and _items(out) == [_key("FIELD_NO_DELETE", REL, MSG_EMAIL)]


def _decoys(tmp_path, monkeypatch):
    """tempfile.tempdir under tmp_path/a, a v2 buf.yaml in it whose module is the run's rev folder, and a buf.work.yaml one
    level up covering both inputs; each would be read if buf's walk-up reached it (shown in test 32)."""
    a = tmp_path / "a"
    tmpdir = a / "tmpdir"
    tmpdir.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))  # the real $TMPDIR is never written
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kw):
        d = real_mkdtemp(*args, **kw)
        if os.path.basename(d).startswith("lg-buf-"):
            name = os.path.basename(d)
            (tmpdir / "buf.yaml").write_text(f"version: v2\nmodules:\n  - path: {name}/rev\nbreaking:\n  use: [FILE]\n"
                                             "  except: [FIELD_NO_DELETE]\n")
            (a / "buf.work.yaml").write_text(f"version: v1\ndirectories:\n  - tmpdir/{name}/rev\n  - tmpdir/{name}/base\n")
        return d
    monkeypatch.setattr(contract_tools.tempfile, "mkdtemp", mkdtemp)
    return tmpdir


# 7
def test_decoys_and_project_config_are_not_given_to_buf(tmp_path, make_cfg, monkeypatch):
    project = {"proto/buf.yaml": "version: v2\nbreaking:\n  except: [FIELD_NO_DELETE]\n", "proto/buf.work.yaml": "version: v1\n"}
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER, **project}, {REL: NO_EMAIL, **project})
    tmpdir = _decoys(tmp_path, monkeypatch)
    out = _compute(cfg, base_dir, root)
    (call,) = fake.calls(fb)
    assert os.path.dirname(call["cwd"]) == str(tmpdir)
    assert call["listing"] == ["base/acme/user/v1/user.proto", "base/buf.yaml", "rev/acme/user/v1/user.proto", "rev/buf.yaml"]
    assert "except" not in call["configs"]["rev/buf.yaml"]
    assert _items(out) == [_key("FIELD_NO_DELETE", REL, MSG_EMAIL)]


# 8, 8b
def test_root_folder_prefix(tmp_path, make_cfg, monkeypatch):
    pkg = 'syntax = "proto3";\npackage gone.v1; // PACKAGEGONE\n'
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {MOD: USER, "p.proto": pkg}, {MOD: NO_EMAIL, "p.proto": pkg},
                                    folder=".")
    (root / ".legacyguard/baseline/acme").mkdir(parents=True)
    (root / ".legacyguard/baseline/acme/x.proto").write_text(USER)  # never collected
    assert proto_contract.applies(cfg, [MOD]) and not proto_contract.applies(cfg, ["app.py"])
    out = _compute(cfg, base_dir, root)
    (call,) = fake.calls(fb)
    assert [p for p in call["listing"] if p.endswith(".proto")] == ["base/" + MOD, "base/p.proto", "rev/" + MOD, "rev/p.proto"]
    got = {(file, b.item.split(":")[1], b.item.split(":")[2]) for file, b in out.breaks}
    assert got == {(MOD, "FIELD_NO_DELETE", MOD), (".", "PACKAGE_NO_DELETE", "-")}
    assert _key("FIELD_NO_DELETE", MOD, MSG_EMAIL) in _items(out)


# ---- 6.4: results and failures ----

# 9
def test_field_deleted_and_service_deleted(tmp_path, make_cfg, monkeypatch):
    no_service = NO_EMAIL[: NO_EMAIL.index("service UserService")]
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER}, {REL: no_service})
    out = _compute(cfg, base_dir, root)
    by_rule = {b.item.split(":")[1]: (file, b) for file, b in out.breaks}
    file, b = by_rule["FIELD_NO_DELETE"]
    assert (file, b.kind, b.source, b.contract, b.line) == (REL, "proto", "buf", REL, 5)
    assert b.subject == MSG_EMAIL + " (buf FIELD_NO_DELETE)"
    file, b = by_rule["SERVICE_NO_DELETE"]
    assert file == REL and b.line is None


# 10
def test_type_change_rename_and_no_change(tmp_path, make_cfg, monkeypatch):
    rev = USER.replace("string id = 1;\n  string email", "int64 id = 1;\n  string mail")
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER}, {REL: rev})
    rules = sorted(b.item.split(":")[1] for _, b in _compute(cfg, base_dir, root).breaks)
    assert rules == ["FIELD_SAME_NAME", "FIELD_SAME_TYPE"]
    (root / REL).write_text(USER + "// a comment\n")
    out = _compute(cfg, base_dir, root)
    assert out.state == "checked" and out.breaks == [] and len(fake.calls(fb)) == 2


# 11
def test_deleted_file_is_attributed_by_message(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER, "proto/acme/user/v1/old.proto": OLD}, {REL: USER})
    ((file, b),) = _compute(cfg, base_dir, root).breaks
    msg = 'Previously present file "acme/user/v1/old.proto" was deleted.'
    assert file == "proto/acme/user/v1/old.proto" and b.line is None
    assert b.item == _key("FILE_NO_DELETE", "proto/acme/user/v1/old.proto", msg)


# 12
def test_pathless_rule_goes_to_the_folder(tmp_path, make_cfg, monkeypatch):
    pkg = 'syntax = "proto3";\npackage gone.v1; // PACKAGEGONE\n'
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER, "proto/p.proto": pkg}, {REL: USER, "proto/p.proto": pkg + "\n"})
    ((file, b),) = _compute(cfg, base_dir, root).breaks
    assert file == "proto" and ":-:" in b.item and b.item.startswith("buf:PACKAGE_NO_DELETE:-:") and b.line is None


# 13
def test_category_reaches_the_config(tmp_path, make_cfg, monkeypatch):
    base = USER + "message Extra {\n  string a = 1;\n}\n"
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: base}, {REL: NO_EMAIL},
                                    contract_tools={"buf_category": "WIRE_JSON"})
    out = _compute(cfg, base_dir, root)
    (call,) = fake.calls(fb)
    assert "- WIRE_JSON" in call["configs"]["rev/buf.yaml"] and "- WIRE_JSON" in call["configs"]["base/buf.yaml"]
    assert [b.item.split(":")[1] for _, b in out.breaks] == ["FIELD_NO_DELETE"] and out.category == "WIRE_JSON"


# 14
def test_compile_errors(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER}, {REL: NO_EMAIL + "// COMPILEERR\n"})
    out = _compute(cfg, base_dir, root)
    assert (out.state, out.kind, out.breaks) == ("not_checked", "compile", [])
    (kind, text), = out.notes
    assert kind == "buf-compile" and "the new .proto files" in text and REL in text and "lg-buf-" not in text
    (base_dir / REL).write_text(USER + "// COMPILEERR\n")
    (root / REL).write_text(NO_EMAIL)
    out = _compute(cfg, base_dir, root)
    assert out.kind == "compile" and "the baseline .proto files" in out.notes[0][1]


# 15
@pytest.mark.parametrize("token", ["IMPORTMISSING", "STDERRIMPORT"])
def test_missing_import(tmp_path, make_cfg, monkeypatch, token):
    rev = NO_EMAIL.replace("package acme.user.v1;\n", f'package acme.user.v1;\nimport "third/x.proto"; // {token}\n')
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {REL: USER}, {REL: rev})
    out = _compute(cfg, base_dir, root)
    assert (out.state, out.kind) == ("not_checked", "imports")
    (kind, text), = out.notes
    assert kind == "buf-imports" and 'import "third/x.proto" in ' + REL in text and "fetches no dependencies" in text
    assert "lg-buf-" not in text


# 16
def test_empty_sides(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, {"app.py": "x = 1\n"}, {REL: USER})
    assert _compute(cfg, base_dir, root).state == "skip" and fake.calls(fb) == []
    (base_dir / REL).parent.mkdir(parents=True)
    (base_dir / REL).write_text(USER)
    (base_dir / "proto/acme/user/v1/old.proto").write_text(OLD)
    (root / REL).unlink()
    out = _compute(cfg, base_dir, root)
    assert fake.calls(fb) == [] and out.state == "checked"
    assert sorted(_items(out)) == sorted([_key("FILE_NO_DELETE", REL, f'Previously present file "{MOD}" was deleted.'),
                                          _key("FILE_NO_DELETE", "proto/acme/user/v1/old.proto",
                                               'Previously present file "acme/user/v1/old.proto" was deleted.')])
    # the same key as buf's own line for one deleted file
    (root / REL).write_text(USER)
    single = _compute(cfg, base_dir, root)
    assert _items(single) == [_key("FILE_NO_DELETE", "proto/acme/user/v1/old.proto",
                                   'Previously present file "acme/user/v1/old.proto" was deleted.')]
    assert set(_items(single)) <= set(_items(out))
    (root / REL).unlink()
    cfg.raw["contract_tools"]["buf_category"] = "WIRE"
    out = _compute(cfg, base_dir, root)
    assert (out.state, out.kind, out.notes[0][0]) == ("not_checked", "empty", "buf-empty")


# 17
@pytest.mark.parametrize("mode,kind", [("garbage", "failed"), ("array", "failed"), ("rc1", "failed"), ("rc0-nonempty", "failed"),
                                       ("rc100-empty", "failed"), ("bad-field", "failed"), ("zero-line", "failed"),
                                       ("abs-path", None), ("foreign-path", "failed"), ("plugin", None)])
def test_failure_modes(tmp_path, make_cfg, monkeypatch, mode, kind):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, mode=mode)
    out = _compute(cfg, base_dir, root)
    if kind is None:
        assert out.state == "checked" and _key("FIELD_NO_DELETE", REL, MSG_EMAIL) in _items(out)
        return
    assert (out.state, out.kind, out.breaks) == ("not_checked", kind, [])
    (note_kind, text), = out.notes
    assert note_kind == "buf-failed" and "lg-buf-" not in text
    if mode == "rc1":
        assert "buf could not check the .proto files: Failure: something; nothing was reported." in text
    if mode == "foreign-path":
        assert "did not give it" in text
    data = {}
    assert contract_tools.take_notes(data, out.notes) == [text]
    assert contract_tools.take_notes(data, out.notes) == []


# 18
def test_missing_off_bad_path_and_old(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, on_path=False)
    _no_buf_on_path(monkeypatch)
    out = _compute(cfg, base_dir, root)
    assert (out.kind, out.notes) == ("missing", [("buf", contract_tools.BUF_MISSING_NOTE)])
    cfg.raw["contract_tools"]["buf"] = False
    out = _compute(cfg, base_dir, root)
    assert (out.state, out.kind, out.notes) == ("not_checked", "off", [])
    cfg.raw["contract_tools"]["buf"] = str(tmp_path / "nope")
    out = _compute(cfg, base_dir, root)
    assert out.kind == "missing" and out.notes[0][1] == (f"LegacyGuard: buf configured path {tmp_path / 'nope'} not found or not "
                                                        "executable, so .proto changes were not checked.")
    (tmp_path / "noexec").write_text("x")
    cfg.raw["contract_tools"]["buf"] = str(tmp_path / "noexec")
    assert _compute(cfg, base_dir, root).kind == "missing"
    cfg.raw["contract_tools"]["buf"] = str(fb / "bin" / "buf")
    fake.set_mode(fb, "old-version")
    contract_tools.refresh(cfg, reset_counters=True)
    out = _compute(cfg, base_dir, root)
    assert out.kind == "old" and out.notes[0][0] == "buf-old" and "1.31.0 is older than 1.32.0" in out.notes[0][1]
    assert fake.calls(fb) == []
    assert "buf 1.31.0 too old" in contract_tools.report_line(cfg, {})
    assert fake.calls(fb) == []


# 19
def test_hang_is_bounded_and_the_process_group_killed(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, mode="hang-child")
    started = time.perf_counter()
    out = _compute(cfg, base_dir, root, timeout=0.3)
    took = time.perf_counter() - started
    assert out.kind == "timeout" and took < 1.5
    assert out.notes == [("buf-timeout", "LegacyGuard: buf took longer than 0.3 s; .proto changes were not checked.")]
    pids = [int(p) for p in (fb / "hang.pids").read_text().split()]
    end = time.time() + 1.0
    while time.time() < end and any(alive(p) for p in pids):
        time.sleep(0.05)
    left = [p for p in pids if alive(p)]
    for p in left:
        os.kill(p, 9)
    assert left == []


# 20
def test_refresh_probes_only_with_contracts_proto(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, contracts={})
    contract_tools.refresh(cfg, reset_counters=True)
    assert fake.version_calls(fb) == 0 and "buf" not in json.loads((cfg.state_dir / "tools.json").read_text())
    cfg.raw["contracts"] = {"proto": "proto"}
    contract_tools.refresh(cfg, reset_counters=True)
    assert fake.version_calls(fb) == 1
    contract_tools.refresh(cfg, reset_counters=False)  # Stop and ci.run: an unchanged binary is not probed again
    assert fake.version_calls(fb) == 1 and fake.calls(fb) == []


# 21
needs_sandbox = pytest.mark.skipif(not os_sandbox.active(sys.executable), reason="no OS sandbox on this machine")


@needs_sandbox
def test_sandbox_blocks_writes_and_network(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, mode="probe", sandbox="auto")
    real_wrap = os_sandbox.wrap
    # the fake logs its calls under its own folder: that folder (only) is made writable too
    monkeypatch.setattr(os_sandbox, "wrap", lambda cmd, py, writable, readonly, mode="auto", **kw:
                        real_wrap(cmd, py, list(writable) + [str(fb)], readonly, mode, **kw))
    (fb / "probe_target").write_text(str(root / "probe.txt"))
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    (fb / "port").write_text(str(server.getsockname()[1]))
    try:
        out = _compute(cfg, base_dir, root)
    finally:
        server.close()
    assert json.loads((fb / "probe.json").read_text()) == {"write": "denied", "connect": "denied"}
    assert not (root / "probe.txt").exists() and out.state == "checked"
    contract_tools.refresh(cfg, reset_counters=True)
    _compute(cfg, base_dir, root)
    line = contract_tools.report_line(cfg, {})
    assert "buf 1.75.0" in line and "no OS sandbox" not in line

    cfg.raw["behavioral_diff"]["os_sandbox"] = "off"
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    (fb / "port").write_text(str(server.getsockname()[1]))
    try:
        _compute(cfg, base_dir, root)
    finally:
        server.close()
    assert json.loads((fb / "probe.json").read_text()) == {"write": "ok", "connect": "ok"}
    assert "no OS sandbox" in contract_tools.report_line(cfg, {})


# ---- 6.5: hooks ----

def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _session(tmp_path, make_cfg, monkeypatch, mode="breaking", files=None, **overrides):
    root = _write(tmp_path / "proj", {REL: USER, "proto/acme/user/v1/old.proto": OLD, "app.py": "def f(x):\n    return x\n",
                                      ".gitignore": ".legacyguard/\n", **(files or {})})
    overrides.setdefault("contracts", {"proto": "proto"})
    overrides.setdefault("behavioral_diff", {"os_sandbox": "off"})
    cfg = make_cfg(root, **overrides)
    fb = _install(tmp_path, monkeypatch, mode)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    info = baseline.create(cfg)
    with ledger.locked(cfg.state_dir) as data:
        data.update(baseline_ok=True, baseline_commit=info["commit"], session_id="s")
    return cfg, root, fb


def _stop(cfg):
    with ledger.locked(cfg.state_dir) as data:
        audit.run(cfg, data)
    return ledger.load(cfg.state_dir)


def _bufs(data):
    return [f for f in data["findings"] if f["kind"] == "contract_break" and f.get("source") == "buf"]


# 22
def test_stop_finding_lifecycle(tmp_path, make_cfg, monkeypatch):
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch)
    (root / REL).write_text(NO_EMAIL)  # a shell edit
    with ledger.locked(cfg.state_dir) as data:
        unresolved = audit.run(cfg, data)
    assert [f["source"] for f in unresolved] == ["buf"]
    data = ledger.load(cfg.state_dir)
    (f,) = _bufs(data)
    assert (f["state"], f["contract_kind"], f["file"], f["item"]) == ("open", "proto", REL, _key("FIELD_NO_DELETE", REL, MSG_EMAIL))
    assert "found by buf breaking (FILE) against the session baseline" in f["evidence"]
    assert len(_bufs(_stop(cfg))) == 1 and len(fake.calls(fb)) == 2
    (root / REL).write_text(USER + "// restored\n")
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["not_applied"] and len(fake.calls(fb)) == 3
    (root / REL).write_text(NO_EMAIL)
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["open"]
    (root / REL).write_text(USER)  # nothing differs from the baseline: buf is not run, the sweep closes it
    n = len(fake.calls(fb))
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["not_applied"] and len(fake.calls(fb)) == n


# 23
def test_lifecycle_kinds(tmp_path, make_cfg, monkeypatch):
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch)

    def fresh():
        fake.set_mode(fb, "breaking")
        cfg.raw["contract_tools"] = {**cfg.raw["contract_tools"], "buf": "auto", "timeout_s": 20}
        cfg.raw["contracts"] = {"proto": "proto"}
        cfg.raw["scope"] = "."
        (root / REL).write_text(NO_EMAIL)
        with ledger.locked(cfg.state_dir) as d:
            d["findings"], d["tool_notes"] = [], []
        assert [x["state"] for x in _bufs(_stop(cfg))] == ["open"]

    fresh()  # (a) timeout
    fake.set_mode(fb, "slow")
    cfg.raw["contract_tools"]["timeout_s"] = 1
    data = _stop(cfg)
    assert [x["state"] for x in _bufs(data)] == ["open"] and "buf-timeout" in data["tool_notes"]
    assert any("buf took longer than 1 s" in n for n in data["contract_tools"]["notes"])
    fresh()  # (b) missing
    cfg.raw["contract_tools"]["buf"] = str(tmp_path / "nope")
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["open"]
    fresh()  # (c) compile error
    (root / REL).write_text(NO_EMAIL + "// COMPILEERR\n")
    data = _stop(cfg)
    assert [x["state"] for x in _bufs(data)] == ["open"] and "buf-compile" in data["tool_notes"]
    fresh()  # (d) missing import
    (root / REL).write_text(NO_EMAIL.replace("package acme.user.v1;\n", 'package acme.user.v1;\nimport "x/y.proto"; // IMPORTMISSING\n'))
    data = _stop(cfg)
    assert [x["state"] for x in _bufs(data)] == ["open"] and "buf-imports" in data["tool_notes"]
    fresh()  # (e) switched off
    cfg.raw["contract_tools"]["buf"] = False
    data = _stop(cfg)
    assert [x["state"] for x in _bufs(data)] == ["not_applied"]
    assert "buf switched off" in contract_tools.report_line(cfg, data)
    fresh()  # (f) invalid folder, also on a Stop with no changed .proto
    cfg.raw["contracts"]["proto"] = "proto/user.proto"
    data = _stop(cfg)
    assert [x["state"] for x in _bufs(data)] == ["open"] and "buf-folder" in data["tool_notes"]
    assert "buf not run: contracts.proto is invalid" in contract_tools.report_line(cfg, data)
    (root / REL).write_text(USER)
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["open"]
    fresh()  # (g) key removed
    cfg.raw["contracts"] = {}
    data = _stop(cfg)
    assert [x["state"] for x in _bufs(data)] == ["not_applied"]
    assert "buf not run: contracts.proto not set" in contract_tools.report_line(cfg, data)
    from guardlib import report
    assert "buf not run: contracts.proto not set" in report.tools_line(data, cfg)
    fresh()  # (h) folder outside scope
    cfg.raw["scope"] = "src/"
    cfg.raw["contracts"] = {"proto": "../proto"}
    assert cfg.contract_path("proto") == "proto"
    n = len(fake.calls(fb))
    first = _stop(cfg)
    assert [x["state"] for x in _bufs(first)] == ["open"]
    assert any("is outside scope (src/)" in t for t in first["contract_tools"]["notes"])
    second = _stop(cfg)
    assert [x["state"] for x in _bufs(second)] == ["open"] and second["tool_notes"].count("buf-folder") == 1
    assert not any("is outside scope" in t for t in second["contract_tools"]["notes"]) and len(fake.calls(fb)) == n


# 24
def test_waiver(tmp_path, make_cfg, monkeypatch):
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch)
    (root / REL).write_text(NO_EMAIL)
    (f,) = _bufs(_stop(cfg))
    with ledger.locked(cfg.state_dir) as data:  # what /legacyguard:waive does
        g = ledger.find(data, f["id"])
        waivers.add(cfg, g, "intended")
        g["state"] = "waived"
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["waived"]
    (root / REL).write_text(NO_EMAIL + "// unrelated\n")
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["waived"]
    (root / REL).write_text(NO_EMAIL)
    with ledger.locked(cfg.state_dir) as d:  # a new session, the same text
        d["findings"] = []
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["waived"]
    (root / REL).write_text(NO_EMAIL + "// other\n")
    with ledger.locked(cfg.state_dir) as d:
        d["findings"] = []
    assert [x["state"] for x in _bufs(_stop(cfg))] == ["open"]


# 25
def test_one_buf_call_per_stop(tmp_path, make_cfg, monkeypatch):
    three = {f"proto/acme/v1/f{i}.proto": f'syntax = "proto3";\npackage acme.v1;\nmessage M{i} {{\n  string a = 1;\n}}\n' for i in range(3)}
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch, files=three, contract_tools={"timeout_s": 1})
    fake.set_mode(fb, "slow")
    for rel in three:
        (root / rel).write_text((root / rel).read_text() + "// changed\n")
    started = time.perf_counter()
    _stop(cfg)
    assert time.perf_counter() - started < 7 and len(fake.calls(fb)) == 1


def _ci_repo(tmp_path, monkeypatch, base_files, pr_files, extra_config=""):
    root = _write(tmp_path / "repo", {"app.py": "def f(x):\n    return x\n", ".gitignore": ".legacyguard/\n", **base_files})
    (root / ".legacyguard").mkdir()
    (root / ".legacyguard/config.yaml").write_text("contracts:\n  proto: proto\nbehavioral_diff:\n  os_sandbox: \"off\"\n" + extra_config)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base_sha = _git(root, "rev-parse", "HEAD").strip()
    _write(root, pr_files)
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pr")
    fb = _install(tmp_path, monkeypatch)
    return root, base_sha, fb


# 26
def test_ci(tmp_path, monkeypatch):
    pkg = 'syntax = "proto3";\npackage gone.v1; // PACKAGEGONE\n'
    root, base_sha, fb = _ci_repo(tmp_path, monkeypatch, {REL: USER, "proto/p.proto": pkg},
                                  {REL: NO_EMAIL, "proto/p.proto": pkg + "\n"})
    cfg, out = ci.run(str(root), base_sha, TARGET_PY, index=False)
    assert len(fake.calls(fb)) == 1
    got = sorted((f, b.item.split(":")[1], b.line) for f, b in out.breaks)
    assert got == [("proto", "PACKAGE_NO_DELETE", None), (REL, "FIELD_NO_DELETE", 5)]
    lines = [a for a in ci.annotations(cfg, out) if "contract" in a]
    assert any(a.startswith(f"::warning file={REL},line=5,title=LegacyGuard contract::{MSG_EMAIL}") for a in lines)
    pathless = [a for a in lines if "PACKAGE_NO_DELETE" in a]
    assert pathless and all("file=" not in a.split("::")[1] for a in pathless)
    (f, b), = [(f, b) for f, b in out.breaks if f == REL]
    waivers.add(cfg, {"id": "LG-1", "kind": "contract_break", "file": f, "subject": b.subject, "item": b.item,
                      "code_hash": proto_contract.code_hash(str(root), f, b.item)}, "intended")
    cfg, out = ci.run(str(root), base_sha, TARGET_PY, index=False)
    assert [f for f, _ in out.breaks] == ["proto"]
    fake.set_mode(fb, "rc1")
    cfg, out = ci.run(str(root), base_sha, TARGET_PY, index=False)
    assert out.breaks == [] and any("Failure: something" in n for n in out.notes)
    assert "Contract tool notes" in ci.report(cfg, out)


def test_ci_service_deleted_has_no_line(tmp_path, monkeypatch):
    no_service = USER[: USER.index("service UserService")]
    root, base_sha, fb = _ci_repo(tmp_path, monkeypatch, {REL: USER}, {REL: no_service})
    cfg, out = ci.run(str(root), base_sha, TARGET_PY, index=False)
    ((f, b),) = out.breaks
    assert b.item.split(":")[1] == "SERVICE_NO_DELETE" and b.line is None
    (line,) = [a for a in ci.annotations(cfg, out) if "contract" in a]
    assert line.startswith(f"::warning file={REL},title=LegacyGuard contract::") and "line=" not in line


# 27
def test_report_line_and_ledger_source(tmp_path, make_cfg, monkeypatch):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch)
    contract_tools.refresh(cfg, reset_counters=True)
    _compute(cfg, base_dir, root)
    line = contract_tools.report_line(cfg, {})
    path = str(fb / "bin" / "buf")
    assert f"buf 1.75.0 ({path}), last run " in line and "no OS sandbox" in line  # the sandbox is off in these tests
    cfg.raw["contract_tools"]["buf_category"] = "WIRE"
    assert "category WIRE" in contract_tools.report_line(cfg, {})
    cfg2, _, _, _ = _pair(tmp_path / "two", make_cfg, monkeypatch, on_path=False)
    _no_buf_on_path(monkeypatch)
    contract_tools.refresh(cfg2, reset_counters=True)
    assert "buf not found" in contract_tools.report_line(cfg2, {})
    cfg3, _, _, _ = _pair(tmp_path / "three", make_cfg, monkeypatch, contracts={})
    assert "buf" not in contract_tools.report_line(cfg3, {"findings": []})
    from guardlib import report
    assert "buf not found" in report.tools_line({}, cfg2)
    f, _ = ledger.add_finding(ledger.load(cfg.state_dir), kind="contract_break", status=ledger.FAILURE, source="buf",
                              subject="s", file=REL, evidence="e", item="buf:X:-:m:0")
    assert f["source"] == "buf"


# 28
def _setup_repo(root: Path, files: dict) -> Path:
    _write(root, {"a/__init__.py": "", "a/b.py": "x = 1\n", "a/c.py": "y = 2\n", **files})
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    return root


@pytest.mark.parametrize("files,want", [
    ({"proto/buf.yaml": "version: v1\n"}, "proto"),
    ({"buf.yaml": "version: v2\nmodules:\n  - path: proto\n"}, "proto"),
    ({"buf.yaml": "version: v2\n"}, "."),
    ({"buf.yaml": "version: v2\nmodules:\n  - path: a\n  - path: b\n"}, None),
    ({"proto/buf.yaml": "version: v1\n", "buf.work.yaml": "version: v1\ndirectories: [proto]\n"}, None),
    ({"proto/buf.yaml": "version: v1\n", "other/buf.yaml": "version: v1\n"}, None),
    ({"proto/buf.yaml": "version: [\n"}, None),
    ({"proto/buf.yaml": "version: v9\n"}, None),
])
def test_autosetup_proto(tmp_path, files, want):
    root = _setup_repo(tmp_path / "r", files)
    found = autosetup.detect(root)
    assert found["proto"] == want
    text = autosetup.render(found)
    assert (f"  proto: {want}" in text) == (want is not None)


def test_autosetup_proto_relative_to_scope(tmp_path):
    src = {"src/pkg/__init__.py": "", "src/pkg/a.py": "x = 1\n", "src/pkg/b.py": "y = 1\n"}
    root = _setup_repo(tmp_path / "s", {**src, "src/proto/buf.yaml": "version: v1\n"})
    found = autosetup.detect(root)
    assert found["scope"] == "src/" and found["proto"] == "proto"
    root2 = _setup_repo(tmp_path / "t", {**src, "proto/buf.yaml": "version: v1\n"})
    assert autosetup.detect(root2)["proto"] is None


def test_ci_load_cfg_picks_up_the_key(tmp_path):
    root = _setup_repo(tmp_path / "r", {"proto/buf.yaml": "version: v1\n"})
    cfg = ci.load_cfg(str(root))
    assert cfg.contract_path("proto") == "proto"
    root2 = _setup_repo(tmp_path / "s", {"src/pkg/__init__.py": "", "src/pkg/a.py": "x = 1\n", "src/pkg/b.py": "y = 1\n",
                                         "src/proto/buf.yaml": "version: v1\n"})
    assert ci.load_cfg(str(root2)).contract_path("proto") == "src/proto"


# 29
def test_no_buf_at_pre_and_post_edit(tmp_path, make_cfg, monkeypatch):
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    out = pre_edit.handle(_event(root, REL, NO_EMAIL))
    assert "buf" not in json.dumps(out or {})
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import post_edit
    monkeypatch.setattr(post_edit, "load_guard", lambda event, fp=None: (str(root), cfg))
    (root / REL).write_text(NO_EMAIL)
    post_edit.handle({"tool_input": {"file_path": str(root / REL)}, "cwd": str(root), "session_id": "s"})
    assert fake.calls(fb) == [] and "buf" not in json.dumps(ledger.load(cfg.state_dir).get("tool_notes") or [])


# 29b
def test_unset_key_note(tmp_path, make_cfg, monkeypatch):
    cfg, root, fb = _session(tmp_path, make_cfg, monkeypatch, contracts={}, files={"other/x.proto": OLD})
    (root / REL).write_text(NO_EMAIL)
    data = _stop(cfg)
    want = proto_contract.UNSET_NOTE.format(rel=REL)
    assert want in data["contract_tools"]["notes"] and fake.calls(fb) == [] and _bufs(data) == []
    data = _stop(cfg)
    assert want not in data["contract_tools"]["notes"] and data["tool_notes"].count("buf-unset") == 1

    cfg2, root2, fb2 = _session(tmp_path / "two", make_cfg, monkeypatch, contracts={}, scope="src/",
                                files={"src/app.py": "x = 1\n"})
    (root2 / REL).write_text(NO_EMAIL)  # outside scope
    assert "buf-unset" not in (_stop(cfg2).get("tool_notes") or [])


# 29c
@pytest.mark.parametrize("word,want", [("1.75.0", (1, 75, 0)), ("1.75.1-dev", (1, 75, 1)), ("1.40.0", (1, 40, 0)),
                                       ("1.31.0", (1, 31, 0)), ("buf", None), ("v1.75.0", None),
                                       ("１.７５.０", None), ("", None), (None, None)])
def test_buf_version(word, want):
    assert contract_tools.buf_version(word) == want


@pytest.mark.parametrize("mode,runs", [("dev-version", True), ("garbage-version", True), ("old-version", False)])
def test_version_gate(tmp_path, make_cfg, monkeypatch, mode, runs):
    cfg, base_dir, root, fb = _pair(tmp_path, make_cfg, monkeypatch, mode=mode)
    contract_tools.refresh(cfg, reset_counters=True)
    out = _compute(cfg, base_dir, root)
    assert (len(fake.calls(fb)) == 1) == runs
    assert (out.kind == "old") != runs


# ---- 6.6: the real buf (skips without it) ----

def _find_real() -> str | None:
    for cand in (os.environ.get("LEGACYGUARD_TEST_BUF"), shutil.which("buf")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            try:
                res = os_sandbox.run([cand, "--version"], 10, capture_output=True, text=True)
            except (OSError, subprocess.TimeoutExpired):
                continue
            v = contract_tools.buf_version((res.stdout or "").strip().split()[-1] if (res.stdout or "").strip() else "")
            if v is not None and v >= contract_tools.BUF_MIN_VERSION:
                return cand
    return None


REAL = _find_real()
needs_real = pytest.mark.skipif(REAL is None, reason="buf >= 1.32.0 not found (set LEGACYGUARD_TEST_BUF or put buf on PATH)")
FIELDS = {"path", "start_line", "start_column", "end_line", "end_column", "type", "message", "plugin", "policy"}
PAIR_BASE = {REL: USER, "proto/acme/user/v1/old.proto": OLD}
PAIR_REV = {REL: NO_EMAIL.replace("string id = 1;\n}\n\nmessage GetRequest", "int64 id = 1;\n}\n\nmessage GetRequest")
            .replace("  rpc Get(GetRequest) returns (User);\n", "")}


def _real_pair(tmp_path, make_cfg, base_files=PAIR_BASE, rev_files=PAIR_REV, sandbox="auto", **ct):
    base_dir = _write(tmp_path / "base", base_files)
    root = _write(tmp_path / "proj", rev_files)
    _gitinit(base_dir)
    _gitinit(root)
    cfg = make_cfg(root, contracts={"proto": "proto"}, contract_tools={"buf": REAL, **ct},
                   behavioral_diff={"os_sandbox": sandbox})
    contract_tools.refresh(cfg, reset_counters=True)
    return cfg, base_dir, root


def _raw(tmp_path, base_files, rev_files, our_config=True, binary=None):
    """Run the real buf by hand on a layout like ours (rev/, base/), for shape checks. Returns (rc, stdout, stderr)."""
    w = Path(tempfile.mkdtemp(prefix="lg-buf-raw-", dir=str(tmp_path)))
    for side, files in (("base", base_files), ("rev", rev_files)):
        (w / side).mkdir()
        for m, body in files.items():
            _write(w / side, {m: body})
        if our_config:
            (w / side / "buf.yaml").write_text(contract_tools.buf_config_text("FILE"))
    for d in ("cache", "config", "data", "home"):
        (w / d).mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("BUF_")}
    env.update(BUF_CACHE_DIR=str(w / "cache"), BUF_CONFIG_DIR=str(w / "config"), BUF_DATA_DIR=str(w / "data"), HOME=str(w / "home"))
    res = os_sandbox.run([binary or REAL, "breaking", "rev#format=dir", "--against", "base#format=dir", "--error-format", "json",
                          "--disable-symlinks"], 60, input=b"", capture_output=True, cwd=str(w), env=env)
    return res.returncode, res.stdout.decode(), res.stderr.decode()


def _mods(files):
    return {k[len("proto/"):]: v for k, v in files.items()}


# 30
@needs_real
def test_real_shape(tmp_path, make_cfg):
    rc, out, err = _raw(tmp_path, _mods(PAIR_BASE), _mods(PAIR_REV))
    print(out)
    assert rc == 100 and err == ""
    lines = contract_tools.parse_buf(out)
    assert lines is not None
    for raw in out.splitlines():
        assert set(json.loads(raw)) <= FIELDS
        keys = list(json.loads(raw))
        assert keys == [k for k in ("path", "start_line", "start_column", "end_line", "end_column", "type", "message") if k in keys]
    rules = {x["type"] for x in lines}
    assert {"FIELD_NO_DELETE", "FIELD_SAME_TYPE", "RPC_NO_DELETE", "FILE_NO_DELETE"} <= rules
    (deleted,) = [x for x in lines if x["type"] == "FILE_NO_DELETE"]
    assert "path" not in deleted and deleted["message"] == 'Previously present file "acme/user/v1/old.proto" was deleted.'
    assert {x["path"] for x in lines if "path" in x} == {"rev/" + MOD}
    cfg, base_dir, root = _real_pair(tmp_path / "c", make_cfg)
    res = _compute(cfg, base_dir, root)
    assert res.state == "checked" and _key("FIELD_NO_DELETE", REL, MSG_EMAIL) in _items(res)
    assert {f for f, _ in res.breaks} == {REL, "proto/acme/user/v1/old.proto"}


# 31
@needs_real
def test_real_failures(tmp_path, make_cfg):
    cfg, base_dir, root = _real_pair(tmp_path / "a", make_cfg, rev_files={REL: NO_EMAIL.replace("string id = 1;", "string id = 1")})
    res = _compute(cfg, base_dir, root)
    assert res.kind == "compile" and "the new .proto files" in res.notes[0][1] and REL in res.notes[0][1]
    rev = NO_EMAIL.replace("package acme.user.v1;\n", 'package acme.user.v1;\nimport "third/x.proto";\n')
    cfg, base_dir, root = _real_pair(tmp_path / "b", make_cfg, rev_files={REL: rev})
    res = _compute(cfg, base_dir, root)
    assert res.kind == "imports" and 'import "third/x.proto" in ' + REL in res.notes[0][1]
    rc, out, err = _raw(tmp_path, _mods(PAIR_BASE), {})
    print(rc, repr(out), repr(err))
    assert rc == 1 and out == "" and "had no .proto files" in err
    no_service = USER[: USER.index("service UserService")]
    cfg, base_dir, root = _real_pair(tmp_path / "c", make_cfg, base_files={REL: USER}, rev_files={REL: no_service})
    res = _compute(cfg, base_dir, root)
    ((f, b),) = [(f, b) for f, b in res.breaks if b.item.split(":")[1] == "SERVICE_NO_DELETE"]
    assert f == REL and b.line is None


# 32
@needs_real
def test_real_decoys_are_ignored(tmp_path, make_cfg, monkeypatch):
    # First: without our buf.yaml, a v2 buf.yaml one level up whose module is rev/ does change the result.
    w = tmp_path / "proof"
    w.mkdir()
    rc, out, _ = _raw(w, _mods({REL: USER}), _mods({REL: NO_EMAIL}), our_config=False)
    assert "FIELD_NO_DELETE" in out
    (inner,) = [p for p in w.iterdir() if p.name.startswith("lg-buf-raw-")]
    (w / "buf.yaml").write_text(f"version: v2\nmodules:\n  - path: {inner.name}/rev\nbreaking:\n  use: [FILE]\n  except: [FIELD_NO_DELETE]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("BUF_")}
    env.update(BUF_CACHE_DIR=str(inner / "cache"), HOME=str(inner / "home"))
    res = os_sandbox.run([REAL, "breaking", "rev#format=dir", "--against", "base#format=dir", "--error-format", "json"], 60,
                         input=b"", capture_output=True, cwd=str(inner), env=env)
    assert b'"FIELD_NO_DELETE"' not in res.stdout and res.returncode in (0, 100)  # the decoy alone silences the rule
    (w / "buf.yaml").unlink()
    (w / "buf.work.yaml").write_text(f"version: v1\ndirectories:\n  - {inner.name}/rev\n  - {inner.name}/base\n")
    res = os_sandbox.run([REAL, "breaking", "rev#format=dir", "--against", "base#format=dir", "--error-format", "json"], 60,
                         input=b"", capture_output=True, cwd=str(inner), env=env)
    assert res.returncode == 1  # the buf.work.yaml decoy alone also changes the result (an error)

    project = {"proto/buf.yaml": "version: v2\nbreaking:\n  ignore: [acme]\n"}
    cfg, base_dir, root = _real_pair(tmp_path / "c", make_cfg, base_files={REL: USER, **project},
                                     rev_files={REL: NO_EMAIL, **project}, sandbox="off")
    _decoys(tmp_path / "c", monkeypatch)
    out = _compute(cfg, base_dir, root)
    assert out.state == "checked" and _key("FIELD_NO_DELETE", REL, MSG_EMAIL) in _items(out)


# 33
@needs_real
def test_real_cache_and_home_stay_empty(tmp_path, make_cfg, monkeypatch):
    home, xdg = tmp_path / "home", tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    cfg, base_dir, root = _real_pair(tmp_path / "c", make_cfg)
    seen = []
    real_rmtree = contract_tools.shutil.rmtree

    def rmtree(path, *a, **k):  # what buf left in our temp folder, before it is removed
        if os.path.basename(str(path)).startswith("lg-buf-"):
            seen.extend(sorted(os.path.relpath(os.path.join(d, f), path) for d, _, fs in os.walk(path) for f in fs))
        return real_rmtree(path, *a, **k)
    monkeypatch.setattr(contract_tools.shutil, "rmtree", rmtree)
    assert _compute(cfg, base_dir, root).state == "checked"
    print("files in the temp folder after the run:", seen)
    assert list(home.iterdir()) == [] and list(xdg.iterdir()) == []
    assert not [s for s in seen if not s.startswith(("rev/", "base/"))]  # buf wrote no file of its own


def _timing(cfg, base_dir, root, n=10):
    times = []
    for _ in range(n):
        started = time.perf_counter()
        out = _compute(cfg, base_dir, root)
        times.append(time.perf_counter() - started)
        assert out.state == "checked" and out.breaks
    return statistics.median(times), max(times)


# 34
@needs_real
def test_real_timing(tmp_path, make_cfg):
    big_base, big_rev = {}, {}
    for i in range(200):
        body = f'syntax = "proto3";\npackage acme.gen.v1;\nmessage M{i} {{\n' + "".join(f"  string f{j} = {j};\n" for j in range(1, 6)) + "}\n"
        big_base[f"proto/acme/gen/v1/m{i}.proto"] = body
        big_rev[f"proto/acme/gen/v1/m{i}.proto"] = body.replace("  string f5 = 5;\n", "") if i == 0 else body
    report = []
    for sandbox in ("auto", "off"):
        if sandbox == "auto" and not os_sandbox.active(sys.executable):
            continue
        cfg, base_dir, root = _real_pair(tmp_path / f"small-{sandbox}", make_cfg, sandbox=sandbox)
        _compute(cfg, base_dir, root)  # warm-up (page cache)
        med, mx = _timing(cfg, base_dir, root)
        report.append(f"3-file pair, sandbox {sandbox}: median {med:.3f} s, max {mx:.3f} s")
        assert mx < contract_tools.settings(cfg)["timeout_s"]
        cfg, base_dir, root = _real_pair(tmp_path / f"big-{sandbox}", make_cfg, big_base, big_rev, sandbox=sandbox)
        _compute(cfg, base_dir, root)
        med, mx = _timing(cfg, base_dir, root)
        report.append(f"200-file module, sandbox {sandbox}: median {med:.3f} s, max {mx:.3f} s")
        assert mx < contract_tools.settings(cfg)["timeout_s"]
    print("\nbuf timing: " + "; ".join(report))


# 35
@needs_real
def test_real_crlf_and_bom(tmp_path, make_cfg):
    cfg, base_dir, root = _real_pair(tmp_path / "lf", make_cfg)
    lf = _compute(cfg, base_dir, root)
    crlf_rev = {k: v.replace("\n", "\r\n") for k, v in PAIR_REV.items()}
    cfg, base_dir, root = _real_pair(tmp_path / "crlf", make_cfg, rev_files=crlf_rev)
    crlf = _compute(cfg, base_dir, root)
    assert sorted(_items(lf)) == sorted(_items(crlf))
    assert sorted((b.item, b.line) for _, b in lf.breaks) == sorted((b.item, b.line) for _, b in crlf.breaks)
    bom = {k: b"\xef\xbb\xbf" + v.encode() for k, v in PAIR_REV.items()}
    cfg, base_dir, root = _real_pair(tmp_path / "bom", make_cfg, rev_files=bom)
    res = _compute(cfg, base_dir, root)
    print("BOM-prefixed file:", res.state, res.kind)
    assert res.state == "checked" and sorted(_items(res)) == sorted(_items(lf))


# 30b: the fake prints what the real binary prints on the fixture pairs (keeps tests/buf_fake.py pinned)
@needs_real
@pytest.mark.parametrize("pair", ["main", "service", "message-and-rename"])
def test_fake_matches_real(tmp_path, monkeypatch, pair):
    if pair == "main":
        base_files, rev_files = _mods(PAIR_BASE), _mods(PAIR_REV)
    elif pair == "service":
        base_files, rev_files = {MOD: USER}, {MOD: NO_EMAIL[: NO_EMAIL.index("service UserService")]}
    else:
        base_files = {MOD: USER + "\nmessage Gone {\n  string z = 1;\n}\n"}
        rev_files = {MOD: USER.replace("string email = 2;", "string mail = 2;")}
    real = _raw(tmp_path, base_files, rev_files)
    fb = _install(tmp_path, monkeypatch)
    faked = _raw(tmp_path, base_files, rev_files, binary=str(fb / "bin" / "buf"))
    drop = {"FIELD_SAME_JSON_NAME"}  # a rename also gives this rule on the real binary; the fake does not model it
    real_lines = [x for x in real[1].splitlines() if json.loads(x)["type"] not in drop]
    assert (real[0], real_lines) == (faked[0], faked[1].splitlines())
