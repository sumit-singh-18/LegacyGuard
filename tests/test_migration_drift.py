"""Sprint 7.6 Part A: Django model drift from `makemigrations --check --dry-run` with no database (tests 16-28).

Skipped when the target interpreter cannot import Django (requirements-dev.txt has it, decision D3)."""
from __future__ import annotations

import fcntl
import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import PLUGIN, TARGET_PY, write_tree

if subprocess.run([TARGET_PY, "-c", "import django"], capture_output=True).returncode != 0:
    pytest.skip("Django is not importable in the target interpreter", allow_module_level=True)

from guardlib import audit, ci, ledger, migration_drift, symbols, waivers  # noqa: E402
from runner import diff as differ  # noqa: E402

SETTINGS = """\
from pathlib import Path
SECRET_KEY = "x"
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "shop"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(Path(__file__).resolve().parent / "db.sqlite3")}}
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
"""
MODELS = """\
from django.db import models


class Item(models.Model):
    name = models.CharField(max_length=50)
"""
INITIAL = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(name="Item", fields=[
            ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("name", models.CharField(max_length=50)),
        ]),
    ]
"""
PRICE = '    price = models.IntegerField(default=0)\n'
ADD_PRICE = """\
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [migrations.AddField(model_name="item", name="price", field=models.IntegerField(default=0))]
"""
LEAF = 'from django.db import migrations\n\n\nclass Migration(migrations.Migration):\n    dependencies = [("{app}", "0001_initial")]\n    operations = []\n'


def files(models=MODELS, extra=None, settings=SETTINGS):
    out = {"settings.py": settings, "shop/__init__.py": "", "shop/models.py": models, "shop/migrations/__init__.py": "",
           "shop/migrations/0001_initial.py": INITIAL, "app.py": "def f(x):\n    return x\n"}
    out.update(extra or {})
    return out


def _cfg(tmp_path, make_cfg, base_files, work_files, **overrides):
    root = write_tree(tmp_path / "proj", work_files)
    cfg = make_cfg(root, django_settings="settings", **overrides)
    base = write_tree(cfg.state_dir / "baseline", base_files)
    return cfg, base, root


def _items(outcome):
    return sorted(b.item for _, b in outcome.breaks)


# 16
def test_field_without_migration_flagged_with_no_database(tmp_path, make_cfg):
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    out = migration_drift.compute(cfg, str(base), str(root))
    assert out.state == "checked" and _items(out) == ["drift:shop:Add field price to item"]
    (file, b), = out.breaks
    assert file == "shop/models.py" and b.kind == "db" and b.source == "makemigrations" and b.contract == "shop/models.py"
    assert b.subject == "model change without a migration: shop: Add field price to item"
    raw = migration_drift.run_tree(cfg, str(root), 60)
    assert raw["blocks"] == [] and raw["error"] is None and raw["exit"] == 1
    assert not (root / "db.sqlite3").exists() and not (base / "db.sqlite3").exists()
    assert not (root / "shop/migrations/0002_item_price.py").exists()


# 17
def test_matching_migration_means_no_drift(tmp_path, make_cfg):
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE, {"shop/migrations/0002_item_price.py": ADD_PRICE}))
    out = migration_drift.compute(cfg, str(base), str(root))
    assert out.state == "checked" and out.breaks == []


# 18
def test_only_new_drift(tmp_path, make_cfg):
    old = MODELS + "    stock = models.IntegerField(default=1)\n"
    cfg, base, root = _cfg(tmp_path, make_cfg, files(old), files(old))
    assert migration_drift.compute(cfg, str(base), str(root)).breaks == []
    (root / "shop/models.py").write_text(old + PRICE)
    assert _items(migration_drift.compute(cfg, str(base), str(root))) == ["drift:shop:Add field price to item"]


# 19
def test_non_null_without_default(tmp_path, make_cfg):
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + "    price = models.IntegerField()\n"))
    out = migration_drift.compute(cfg, str(base), str(root))
    assert _items(out) == ["drift:shop:Add field price to item", "drift:shop:needs-default:item.price"]
    raw = migration_drift.run_tree(cfg, str(root), 60)
    assert raw["exit"] == 1 and raw["asked"] == [{"kind": "not_null", "model": "item", "field": "price", "app": "shop"}]


# 20
def test_baseline_exit3_field_does_not_hide_new_drift(tmp_path, make_cfg):
    stuck = MODELS + "    price = models.IntegerField()\n"
    cfg, base, root = _cfg(tmp_path, make_cfg, files(stuck), files(stuck + '    colour = models.CharField(max_length=10, default="red")\n'))
    out = migration_drift.compute(cfg, str(base), str(root))
    assert _items(out) == ["drift:shop:Add field colour to item"]


# 21
def test_baseline_conflict_does_not_hide_new_drift(tmp_path, make_cfg):
    leaves = {"shop/migrations/0002_a.py": LEAF.format(app="shop"), "shop/migrations/0002_b.py": LEAF.format(app="shop")}
    cfg, base, root = _cfg(tmp_path, make_cfg, files(extra=leaves), files(MODELS + PRICE, leaves))
    out = migration_drift.compute(cfg, str(base), str(root))
    assert _items(out) == ["drift:shop:Add field price to item"]

    (root / "shop/migrations/0002_c.py").write_text(LEAF.format(app="shop"))
    out = migration_drift.compute(cfg, str(base), str(root))
    assert _items(out) == ["drift:shop:Add field price to item", "drift:shop:conflict"]
    conflict = next(b for _, b in out.breaks if b.item == "drift:shop:conflict")
    assert conflict.subject == "conflicting migrations in shop: 0002_a, 0002_b, 0002_c"
    assert migration_drift.code_hash(conflict) == symbols.sha("drift:shop:conflict|0002_a|0002_b|0002_c")

    (root / "shop/migrations/0002_c.py").unlink()
    blog = {"blog/__init__.py": "", "blog/models.py": "from django.db import models\n\n\nclass Post(models.Model):\n    pass\n",
            "blog/migrations/__init__.py": "",
            "blog/migrations/0001_initial.py": INITIAL.replace('"Item"', '"Post"').replace(
                '            ("name", models.CharField(max_length=50)),\n', ""),
            "blog/migrations/0002_a.py": LEAF.format(app="blog"), "blog/migrations/0002_b.py": LEAF.format(app="blog")}
    two_apps = SETTINGS.replace('"shop"]', '"shop", "blog"]')
    write_tree(root, {**blog, "settings.py": two_apps})
    write_tree(base, {k: v for k, v in blog.items() if "0002" not in k})
    write_tree(base, {"settings.py": two_apps})
    out = migration_drift.compute(cfg, str(base), str(root))
    assert _items(out) == ["drift:blog:conflict", "drift:shop:Add field price to item"]


# 22
def test_formatter_patch_without_naming_a_formatter(tmp_path, make_cfg):
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    marker = tmp_path / "formatter-ran"
    tool = tmp_path / "formatter"
    tool.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    tool.chmod(0o755)
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"root": str(root), "sys_path": [str(root)], "protect": [str(root)], "cwd": str(tmp_path),
                               "scratch": str(tmp_path / "scratch"), "django_settings": "settings", "deterministic": False,
                               "io": "stub"}))
    driver = f"""
