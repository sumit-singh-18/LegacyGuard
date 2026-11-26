"""Sprint 7.6 Part C: squawk for `.sql` migrations under contracts.db (tests 5-31b of the plan).

Tests 5-26 use tests/squawk_fake.py; 27-31b also run the real squawk-cli where it is installed (requirements-dev.txt
pins squawk-cli==2.74.0) and skip without it."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import time

import pytest

import oasdiff_fake
import squawk_fake as fake
from guardlib import audit, baseline, checks, ci, contract_tools, ledger, waivers
from test_contract_tools import NO_PETS, SPEC, _event, _pre_edit
from test_process_group import alive

from conftest import PLUGIN, TARGET_PY, write_tree

INIT = "CREATE TABLE t (id bigint, c int);\n"
REL = "db/0002.sql"


def _real_squawk() -> str | None:
    found = shutil.which("squawk")
    if found:
        return found
    candidate = os.path.join(sysconfig.get_path("scripts"), "squawk")  # pip's scripts folder, when the venv is not activated
    return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None


REAL = _real_squawk()
needs_real = pytest.mark.skipif(REAL is None, reason="squawk is not installed (pip install squawk-cli==2.74.0)")


def _project(tmp_path, make_cfg, monkeypatch, mode="lint", on_path=True, files=None, **overrides):
    root = write_tree(tmp_path / "proj", {"db/0001_init.sql": INIT, "app.py": "def f(x):\n    return x\n", **(files or {})})
    overrides.setdefault("contracts", {"db": "db"})
    cfg = make_cfg(root, **overrides)
    base = tmp_path / "fake"
    bin_dir = fake.install(base, mode)
    path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", (str(bin_dir) + os.pathsep + path) if on_path else path)
    return cfg, root, base


def _check(cfg, root, before, after, rel=REL, notes=None, state=None, **kw):
    notes = [] if notes is None else notes
    state = {} if state is None else state
    out = checks.run_checks(cfg, rel, before, after, {}, trees=(str(root), str(root)), notes=notes, tool_state=state, **kw)
    return out, notes, state


def _kinds(notes):
    return [k for k, _ in notes]


def _texts(notes):
    return [t for _, t in notes]


def _sha12(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _items(out):
    return [b.item for b in out]


def _no_squawk_on_path(monkeypatch):
    real_which = contract_tools.shutil.which
    monkeypatch.setattr(contract_tools.shutil, "which", lambda name, *a, **k: None if name == "squawk" else real_which(name, *a, **k))


# ---- 6.3: the check itself, fake binary ----

SNAPSHOT_LINT = ('[{"file":"main.sql","line":1,"column":3,"level":"Warning","message":"missing `set lock_timeout` before potentially slow `access exclusive` lock operations","note":"statement requires: `access exclusive` lock; blocking: reads, writes, schema changes","help":"configure a `lock_timeout` before this statement","rule_name":"require-lock-timeout","column_end":63,"line_end":1},'
                 '{"file":"main.sql","line":1,"column":3,"level":"Warning","message":"missing `set statement_timeout` before potentially slow operations","note":null,"help":"configure a `statement_timeout` before this statement","rule_name":"require-statement-timeout","column_end":63,"line_end":1},'
                 '{"file":"main.sql","line":1,"column":29,"level":"Warning","message":"adding a new column that is `not null` and has no default value to an existing table effectively makes it required","note":null,"help":"make the field nullable or add a non-volatile default","rule_name":"adding-required-field","column_end":62,"line_end":1},'
                 '{"file":"main.sql","line":1,"column":29,"level":"Warning","message":"missing `if not exists`, the migration can\'t be rerun if it fails part way through","note":null,"help":null,"rule_name":"prefer-robust-stmts","column_end":62,"line_end":1},'
                 '{"file":"main.sql","line":1,"column":46,"level":"Warning","message":"using 32-bit integer fields can result in hitting the max `int` limit","note":null,"help":"use 64-bit integer values instead to prevent hitting this limit","rule_name":"prefer-bigint-over-int","column_end":53,"line_end":1},'
                 '{"file":"main.sql","line":2,"column":23,"level":"Warning","message":"adding a new column that is `not null` and has no default value to an existing table effectively makes it required","note":null,"help":"make the field nullable or add a non-volatile default","rule_name":"adding-required-field","column_end":56,"line_end":2},'
                 '{"file":"main.sql","line":2,"column":23,"level":"Warning","message":"missing `if not exists`, the migration can\'t be rerun if it fails part way through","note":null,"help":null,"rule_name":"prefer-robust-stmts","column_end":56,"line_end":2},'
                 '{"file":"main.sql","line":2,"column":40,"level":"Warning","message":"using 32-bit integer fields can result in hitting the max `int` limit","note":null,"help":"use 64-bit integer values instead to prevent hitting this limit","rule_name":"prefer-bigint-over-int","column_end":47,"line_end":2}]\n')
SNAPSHOT_SYNTAX = ('[{"file":"test.sql","line":0,"column":36,"level":"Error","message":"missing comma","note":null,"help":null,'
                   '"rule_name":"syntax-error","column_end":36,"line_end":0}]\n')


# 5
def test_parse_squawk_accepts_the_pinned_snapshots():
    lint = contract_tools.parse_squawk(SNAPSHOT_LINT)
    assert len(lint) == 8 and lint[0]["rule_name"] == "require-lock-timeout" and (lint[0]["line"], lint[0]["column"]) == (1, 3)
    syntax = contract_tools.parse_squawk(SNAPSHOT_SYNTAX)
    assert len(syntax) == 1 and syntax[0]["level"] == "Error" and syntax[0]["rule_name"] == "syntax-error"
    assert contract_tools.parse_squawk("[]\n") == []


# 6
@pytest.mark.parametrize("text", [
    '{}', '"x"', '[1]', '[{"file":"a.sql","line":0,"column":0,"level":"Warning","message":"m"}]',
    '[{"file":"a.sql","line":true,"column":0,"level":"Warning","message":"m","rule_name":"r"}]',
    '[{"file":"a.sql","line":0,"column":0,"level":"Info","message":"m","rule_name":"r"}]',
    '[{"file":"a.sql","line":0,"column":-1,"level":"Warning","message":"m","rule_name":"r"}]',
    'not json'])
def test_parse_squawk_rejects_other_shapes(text):
    assert contract_tools.parse_squawk(text) is None


# 7
def test_command_cwd_config_and_env(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))  # the real $TMPDIR is never written
    for folder in (tmpdir, root):
        (folder / ".squawk.toml").write_text('excluded_rules = ["ban-drop-view"]\n')
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("SQUAWK_GITHUB_TOKEN", "x")
    out, notes, _ = _check(cfg, root, INIT, INIT + "DROP VIEW v;\n")
    (call,) = fake.calls(base)
    assert call["argv"] == ["--reporter", "json", "--config", "squawk.toml", "before.sql", "after.sql"]
    assert os.path.basename(call["cwd"]).startswith("lg-squawk-") and os.path.dirname(call["cwd"]) == str(tmpdir)
    assert call["listing"] == ["after.sql", "before.sql", "squawk.toml"]
    assert call["config"] == "assume_in_transaction = false\n"
    assert "GITHUB_ACTIONS" not in call["env"]  # other GITHUB_* names (set on CI runners) are passed through
    assert [k for k in call["env"] if k.startswith("SQUAWK")] == ["SQUAWK_DISABLE_GITHUB_ANNOTATIONS"]
    assert not os.path.exists(call["cwd"])
    assert [b.source for b in out] == ["squawk"] and notes == []

    fake.set_mode(base, "abs-file")
    out, notes, state = _check(cfg, root, INIT, INIT + "DROP VIEW v;\n")
    assert out == [] and _kinds(notes) == ["squawk-failed"] and str(tmpdir) not in _texts(notes)[0]
    assert state["squawk_unchecked_kind"] == {REL: "failed"}


# 8
def test_ignore_comments_are_neutralised(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    after = "-- squawk-ignore-file\nDROP VIEW v;\n"
    out, _, _ = _check(cfg, root, "", after)
    assert [(b.source, b.line) for b in out] == [("squawk", 2)] and "ban-drop-view" in out[0].subject
    sent = fake.calls(base)[0]["files"]["after.sql"]
    assert "squawk_ignore-file" in sent and "squawk-ignore" not in sent
    assert len(sent.encode()) == len(after.encode())
    assert contract_tools.prepare("-- squawk-disable-assume-in-transaction\n") == "-- squawk_disable_assume_in_transaction\n"


# 9
def test_new_file_only_after_is_passed(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    out, _, _ = _check(cfg, root, "", "DROP VIEW v;\n")
    (call,) = fake.calls(base)
    assert call["argv"][-1] == "after.sql" and "before.sql" not in call["argv"] and "before.sql" not in call["listing"]
    (b,) = out
    assert b.item == "squawk:ban-drop-view:drop view v:" + hashlib.sha256(b"drop view v").hexdigest()[:12]
    assert b.source == "squawk" and b.kind == "db" and b.line == 1 and b.contract == REL
    assert b.subject == "dropping a view may break existing clients (squawk ban-drop-view, line 1): drop view v"


# 10
def test_statement_already_there_is_not_new(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    out, notes, _ = _check(cfg, root, "drop   view\n  V;\n", "DROP VIEW v;\nSELECT 1;\n")
    assert out == [] and notes == []


# 11
def test_regex_items_stand_and_squawk_duplicates_go(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    out, _, _ = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c;\n")
    assert _items(out) == ["stmt:column dropped:c"]
    out, _, _ = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c; ALTER TABLE u DROP COLUMN d;\n")
    assert _items(out) == ["stmt:column dropped:c", "stmt:column dropped:d"]
    fake.set_mode(base, "drop-attr")
    out, _, _ = _check(cfg, root, "", "ALTER TYPE x DROP ATTRIBUTE a;\n")
    assert _items(out) == ["squawk:ban-drop-column:alter type x drop attribute a:" + _sha12("alter type x drop attribute a")]


# 11b
def test_sqlite_column_item_stands_for_drop_without_column_keyword(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    create = "CREATE TABLE t (a int, c int);\n"
    out, _, _ = _check(cfg, root, "", "ALTER TABLE t DROP c;\n", read_migrations=lambda r, b, a: ([create], [create, a]))
    assert [(b.item, b.subject) for b in out] == [("column:t.c", "column dropped: t.c")]
    out, _, _ = _check(cfg, root, "", "ALTER TABLE t DROP c;\n", read_migrations=lambda r, b, a: ([], [a]))  # SQLite fails
    assert _items(out) == ["squawk:ban-drop-column:alter table t drop c:" + _sha12("alter table t drop c")]


# 12
def test_risk_is_a_note_not_a_break(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    out, notes, _ = _check(cfg, root, "", "CREATE INDEX i ON t (c);\n")
    assert out == []
    assert notes == [(None, "LegacyGuard: squawk also reports 1 locking or run-time risk in db/0002.sql: "
                            "require-concurrent-index-creation (line 1).")]
    out, notes, _ = _check(cfg, root, "", "".join(f"CREATE INDEX i{n} ON t (c);\n" for n in range(5)))
    assert _texts(notes) == ["LegacyGuard: squawk also reports 5 locking or run-time risks in db/0002.sql: "
                             "require-concurrent-index-creation (line 1); require-concurrent-index-creation (line 2); "
                             "require-concurrent-index-creation (line 3); +2 more."]


# 13
def test_ignored_rules_give_nothing(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    out, notes, _ = _check(cfg, root, "", "ALTER TABLE t ADD COLUMN x text;\n")
    assert out == [] and notes == []
    assert len(fake.calls(base)) == 1


# 14
def test_unknown_rules_are_ignored_and_counted(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    out, notes, _ = _check(cfg, root, "", "SELECT FAKE_NEW_RULE;\n")
    assert out == [] and notes == []
    state = json.loads((cfg.state_dir / "tools.json").read_text())["squawk"]
    assert state["unknown_rules"] == ["ban-something-new"]
    assert "squawk 2.74.0" not in contract_tools.report_line(cfg, {})  # not probed yet: version unknown
    assert ", 1 unknown rule ignored" in contract_tools.report_line(cfg, {})
    _check(cfg, root, "", "SELECT FAKE_NEW_RULE2;\n")
    assert ", 2 unknown rules ignored" in contract_tools.report_line(cfg, {})


# 15
def test_parse_failure_keeps_the_regex_result(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    out, notes, state = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c;\nSYNTAXERR;\n")
    assert _items(out) == ["stmt:column dropped:c"] and _kinds(notes) == ["squawk-parse"]
    assert "the new text of db/0002.sql" in _texts(notes)[0] and "(line 2:" in _texts(notes)[0]
    assert state["squawk_unchecked_kind"] == {REL: "parse"}
    out, notes, _ = _check(cfg, root, "SYNTAXERR;\n", "SYNTAXERR;\nALTER TABLE t DROP COLUMN c;\n")
    assert _items(out) == ["stmt:column dropped:c"] and "the new text" in _texts(notes)[0]
    out, notes, _ = _check(cfg, root, "SELECT 1;\nSYNTAXERR;\n", "ALTER TABLE t DROP COLUMN c;\n")
    assert _items(out) == ["stmt:column dropped:c"] and _kinds(notes) == ["squawk-parse"]
    assert "the old text of db/0002.sql" in _texts(notes)[0] and "(line 2:" in _texts(notes)[0]


# 16
def test_missing_off_and_bad_configured_path(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch, on_path=False)
    _no_squawk_on_path(monkeypatch)
    out, notes, state = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c;\n")
    assert _items(out) == ["stmt:column dropped:c"] and notes == [("squawk", contract_tools.SQUAWK_MISSING_NOTE)]
    assert state["squawk_unchecked_kind"] == {REL: "missing"}
    data = {}
    assert contract_tools.take_notes(data, notes) == [contract_tools.SQUAWK_MISSING_NOTE]
    _, notes2, _ = _check(cfg, root, "", "DROP VIEW v;\n")
    assert contract_tools.take_notes(data, notes2) == []

    cfg.raw["contract_tools"]["squawk"] = False
    out, notes, state = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c;\n")
    assert _items(out) == ["stmt:column dropped:c"] and notes == [] and state["squawk_unchecked_kind"] == {REL: "off"}

    cfg.raw["contract_tools"]["squawk"] = "tools/nope"
    out, notes, _ = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c;\n")
    assert _items(out) == ["stmt:column dropped:c"]
    assert _texts(notes) == ["LegacyGuard: squawk configured path tools/nope not found or not executable, so the SQL "
                             "migration check used its built-in patterns."]
    (root / "tools").mkdir()
    (root / "tools" / "squawk").write_text("not a program\n")
    cfg.raw["contract_tools"]["squawk"] = "tools/squawk"
    _, notes, _ = _check(cfg, root, "", "DROP VIEW v;\n")
    assert _texts(notes) == ["LegacyGuard: squawk configured path tools/squawk not found or not executable, so the SQL "
                             "migration check used its built-in patterns."]
    assert fake.calls(base) == []


# 17
@pytest.mark.parametrize("mode", ["garbage", "object", "rc2", "config-error", "bad-field", "rc0-nonempty"])
def test_failure_modes_fall_back(tmp_path, make_cfg, monkeypatch, mode):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch, mode=mode)
    out, notes, state = _check(cfg, root, "", "ALTER TABLE t DROP COLUMN c;\nDROP VIEW v;\n")
    assert _items(out) == ["stmt:column dropped:c"] and _kinds(notes) == ["squawk-failed"]
    assert state["squawk_unchecked_kind"] == {REL: "failed"} and not state.get("squawk_stopped")
    if mode == "config-error":
        assert "Configuration error" in _texts(notes)[0]


# 18
def test_utf8_columns_and_bom(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    out, _, _ = _check(cfg, root, "", 'CREATE TABLE t (id bigint);\n/* é */ DROP VIEW "vé";\n')
    assert _items(out) == ["squawk:ban-drop-view:drop view vé:" + _sha12("drop view vé")] and out[0].line == 2
    plain, _, _ = _check(cfg, root, "", "DROP VIEW v;\n")
    bom, _, _ = _check(cfg, root, "", "\ufeffDROP VIEW v;\n")
    assert _items(bom) == _items(plain) and bom[0].line == plain[0].line == 1


# 18b
def test_long_statements_and_semicolons_in_comments(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    one, two = "v" * 125 + "aaaaa", "v" * 125 + "bbbbb"
    out, _, _ = _check(cfg, root, "", f"DROP VIEW {one};\nDROP VIEW {two};\n")
    assert len(out) == 2 and out[0].item != out[1].item
    first, second = (b.item.split(":") for b in out)
    assert first[2] == second[2] == ("drop view " + "v" * 125)[:80] and first[3] != second[3]
    assert first[3] == _sha12(f"drop view {one}")
    plain, _, _ = _check(cfg, root, "", "DROP VIEW v;\n")
    commented, _, _ = _check(cfg, root, "", "-- a; b\n/* c; d */ DROP VIEW v;\n")
    assert _items(commented) == _items(plain)


# 19
@pytest.mark.parametrize("value,passed", [
    ("13", "13"), (13, "13"), ("13.10", "13.10"), (" 12.4 ", "12.4"), (13.4, None), (13.1, None), ("thirteen", None),
    ("１３", None), ("99999", None), ("13.4.1.2", None), (True, None)])
def test_pg_version(tmp_path, make_cfg, monkeypatch, value, passed):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch, contract_tools={"squawk_pg_version": value})
    _, notes, _ = _check(cfg, root, "", "SELECT 1;\n")
    argv = fake.calls(base)[0]["argv"]
    if passed is None:
        assert "--pg-version" not in argv and notes == [("squawk-config", contract_tools.SQUAWK_CONFIG_NOTE)]
    else:
        assert argv[argv.index("--pg-version") + 1] == passed and notes == []


def test_pg_version_note_only_when_squawk_is_found(tmp_path, make_cfg, monkeypatch):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch, on_path=False,
                            contract_tools={"squawk_pg_version": "thirteen", "squawk": False})
    _, notes, _ = _check(cfg, root, "", "SELECT 1;\n")
    assert notes == []
    cfg.raw["contract_tools"]["squawk"] = "auto"
    _no_squawk_on_path(monkeypatch)
    _, notes, _ = _check(cfg, root, "", "SELECT 1;\n")
    assert _kinds(notes) == ["squawk"]


def test_assume_in_transaction(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch, contract_tools={"squawk_assume_in_transaction": True})
    _check(cfg, root, "", "SELECT 1;\n")
    call = fake.calls(base)[0]
    assert "--assume-in-transaction" in call["argv"] and call["config"] == "assume_in_transaction = true\n"


# 20
def test_hang_is_bounded_and_the_process_group_killed(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch, mode="hang-child")
    started = time.perf_counter()
    res = contract_tools.squawk_lint(cfg, REL, "", "DROP VIEW v;\n", 0.3)
    took = time.perf_counter() - started
    assert isinstance(res, contract_tools.Refusal) and res.kind == "timeout" and took < 1.5
    assert res.reason == "squawk took longer than 0.3 s on db/0002.sql; the built-in patterns were used."
    pids = [int(p) for p in (base / "hang.pids").read_text().split()]
    end = time.time() + 1.0
    while time.time() < end and any(alive(p) for p in pids):
        time.sleep(0.05)
    left = [p for p in pids if alive(p)]
    for p in left:
        os.kill(p, 9)
    assert left == []


# ---- 6.4: hooks, fake binary ----

def _git_session(cfg, root):
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    info = baseline.create(cfg)
    with ledger.locked(cfg.state_dir) as data:
        data.update(baseline_ok=True, baseline_commit=info["commit"], session_id="s")


def _hso(out):
    return (out or {}).get("hookSpecificOutput", {})


def _counter(cfg, name="squawk") -> int:
    return int(json.loads((cfg.state_dir / "tools.json").read_text()).get(name, {}).get("pre_edit_timeouts") or 0)


ITEM_V = "squawk:ban-drop-view:drop view v:" + hashlib.sha256(b"drop view v").hexdigest()[:12]


# 21
@pytest.mark.parametrize("mode", ["guarded", "strict", "advisory"])
def test_pre_edit_decision_per_mode_and_waiver(tmp_path, make_cfg, monkeypatch, mode):
    cfg, root, _ = _project(tmp_path, make_cfg, monkeypatch)
    cfg.raw["mode"] = mode  # the policy mode (_project's own `mode` is the fake's)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    out = pre_edit.handle(_event(root, REL, "DROP VIEW v;\n"))
    hso = _hso(out)
    data = ledger.load(cfg.state_dir)
    (f,) = [x for x in data["findings"] if x["kind"] == "contract_break"]
    assert f["source"] == "squawk" and f["contract_kind"] == "db" and f["item"] == ITEM_V
    if mode == "guarded":
        assert hso["permissionDecision"] == "ask" and "ban-drop-view" in hso["permissionDecisionReason"]
    elif mode == "strict":
        assert hso["permissionDecision"] == "deny" and "ban-drop-view" in hso["permissionDecisionReason"]
    else:
        assert "permissionDecision" not in hso and "ban-drop-view" in hso["additionalContext"]
    waivers.add(cfg, f, "intended")
    again = _hso(pre_edit.handle(_event(root, REL, "DROP VIEW v;\n")))
    assert again.get("permissionDecision") in (None, "allow")


# 22
def test_pre_edit_timeout_skip_rule_and_counters(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch, mode="slow",
                               contracts={"db": "db", "http": "openapi.yaml"}, files={"openapi.yaml": SPEC})
    oas_bin = oasdiff_fake.install(tmp_path / "oas", "slow")
    monkeypatch.setenv("PATH", str(oas_bin) + os.pathsep + os.environ["PATH"])
    _git_session(cfg, root)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    after = "ALTER TABLE t DROP COLUMN c;\n"
    started = time.perf_counter()
    out = pre_edit.handle(_event(root, REL, after))
    took = time.perf_counter() - started
    print(f"pre_edit.handle with a hanging fake squawk: {took:.3f} s")
    assert took < 1.2
    hso = _hso(out)
    assert "column dropped: c in a migration" in hso["permissionDecisionReason"]
    assert "squawk took longer than 0.8 s on db/0002.sql" in hso["additionalContext"]
    assert _counter(cfg) == 1
    pre_edit.handle(_event(root, REL, after + "-- 2\n"))
    assert _counter(cfg) == 2 and len(fake.calls(base)) == 2

    third = _hso(pre_edit.handle(_event(root, REL, after + "-- 3\n")))
    assert len(fake.calls(base)) == 2 and "twice in a row" in third["additionalContext"]
    fourth = _hso(pre_edit.handle(_event(root, REL, after + "-- 4\n")))
    assert "twice in a row" not in fourth.get("additionalContext", "") and len(fake.calls(base)) == 2

    with ledger.locked(cfg.state_dir) as data:  # Stop
        audit.run(cfg, data)
    contract_tools.refresh(cfg, reset_counters=False)  # as ci.run does
    assert _counter(cfg) == 2

    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS))  # an oasdiff timeout leaves squawk's counter alone
    assert _counter(cfg, "oasdiff") == 1 and _counter(cfg) == 2
    contract_tools.refresh(cfg, reset_counters=True)  # SessionStart
    assert _counter(cfg) == 0 and _counter(cfg, "oasdiff") == 0
    pre_edit.handle(_event(root, REL, after + "-- 5\n"))
    assert _counter(cfg) == 1 and _counter(cfg, "oasdiff") == 0 and len(fake.calls(base)) == 3


# 23
def test_django_migration_under_db_never_runs_squawk(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    after = ("from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n"
             "    operations = [migrations.RemoveField(model_name='item', name='price')]\n")
    out = pre_edit.handle(_event(root, "db/0002_x.py", after))
    assert _hso(out)["permissionDecision"] == "ask" and fake.calls(base) == []


def _stop(cfg, tool_state=None):
    with ledger.locked(cfg.state_dir) as data:
        if tool_state is None:
            audit.run(cfg, data)
        else:  # the same work as audit.run's contract step, with the run state visible to the test
            notes: list = []
            audit._contract_findings(cfg, data, baseline.changed_files(cfg), notes=notes, tool_state=tool_state)
            audit._report_notes(data, notes, None)
    return ledger.load(cfg.state_dir)


def _squawk_findings(data):
    return [f for f in data["findings"] if f["kind"] == "contract_break" and f.get("source") == "squawk"]


# 24
def test_stop_lifecycle(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    _git_session(cfg, root)
    (root / "db/0003.sql").write_text("DROP FUNCTION f;\n")
    data = _stop(cfg)
    (f,) = _squawk_findings(data)
    assert f["state"] == "open" and "found by the session audit" in f["evidence"] and f["contract_kind"] == "db"
    assert f["item"].startswith("squawk:ban-drop-function:drop function f:")
    assert len(_squawk_findings(_stop(cfg))) == 1

    fake.set_mode(base, "slow")
    cfg.raw["contract_tools"]["timeout_s"] = 1
    data = _stop(cfg)
    assert [x["state"] for x in _squawk_findings(data)] == ["open"]
    assert any("squawk took longer than 1 s" in n for n in data["contract_tools"]["notes"])
    cfg.raw["contract_tools"]["timeout_s"] = 20
    fake.set_mode(base, "lint")
    (root / "db/0003.sql").write_text("SELECT 1;\n")
    assert [x["state"] for x in _squawk_findings(_stop(cfg))] == ["not_applied"]

    def fresh():
        (root / "db/0003.sql").write_text("DROP FUNCTION f;\n")
        cfg.raw["contract_tools"]["squawk"] = "auto"
        with ledger.locked(cfg.state_dir) as d:
            d["findings"] = []
        assert [x["state"] for x in _squawk_findings(_stop(cfg))] == ["open"]

    fresh()  # (a) missing
    cfg.raw["contract_tools"]["squawk"] = str(tmp_path / "nope")
    assert [x["state"] for x in _squawk_findings(_stop(cfg))] == ["open"]
    fresh()  # (b) the file stops parsing
    (root / "db/0003.sql").write_text("DROP FUNCTION f;\nSYNTAXERR;\n")
    data = _stop(cfg)
    assert [x["state"] for x in _squawk_findings(data)] == ["open"]
    assert any("squawk could not parse the new text of db/0003.sql" in n for n in data["contract_tools"]["notes"])
    fresh()  # (c) switched off
    cfg.raw["contract_tools"]["squawk"] = False
    data = _stop(cfg)
    assert [x["state"] for x in _squawk_findings(data)] == ["not_applied"]
    assert "squawk switched off" in contract_tools.report_line(cfg, data)


THREE = {"db/0002_a.sql": "ALTER TABLE t DROP COLUMN a;\n", "db/0003_b.sql": "ALTER TABLE t DROP COLUMN b;\n",
         "db/0004_c.sql": "ALTER TABLE t DROP COLUMN c;\n"}


# 24b
def test_time_rule_one_timeout_per_stop(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch, mode="slow", contract_tools={"timeout_s": 1})
    _git_session(cfg, root)
    write_tree(root, THREE)
    state: dict = {}
    started = time.perf_counter()
    data = _stop(cfg, state)
    took = time.perf_counter() - started
    assert len(fake.calls(base)) == 1 and took < 5
    breaks = sorted(f["item"] for f in data["findings"] if f["kind"] == "contract_break")
    assert breaks == ["stmt:column dropped:a", "stmt:column dropped:b", "stmt:column dropped:c"]
    first = state["squawk_stopped"]
    assert state["squawk_unchecked_kind"] == {r: ("timeout" if r == first else "stopped") for r in THREE}
    stopped = [n for n in data["contract_tools"]["notes"] if "was not run on the remaining SQL migrations" in n]
    assert stopped == [checks.SQUAWK_STOPPED_NOTE.format(rel=first)]

    fake.set_mode(base, "garbage")  # bad output is about one file: it does not stop the run
    state = {}
    _stop(cfg, state)
    assert len(fake.calls(base)) == 4 and "squawk_stopped" not in state
    assert set(state["squawk_unchecked_kind"].values()) == {"failed"}


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def _ci_repo(tmp_path, monkeypatch, files, mode="lint", extra_config=""):
    root = write_tree(tmp_path / "repo", {"db/0001_init.sql": INIT, "app.py": "def f(x):\n    return x\n"})
    (root / ".gitignore").write_text(".legacyguard/\n")
    (root / ".legacyguard").mkdir()
    (root / ".legacyguard/config.yaml").write_text("contracts:\n  db: db\n" + extra_config)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base_sha = _git(root, "rev-parse", "HEAD").strip()
    write_tree(root, files)
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pr")
    fake_base = tmp_path / "fake"
    bin_dir = fake.install(fake_base, mode)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return root, base_sha, fake_base


def test_time_rule_in_ci(tmp_path, monkeypatch):
    root, base_sha, fake_base = _ci_repo(tmp_path, monkeypatch, THREE, mode="slow", extra_config="contract_tools:\n  timeout_s: 1\n")
    started = time.perf_counter()
    cfg, out = ci.run(str(root), base_sha, TARGET_PY, index=False)
    assert time.perf_counter() - started < 8
    assert len(fake.calls(fake_base)) == 1
    assert sorted(b.item for _, b in out.breaks) == ["stmt:column dropped:a", "stmt:column dropped:b", "stmt:column dropped:c"]
    assert len([n for n in out.notes if "was not run on the remaining SQL migrations" in n]) == 1


# 25
def test_ci_break_annotation_with_line_and_risk_note(tmp_path, monkeypatch):
    root, base_sha, _ = _ci_repo(tmp_path, monkeypatch, {REL: "DROP VIEW v;\nCREATE INDEX i ON t (id);\n"})
    cfg, out = ci.run(str(root), base_sha, TARGET_PY, index=False)
    assert [(f, b.item, b.line) for f, b in out.breaks] == [(REL, ITEM_V, 1)]
    (line,) = [a for a in ci.annotations(cfg, out) if "contract" in a]
    assert line.startswith("::warning file=db/0002.sql,line=1,title=LegacyGuard contract::dropping a view")
    risk = "LegacyGuard: squawk also reports 1 locking or run-time risk in db/0002.sql: require-concurrent-index-creation (line 2)."
    assert risk in out.notes
    report = ci.report(cfg, out)
    assert "Contract tool notes" in report and risk in report


# 26
def test_report_line_and_ledger_source(tmp_path, make_cfg, monkeypatch):
    cfg, root, base = _project(tmp_path, make_cfg, monkeypatch)
    contract_tools.refresh(cfg, reset_counters=True)
    assert fake.version_calls(base) == 1 and fake.calls(base) == []
    _check(cfg, root, "", "DROP VIEW v;\n")
    line = contract_tools.report_line(cfg, {})
    path = str(base / "bin" / "squawk")
    assert f"squawk 2.74.0 ({path}), last run " in line and " s" in line.split("last run ", 1)[1]

    cfg2, _, _ = _project(tmp_path / "two", make_cfg, monkeypatch, on_path=False)
    _no_squawk_on_path(monkeypatch)
    assert "squawk not found" in contract_tools.report_line(cfg2, {})
    cfg3, _, _ = _project(tmp_path / "three", make_cfg, monkeypatch, on_path=False, contracts={})
    assert "squawk" not in contract_tools.report_line(cfg3, {})

    from guardlib import report
    assert "squawk not found" in report.tools_line({}, cfg2)
    f, _ = ledger.add_finding(ledger.load(cfg.state_dir), kind="contract_break", status=ledger.FAILURE, source="squawk",
                              subject="s", file=REL, evidence="e", item=ITEM_V)
    assert f["source"] == "squawk"


# ---- 6.5: the real squawk (skips when it is not installed) ----

PAIR = ("CREATE TABLE t (id bigint);\n", "CREATE TABLE t (id bigint);\nALTER TABLE t DROP COLUMN c;\nDROP VIEW v;\nCREATE INDEX i ON t (c);\n")
FIELDS = ["file", "line", "column", "level", "message", "note", "help", "rule_name", "column_end", "line_end"]


def _real_cfg(tmp_path, make_cfg, **ct):
    root = write_tree(tmp_path / "proj", {"db/0001_init.sql": INIT})
    return make_cfg(root, contracts={"db": "db"}, contract_tools={"squawk": REAL, **ct}), root


# 27
@needs_real
def test_real_squawk_shape_and_rules(tmp_path, make_cfg):
    cfg, root = _real_cfg(tmp_path, make_cfg)
    contract_tools.refresh(cfg, reset_counters=True)
    assert json.loads((cfg.state_dir / "tools.json").read_text())["squawk"]["version"] == "2.74.0"
    res = contract_tools.squawk_lint(cfg, REL, *PAIR, 20)
    assert isinstance(res, contract_tools.SquawkResult), res
    rules = {(v["rule_name"], v["line"]) for v in res.after}
    assert ("ban-drop-column", 1) in rules and ("ban-drop-view", 2) in rules
    assert any(r == "require-concurrent-index-creation" for r, _ in rules)
    (tmp_path / "before.sql").write_text(PAIR[0])
    (tmp_path / "after.sql").write_text(PAIR[1])
    (tmp_path / "squawk.toml").write_text("assume_in_transaction = false\n")
    done = subprocess.run([REAL, "--reporter", "json", "--config", "squawk.toml", "before.sql", "after.sql"], cwd=tmp_path,
                          capture_output=True, text=True, input="")
    raw = json.loads(done.stdout)
    assert done.returncode == 1 and raw and all(list(v) == FIELDS for v in raw)
    assert [v["file"] for v in raw] == sorted(v["file"] for v in raw)  # after.sql first: files in path order
    out, notes, _ = _check(cfg, root, *PAIR)
    assert [b.item for b in out] == ["stmt:column dropped:c", ITEM_V] and out[1].line == 3
    assert _texts(notes) == ["LegacyGuard: squawk also reports 1 locking or run-time risk in db/0002.sql: "
                             "require-concurrent-index-creation (line 4)."]


# 28
@needs_real
def test_real_squawk_syntax_error_is_a_parse_refusal(tmp_path, make_cfg):
    cfg, _ = _real_cfg(tmp_path, make_cfg)
    res = contract_tools.squawk_lint(cfg, REL, "", "ALTER TABLE foo ALTER CONSTRAINT bar RENAME TO quux;\n", 20)
    assert isinstance(res, contract_tools.Refusal) and res.kind == "parse"
    assert res.reason == ("squawk could not parse the new text of db/0002.sql as PostgreSQL (line 1: missing comma); "
                          "the built-in patterns were used.")


# 29
@needs_real
def test_real_squawk_ignores_project_config_and_ignore_comments(tmp_path, make_cfg, monkeypatch):
    cfg, root = _real_cfg(tmp_path, make_cfg)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    for folder in (tmpdir, root):
        (folder / ".squawk.toml").write_text('excluded_rules = ["ban-drop-view"]\n')
    out, _, _ = _check(cfg, root, "", "DROP VIEW v;\n")
    assert _items(out) == [ITEM_V]
    out, _, _ = _check(cfg, root, "", "-- squawk-ignore-file\n-- squawk-ignore ban-drop-view\nDROP VIEW v;\n")
    assert _items(out) == [ITEM_V] and out[0].line == 3


# 30
@needs_real
def test_real_squawk_timing(tmp_path, make_cfg):
    cfg, _ = _real_cfg(tmp_path, make_cfg)
    contract_tools.squawk_lint(cfg, REL, *PAIR, 20)  # warm-up: a cold first start (CI runners) is not timed
    times = []
    for _ in range(10):
        started = time.perf_counter()
        assert isinstance(contract_tools.squawk_lint(cfg, REL, *PAIR, 20), contract_tools.SquawkResult)
        times.append(time.perf_counter() - started)
    print(f"squawk_lint with the real squawk: median {statistics.median(times):.4f} s, max {max(times):.4f} s")
    assert max(times) < contract_tools.settings(cfg)["pre_edit_timeout_s"]


def _line_ends(cfg, root, base, text, real):
    out, _, _ = _check(cfg, root, "", text)
    if not real:
        assert "\r" not in fake.calls(base)[-1]["files"]["after.sql"]
    return [(b.item, b.line) for b in out]


def _both(tmp_path, make_cfg, monkeypatch, which):
    if which == "real":
        if REAL is None:
            pytest.skip("squawk is not installed")
        cfg, root = _real_cfg(tmp_path, make_cfg)
        return cfg, root, None
    return _project(tmp_path, make_cfg, monkeypatch)


# 31
@pytest.mark.parametrize("which", ["fake", "real"])
def test_crlf_gives_the_same_items(tmp_path, make_cfg, monkeypatch, which):
    cfg, root, base = _both(tmp_path, make_cfg, monkeypatch, which)
    lf = "CREATE TABLE t (id bigint);\nDROP FUNCTION f;\nDROP VIEW v;\n"
    want = _line_ends(cfg, root, base, lf, which == "real")
    assert [line for _, line in want if line] == [2, 3]
    assert _line_ends(cfg, root, base, lf.replace("\n", "\r\n"), which == "real") == want


# 31b
@pytest.mark.parametrize("which", ["fake", "real"])
def test_lone_cr_gives_the_same_items(tmp_path, make_cfg, monkeypatch, which):
    cfg, root, base = _both(tmp_path, make_cfg, monkeypatch, which)
    lf = "CREATE TABLE t (id bigint);\nDROP VIEW v;\n"
    want = _line_ends(cfg, root, base, lf, which == "real")
    assert want == [(ITEM_V, 2)]
    assert _line_ends(cfg, root, base, lf.replace("\n", "\r"), which == "real") == want
