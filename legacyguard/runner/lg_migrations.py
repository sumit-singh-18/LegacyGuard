"""Django model drift: `makemigrations --check --dry-run` inside the worker's set-up (Sprint 7.6 Part A).

Runs in the TARGET project's interpreter: standard library only here; Django is imported only at run time, after
`lg_worker.prepare` has put the blocks, stubs, scratch folder and tree protection in place. The guard side
(`guardlib/migration_drift.py`) starts it under the OS sandbox as `[py, "-B", lg_migrations.py, request.json]` and reads
the one `LGMIG{...}` line it prints.

Four patches make the command safe and complete (`install_patches`):
- `MigrationLoader.check_consistent_history` does nothing: it is the only place makemigrations opens a database
  connection (it reads the applied migrations). Without it the run creates a sqlite file, or is refused by the worker.
- `makemigrations.run_formatters` does nothing: Django calls it at the end of every dry run, and it starts a formatter
  process when one is on PATH, which the worker refuses.
- `NonInteractiveMigrationQuestioner.ask_not_null_addition` and `.ask_auto_now_add_addition` record the question and
  answer `None` (a placeholder default) instead of `sys.exit(3)`, so detection goes on past the first such field. At
  verbosity 1 a dry run only describes operations; the placeholder is never written anywhere.
- `MigrationLoader.detect_conflicts` records the conflicting leaves per app and answers `{}`, so makemigrations does not
  raise `CommandError` before the autodetector runs.

Decisions come from the recorded fields (`changes`, `asked`, `conflicts`), never from the exit code. A run is complete
only when `error` is None and nothing was blocked.

Limits: rename questions answer "no" in non-interactive mode, so a renamed field shows as a remove plus an add. The
settings must import inside the worker (stubs on, network and database blocked) and the database driver must be
importable, else the run reports a set-up error.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

RUNNER_DIR = os.path.dirname(os.path.abspath(__file__))
MARK = "LGMIG"


def install_patches(record: dict) -> None:
    """Patch Django in this process so makemigrations never touches a database, never starts a process and always
    runs detection to the end. Each patch only when the attribute exists (older Django)."""
    record.setdefault("asked", [])
    record.setdefault("conflicts", {})
    record.setdefault("changes", {})
    record.setdefault("apps", {})
    from django.db.migrations import loader as loader_mod
    from django.db.migrations import questioner as questioner_mod
    from django.core.management.commands import makemigrations as mm

    Loader = loader_mod.MigrationLoader
    if hasattr(Loader, "check_consistent_history"):
        Loader.check_consistent_history = lambda self, connection: None
    if hasattr(mm, "run_formatters"):
        mm.run_formatters = lambda *a, **k: None

    Q = getattr(questioner_mod, "NonInteractiveMigrationQuestioner", None)
    if Q is not None:
        def asker(kind):
            def ask(self, field_name, model_name):
                record["asked"].append({"kind": kind, "model": model_name, "field": field_name})
                return None
            return ask
        if hasattr(Q, "ask_not_null_addition"):
            Q.ask_not_null_addition = asker("not_null")
        if hasattr(Q, "ask_auto_now_add_addition"):
            Q.ask_auto_now_add_addition = asker("auto_now_add")

    if hasattr(Loader, "detect_conflicts"):
        original_conflicts = Loader.detect_conflicts

        def detect_conflicts(self):
            found = original_conflicts(self) or {}
            for app, leaves in found.items():
                record["conflicts"][app] = sorted(str(x) for x in leaves)
            return {}
        Loader.detect_conflicts = detect_conflicts

    original_write = mm.Command.write_migration_files

    def write_migration_files(self, changes, *args, **kwargs):
        for app_label, migrations in (changes or {}).items():
            ops = record["changes"].setdefault(app_label, [])
            for m in migrations:
                for op in getattr(m, "operations", []):
                    try:
                        ops.append(str(op.describe()))
                    except Exception:  # noqa: BLE001 - a custom operation without describe()
                        ops.append(type(op).__name__)
        return original_write(self, changes, *args, **kwargs)
    mm.Command.write_migration_files = write_migration_files


def _app_paths(record: dict) -> None:
    from django.apps import apps
    labels = set(record["changes"]) | set(record["conflicts"]) | {a.get("app") for a in record["asked"] if a.get("app")}
    for label in labels:
        try:
            cfg = apps.get_app_config(label)
        except Exception:  # noqa: BLE001
            continue
        record["apps"][label] = {"path": cfg.path, "models_file": getattr(cfg.models_module, "__file__", None)}


def _asked_apps(record: dict) -> None:
    """Questions name the model, not the app: give each one the app whose models include it."""
    try:
        from django.apps import apps
    except Exception:  # noqa: BLE001
        return
    for a in record["asked"]:
        model = str(a.get("model") or "").lower()
        for cfg in apps.get_app_configs():
            if any(m._meta.model_name == model for m in cfg.get_models()):
                a["app"] = cfg.label
                break


def main(request_path: str) -> dict:
    started = time.time()
    with open(request_path) as fh:
        request = json.load(fh)
    if RUNNER_DIR not in sys.path:
        sys.path.insert(0, RUNNER_DIR)
    import lg_worker
    out = {"changes": {}, "asked": [], "conflicts": {}, "apps": {}, "exit": None, "error": None, "blocks": [],
           "django": None, "seconds": 0.0}
    _root_real, err = lg_worker.prepare(request, with_django=True)
    if err:
        out.update(error="setup", detail=err, blocks=list(lg_worker.BLOCKS), seconds=round(time.time() - started, 3))
        return out
    buf = io.StringIO()
    try:
        with lg_worker.setup_phase():
            import django
            from django.core.management import call_command
            out["django"] = django.get_version()
            install_patches(out)
        with lg_worker.setup_phase():
            call_command("makemigrations", check=True, dry_run=True, interactive=False, verbosity=1, stdout=buf, stderr=buf)
        out["exit"] = 0
    except SystemExit as exc:
        out["exit"] = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if out["exit"] not in (0, 1):
            out["error"] = f"makemigrations stopped with exit code {out['exit']}: {buf.getvalue().strip()[-300:]}"
    except BaseException as exc:  # noqa: BLE001 - CommandError, GuardBlocked and anything else: the run is not complete
        out["error"] = f"{type(exc).__name__}: {exc}"[:400]
    try:
        _asked_apps(out)
        _app_paths(out)
    except BaseException:  # noqa: BLE001 - paths only name files; the items still stand
        pass
    out["blocks"] = list(lg_worker.BLOCKS)
    out["seconds"] = round(time.time() - started, 3)
    return out


if __name__ == "__main__":
    try:
        result = main(sys.argv[1])
    except BaseException as exc:  # noqa: BLE001 - always print one line the guard can read
        result = {"error": f"{type(exc).__name__}: {exc}"[:400], "changes": {}, "asked": [], "conflicts": {}, "apps": {},
                  "blocks": [], "exit": None, "django": None}
    sys.__stdout__.write(MARK + json.dumps(result, default=str) + "\n")
    sys.__stdout__.flush()
    try:
        import lg_worker
        lg_worker.drop_scratch()
    except BaseException:  # noqa: BLE001
        pass