import json, os, shutil, sys
shutil.which = lambda *a, **k: {str(tool)!r}
from django.core.management.utils import run_formatters
run_formatters([])
first = os.path.exists({str(marker)!r})
os.remove({str(marker)!r})
sys.path.insert(0, {str(PLUGIN / "runner")!r})
import lg_migrations
res = lg_migrations.main({str(req)!r})
sys.__stdout__.write("LGTEST" + json.dumps({{"first": first, "res": res, "again": os.path.exists({str(marker)!r})}}) + "\\n")
"""
    done = subprocess.run([TARGET_PY, "-B", "-c", driver], cwd=tmp_path, capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    line = next(x for x in done.stdout.splitlines() if x.startswith("LGTEST"))
    out = json.loads(line[len("LGTEST"):])
    assert out["first"] is True  # Django really calls the formatter at the end of a dry run
    res = out["res"]
    assert res["error"] is None and res["blocks"] == [] and res["changes"] == {"shop": ["Add field price to item"]}
    assert out["again"] is False


# 23
def test_settings_that_do_not_import(tmp_path, make_cfg):
    bad = files(MODELS + PRICE, settings='raise RuntimeError("settings exploded")\n')
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), bad)
    out = migration_drift.compute(cfg, str(base), str(root))
    assert out.state == "not_checked" and out.breaks == []
    assert "Django settings did not load" in out.note and "settings exploded" in out.note
    assert out.note_kind == "makemigrations-setup"


# 24
def _post_edit(monkeypatch, cfg, root):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import post_edit
    monkeypatch.setattr(post_edit, "load_guard", lambda event, fp=None: (str(root), cfg))
    with ledger.locked(cfg.state_dir) as data:
        data["baseline_ok"] = True
    return post_edit


def _spy_run_tree(monkeypatch, cfg):
    calls = []
    real = migration_drift.run_tree

    def spy(cfg_, tree, timeout):
        with open(cfg.state_dir / ".session.lock", "w") as fh:  # the ledger lock must be free while it runs
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        calls.append(tree)
        return real(cfg_, tree, timeout)
    monkeypatch.setattr(migration_drift, "run_tree", spy)
    return calls


def test_post_edit_models_only_gives_a_context_note(tmp_path, make_cfg, monkeypatch):
    from guardlib import engine
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    post_edit = _post_edit(monkeypatch, cfg, root)
    calls = _spy_run_tree(monkeypatch, cfg)
    monkeypatch.setattr(engine, "targets_for", lambda *a, **k: [])
    out = post_edit.handle({"tool_input": {"file_path": str(root / "shop/models.py")}, "cwd": str(root)})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("LegacyGuard: model changes without a migration (makemigrations --check --dry-run, no database): "
                          "model change without a migration: shop: Add field price to item")
    assert len(calls) == 2
    data = ledger.load(cfg.state_dir)
    assert data["migration_drift_base"]["result"]["changes"] == {}
    assert not any(f["kind"] == "contract_break" for f in data["findings"])  # no finding at PostToolUse (D2)
    post_edit.handle({"tool_input": {"file_path": str(root / "shop/models.py")}, "cwd": str(root)})
    assert len(calls) == 3  # the baseline result is cached for the session

    (root / "app.py").write_text("def f(x):\n    return x + 1\n")
    before = len(calls)
    post_edit.handle({"tool_input": {"file_path": str(root / "app.py")}, "cwd": str(root)})
    assert len(calls) == before


def test_post_edit_with_targets_note_and_wake(tmp_path, make_cfg, monkeypatch):
    from guardlib import engine
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    post_edit = _post_edit(monkeypatch, cfg, root)
    import _common
    target = differ.Target("shop/models.py", "f", "function", [], "h")

    def targets(cfg_, rel, data, skipped=None):
        skipped.append({"file": "x.py", "qualname": "X.run", "via_file": "shop/models.py", "reason": "r"})
        return [target]
    monkeypatch.setattr(engine, "targets_for", targets)
    monkeypatch.setattr(differ, "run", lambda *a, **k: [])
    monkeypatch.setattr(differ, "format_note", lambda results: "DIFF NOTE")
    monkeypatch.setattr(engine, "affected_tests", lambda *a, **k: [])
    out = post_edit.handle({"tool_input": {"file_path": str(root / "shop/models.py")}, "cwd": str(root)})
    ctx = out["hookSpecificOutput"]["additionalContext"]
    lines = ctx.split("\n")
    assert lines[0] == "DIFF NOTE" and lines[1].startswith("LegacyGuard: model changes without a migration") and lines[2].startswith("1 caller not run")

    monkeypatch.setattr(engine, "record", lambda cfg_, data, results: [({"id": "LG-9"}, None)])
    monkeypatch.setattr(differ, "format_wake", lambda r, fid: "WAKE TEXT")
    with pytest.raises(_common.Wake) as woke:
        post_edit.handle({"tool_input": {"file_path": str(root / "shop/models.py")}, "cwd": str(root)})
    parts = str(woke.value).split("\n\n")
    assert parts[0] == "WAKE TEXT" and parts[1].startswith("LegacyGuard: model changes without a migration") and parts[2].startswith("1 caller not run")


# 25 and 26 use a git repository
def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _git_session(tmp_path, make_cfg, extra=None):
    from guardlib import baseline
    root = write_tree(tmp_path / "proj", files(extra=extra))
    (root / ".gitignore").write_text(".legacyguard/\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    cfg = make_cfg(root, django_settings="settings")
    info = baseline.create(cfg)
    with ledger.locked(cfg.state_dir) as data:
        data.update(baseline_ok=True, baseline_commit=info["commit"], session_id="s")
    return cfg, root


def _audit(cfg):
    with ledger.locked(cfg.state_dir) as data:
        audit.run(cfg, data)
    return ledger.load(cfg.state_dir)


def _drift_findings(data):
    return [f for f in data["findings"] if f["kind"] == "contract_break" and f.get("source") == "makemigrations"]


TEST_FILE = "from app import f\n\n\ndef test_f():\n    assert f(1) == 1\n"


def test_stop_audit_findings(tmp_path, make_cfg, monkeypatch):
    cfg, root = _git_session(tmp_path, make_cfg, {"tests/test_app.py": TEST_FILE})
    (root / "shop/models.py").write_text(MODELS + PRICE)
    (root / "tests/test_app.py").write_text("from app import f\n\n\ndef test_f():\n    f(1)\n")
    data = _audit(cfg)
    (f,) = _drift_findings(data)
    assert f["file"] == "shop/models.py" and f["item"] == "drift:shop:Add field price to item" and f["state"] == "open"
    assert f["code_hash"] == symbols.sha(f["item"]) and f["contract_kind"] == "db"
    tests_break = next(x for x in data["findings"] if x["kind"] == "contract_break" and x["source"] == "tests")
    assert data["contract_tools"]["makemigrations"]["runs"] == 2

    data = _audit(cfg)
    assert [(x["id"], x["state"]) for x in _drift_findings(data)] == [(f["id"], "open")]
    assert data["contract_tools"]["makemigrations"]["runs"] == 3  # baseline cached

    monkeypatch.setattr(migration_drift, "run_tree", lambda *a, **k: None)  # a failed run changes nothing
    data = _audit(cfg)
    assert [x["state"] for x in _drift_findings(data)] == ["open"]
    assert next(x for x in data["findings"] if x["id"] == tests_break["id"])["state"] == "open"
    monkeypatch.undo()

    (root / "shop/migrations/0002_item_price.py").write_text(ADD_PRICE)
    data = _audit(cfg)
    assert [x["state"] for x in _drift_findings(data)] == ["not_applied"]


def test_stop_waiver_survives_unrelated_edit(tmp_path, make_cfg):
    cfg, root = _git_session(tmp_path, make_cfg)
    (root / "shop/models.py").write_text(MODELS + PRICE)
    data = _audit(cfg)
    (f,) = _drift_findings(data)
    waivers.add(cfg, f, "intended; migration comes in the next PR")
    (root / "shop/models.py").write_text("# an unrelated comment\n" + MODELS + PRICE)
    with ledger.locked(cfg.state_dir) as d:
        d["findings"] = []
    data = _audit(cfg)
    assert [x["state"] for x in _drift_findings(data)] == ["waived"]


# 26
def test_ci_reports_drift_and_honours_waivers(tmp_path, monkeypatch):
    root = write_tree(tmp_path / "proj", files())
    (root / ".gitignore").write_text(".legacyguard/\n")
    (root / ".legacyguard").mkdir()
    (root / ".legacyguard/config.yaml").write_text("django_settings: settings\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (root / "shop/models.py").write_text(MODELS + PRICE)
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    drift = [(f, b) for f, b in out.breaks if b.source == "makemigrations"]
    assert [(f, b.item) for f, b in drift] == [("shop/models.py", "drift:shop:Add field price to item")]
    assert any(n.startswith("Model drift check") for n in out.notes)
    assert any(a.startswith("::warning file=shop/models.py,title=LegacyGuard contract::model change without a migration")
               for a in ci.annotations(cfg, out))
    assert "`shop/models.py`: model change without a migration: shop: Add field price to item" in ci.report(cfg, out)

    (root / ".legacyguard/waivers.json").write_text(json.dumps([{"kind": "contract_break", "file": "shop/models.py",
                                                                 "item": drift[0][1].item, "code_hash": symbols.sha(drift[0][1].item)}]))
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    assert not [b for _, b in out.breaks if b.source == "makemigrations"]

    (root / ".legacyguard/waivers.json").unlink()
    monkeypatch.setattr(migration_drift, "run_tree", lambda *a, **k: None)
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, index=False)
    assert not [b for _, b in out.breaks if b.source == "makemigrations"]
    assert any("migration check failed inside LegacyGuard" in n for n in out.notes)


def test_ci_third_party_app_annotation_has_no_file(tmp_path, make_cfg):
    cfg = make_cfg(tmp_path)
    b = migration_drift.DriftBreak("db", "drift:auth:Add field x to user", "model change without a migration: auth: Add field x to user",
                                   "makemigrations", "django-app:auth")
    out = ci.Outcome(base="abc", breaks=[("django-app:auth", b)])
    (line,) = ci.annotations(cfg, out)
    assert line.startswith("::warning title=LegacyGuard contract::django-app:auth: model change") and "file=" not in line
    assert "- `django-app:auth`: model change without a migration" in ci.report(cfg, out)


# 27
def test_baseline_cache_key(tmp_path, make_cfg):
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    write_tree(root, {"settings2.py": SETTINGS})
    write_tree(base, {"settings2.py": SETTINGS})
    data = {"baseline_commit": "c1"}
    first = migration_drift.compute(cfg, str(base), str(root), {}, data=data)
    assert first.runs == 2 and first.base_result
    cached = first.base_result
    assert migration_drift.compute(cfg, str(base), str(root), cached, data=data).runs == 1

    cfg.raw["django_settings"] = "settings2"
    assert migration_drift.compute(cfg, str(base), str(root), cached, data=data).runs == 2
    cfg.raw["django_settings"] = "settings"
    cfg.raw["behavioral_diff"]["io"] = "block"
    assert migration_drift.compute(cfg, str(base), str(root), cached, data=data).runs == 2
    cfg.raw["behavioral_diff"]["io"] = "stub"
    assert migration_drift.compute(cfg, str(base), str(root), cached, data=data).runs == 1

    other_version = json.loads(json.dumps(cached))
    other_version["result"]["django"] = "0.0"
    assert migration_drift.compute(cfg, str(base), str(root), other_version, data=data).runs == 2
    incomplete = json.loads(json.dumps(cached))
    incomplete["result"]["error"] = "setup"
    again = migration_drift.compute(cfg, str(base), str(root), incomplete, data=data)
    assert again.runs == 2 and _items(again) == ["drift:shop:Add field price to item"]


# 28
def test_timing_per_tree(tmp_path, make_cfg):
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    for tree in (base, root):
        started = time.perf_counter()
        res = migration_drift.run_tree(cfg, str(tree), 60)
        took = time.perf_counter() - started
        print(f"makemigrations --check on {tree.name}: {took:.3f} s (Django {res['django']}, inside {res['seconds']:.3f} s)")
        assert took < 10


def test_applies_rules(tmp_path, make_cfg):
    cfg = make_cfg(tmp_path, django_settings="settings")
    assert migration_drift.applies(cfg, "shop/models.py", "")
    assert migration_drift.applies(cfg, "shop/models/item.py", "")
    assert migration_drift.applies(cfg, "shop/migrations/0002_x.py", "")
    assert migration_drift.applies(cfg, "shop/things.py", "from django.db import models\n")
    assert not migration_drift.applies(cfg, "shop/views.py", "def v(): pass\n")
    assert not migration_drift.applies(cfg, "shop/models.txt", "")
    cfg.raw["contract_tools"]["makemigrations"] = False
    assert not migration_drift.applies(cfg, "shop/models.py", "")
    cfg.raw["contract_tools"]["makemigrations"] = "auto"
    cfg.raw["django_settings"] = None
    assert not migration_drift.applies(cfg, "shop/models.py", "")


def test_without_the_patches_the_run_is_not_complete(tmp_path, make_cfg):
    """Acceptance 3: the patches are needed; unpatched, the command reaches the database and the worker stops it."""
    cfg, base, root = _cfg(tmp_path, make_cfg, files(), files(MODELS + PRICE))
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"root": str(root), "sys_path": [str(root)], "protect": [str(root)], "cwd": str(tmp_path),
                               "scratch": str(tmp_path / "scratch"), "django_settings": "settings", "deterministic": False,
                               "io": "stub"}))
    driver = f"""
import json, sys
sys.path.insert(0, {str(PLUGIN / "runner")!r})
import lg_migrations
lg_migrations.install_patches = lambda record: None
res = lg_migrations.main({str(req)!r})
sys.__stdout__.write("LGTEST" + json.dumps(res) + "\\n")
"""
    done = subprocess.run([TARGET_PY, "-B", "-c", driver], cwd=tmp_path, capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
    res = json.loads(next(x for x in done.stdout.splitlines() if x.startswith("LGTEST"))[len("LGTEST"):])
    assert not migration_drift.complete(res), res
    assert not (root / "db.sqlite3").exists()
