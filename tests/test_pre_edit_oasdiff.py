"""Sprint 7.6 Part A: oasdiff inside PreToolUse, its time bound and the skip rule (tests 11-13)."""
from __future__ import annotations

import json
import subprocess
import time

import oasdiff_fake as fake
from guardlib import audit, baseline, contract_tools, ledger
from test_contract_tools import NO_PETS, SPEC, _event, _pre_edit, _project


def _git_session(cfg, root):
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    info = baseline.create(cfg)
    with ledger.locked(cfg.state_dir) as data:
        data.update(baseline_ok=True, baseline_commit=info["commit"], session_id="s")


def _counter(cfg) -> int:
    return json.loads((cfg.state_dir / "tools.json").read_text())["oasdiff"]["pre_edit_timeouts"]


def _ctx(out) -> str:
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


# 11
def test_pre_edit_asks_with_oasdiff_subject_under_a_second(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    times = []
    for i in range(3):
        started = time.perf_counter()
        out = pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + f"# {i}\n"))
        times.append(time.perf_counter() - started)
        if i == 0:
            first = out
    hso = first["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert "GET /pets: api path removed without deprecation" in hso["permissionDecisionReason"]
    assert "possible breaking change" in hso["additionalContext"]
    print(f"pre_edit.handle with the fast fake oasdiff: {', '.join(f'{t:.3f}' for t in times)} s")
    assert max(times) < 1.0
    assert _counter(cfg) == 0


# 12
def test_hanging_tool_is_bounded(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, mode="slow")
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    started = time.perf_counter()
    out = pre_edit.handle(_event(root, "openapi.yaml", NO_PETS))
    took = time.perf_counter() - started
    print(f"pre_edit.handle with a hanging fake oasdiff: {took:.3f} s")
    assert took < contract_tools.settings(cfg)["pre_edit_timeout_s"] + 0.5
    hso = out["hookSpecificOutput"]
    assert "route removed: GET /pets" in hso["permissionDecisionReason"]  # the parser's result
    assert "oasdiff took longer than 0.8 s" in hso["additionalContext"]
    assert _counter(cfg) == 1


# 13
def test_skip_rule_survives_stop_and_ci_and_resets_at_session_start(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, mode="slow")
    _git_session(cfg, root)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS))
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 2\n"))
    assert _counter(cfg) == 2 and len(fake.calls(tmp_path)) == 2

    started = time.perf_counter()
    third = pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 3\n"))
    assert time.perf_counter() - started < 0.5
    assert len(fake.calls(tmp_path)) == 2  # not called
    assert "twice in a row" in _ctx(third)
    fourth = pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 4\n"))
    assert "twice in a row" not in _ctx(fourth) and len(fake.calls(tmp_path)) == 2

    with ledger.locked(cfg.state_dir) as data:  # Stop path
        audit.run(cfg, data)
    assert _counter(cfg) == 2
    contract_tools.refresh(cfg, reset_counters=False)  # as ci.run calls it
    assert _counter(cfg) == 2
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 5\n"))
    assert len(fake.calls(tmp_path)) == 2

    contract_tools.refresh(cfg, reset_counters=True)  # SessionStart
    assert _counter(cfg) == 0
    fake.set_mode(tmp_path, "ok")
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 6\n"))
    assert len(fake.calls(tmp_path)) == 3 and _counter(cfg) == 0


def test_touch_resets_and_fast_run_clears_one_timeout(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, mode="slow")
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS))
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 2\n"))
    assert _counter(cfg) == 2 and not contract_tools.pre_edit_allowed(cfg)
    with (tmp_path / "bin" / "oasdiff").open("a") as fh:
        fh.write("\n# touched\n")
    assert contract_tools.pre_edit_allowed(cfg) and _counter(cfg) == 0

    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 3\n"))
    assert _counter(cfg) == 1
    fake.set_mode(tmp_path, "ok")
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 4\n"))
    assert _counter(cfg) == 0


def test_session_start_resets_counter(tmp_path, make_cfg, monkeypatch):
    """The SessionStart hook itself calls refresh(reset_counters=True)."""
    from conftest import PLUGIN
    cfg, root = _project(tmp_path, make_cfg, monkeypatch, mode="slow")
    _git_session(cfg, root)
    pre_edit = _pre_edit(monkeypatch, cfg, root)
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS))
    pre_edit.handle(_event(root, "openapi.yaml", NO_PETS + "# 2\n"))
    assert _counter(cfg) == 2
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import session_start
    from indexer import __main__ as indexer_main
    monkeypatch.setattr(indexer_main, "refresh", lambda cfg: None)
    session_start.start_session(cfg, {"session_id": "s2", "source": "startup"})
    state = json.loads((cfg.state_dir / "tools.json").read_text())["oasdiff"]
    assert state["pre_edit_timeouts"] == 0 and state["version"] == "1.39.0"


def test_stop_and_ci_refresh_do_not_probe_an_unchanged_binary(tmp_path, make_cfg, monkeypatch):
    cfg, root = _project(tmp_path, make_cfg, monkeypatch)
    contract_tools.refresh(cfg, reset_counters=True)  # SessionStart probes
    assert fake.version_calls(tmp_path) == 1
    contract_tools.refresh(cfg, reset_counters=False)  # Stop
    contract_tools.refresh(cfg, reset_counters=False)  # the next Stop, or ci.run
    assert fake.version_calls(tmp_path) == 1
    assert json.loads((cfg.state_dir / "tools.json").read_text())["oasdiff"]["version"] == "1.39.0"
    with (tmp_path / "bin" / "oasdiff").open("a") as fh:  # a changed binary is probed again
        fh.write("\n# upgraded\n")
    contract_tools.refresh(cfg, reset_counters=False)
    assert fake.version_calls(tmp_path) == 2
    contract_tools.refresh(cfg, reset_counters=True)  # SessionStart still re-probes
    assert fake.version_calls(tmp_path) == 3


def test_pre_edit_on_django_migration_without_db_contract(tmp_path, make_cfg, monkeypatch):
    """The hook runs its checks on a migration .py when no contracts.db is configured (it used to raise TypeError in
    _read_migrations and fail open with no checks)."""
    from conftest import PLUGIN, write_tree
    root = write_tree(tmp_path / "proj", {"shop/migrations/__init__.py": "",
                                         "shop/migrations/0002_x.py": "from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n    operations = []\n"})
    cfg = make_cfg(root)
    assert cfg.contract_path("db") is None
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import pre_edit
    monkeypatch.setattr(pre_edit, "load_guard", lambda event, fp=None: (str(root), cfg))
    after = ("from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n"
             "    operations = [migrations.RunSQL(\"DROP TABLE shop_item\")]\n")
    out = pre_edit.handle(_event(root, "shop/migrations/0002_x.py", after))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask" and "in a migration" in hso["permissionDecisionReason"]
    data = ledger.load(cfg.state_dir)
    assert data["edits"] == 1 and any(f["kind"] == "contract_break" and f["contract_kind"] == "db" for f in data["findings"])
