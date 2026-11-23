# Sprint 7.6 build spec, Part A: contract tools (oasdiff and Django `makemigrations --check`)

Researcher agent, 2026-11-22, main at c3fe2f0. **Revised after reviewer REVISE (rounds 1 and 2); reviewer PASS in round 3; built as Sprint 7.6 Part A.**
Self-contained: the implementer should not need `docs/research/2026-11-sprint7.md`. Spec origin: that file's row 7.6
(unreviewed), `docs/ROADMAP.md` C5 and 3d ("contract tools (7.6) ... split into oasdiff and `makemigrations --check`
first, squawk and buf optional. Unverified research claims (... oasdiff JSON fields ...) must be confirmed before
building").

**Round-2 corrections (reviewer, 2026-11-22) are folded in and override earlier text:** the pre-edit skip counter is
reset only at SessionStart (`refresh(cfg, reset_counters=...)`, 5.1 and 5.3 agree; test 13 covers the Stop path);
one `scratch.for_trees` block per Stop for drift and the diff; `audit.run(log=...)` is the logging route; the oasdiff
sandbox lists `<tmp>` as writable only; third-party drift is annotated without `file=` in CI; section 9 has the
switched-off sweep and the waiver-key wording; gap 2.4 narrowed to 6.0; section 2.3 line numbers fixed.

**Round-1 corrections (reviewer, 2026-11-22) are folded in and override earlier text:** drift Django stops checking
early (exit 3, conflicts) is now recorded and detection continues (6.1, measured); conflicts are one item per app;
the Stop-audit order, finding `file`, code_hash and failure handling are defined (6.4); oasdiff runs under the OS
sandbox whenever it may read files beyond the spec (5.1 step 3); every tool and drift path fails open (5.2, 6.4); the
pre-edit skip rule is concrete and acceptance criterion 1 matches its tests (5.3, 8); the baseline cache key is
complete (6.2); the PostToolUse placement and output per branch are exact (6.4); CI output is defined (6.4); a missing
oasdiff `id` is accepted; the formatter test needs no tool name (7.4 test 20), so no decision D7 is needed.

Every claim from the earlier research was re-checked against primary sources. **Four of them were wrong or
incomplete:**

| Earlier claim (sprint7.md row 7.6) | What the sources say |
|---|---|
| "ERR is a Break, WARN is context" (implies string levels) | In `-f json` output, `level` is an **integer**: 3 = ERR, 2 = WARN, 1 = INFO (section 2.1). |
| "about 3 s" per oasdiff run | No source found for that number. Timing is **unverified** (no binary here, none downloaded). |
| "catch `SystemExit(1)`" | Exit 1 only when changes were found; **exit 3** at the first non-null or `auto_now_add` field without a default (detection stops there), exit 2 for unknown app labels, `CommandError` for conflicting leaves (before any detection) (section 2.2). |
| "patch `MigrationLoader.check_consistent_history` to a no-op" (and nothing else) | Also needed: Django calls `run_formatters([])` at the end of every dry run, which starts a formatter subprocess when one is on PATH; the worker refuses subprocesses, so the check dies with `GuardBlocked`. Patch `makemigrations.run_formatters` too; and patch the questioner and conflict detection so detection runs to the end (measured, section 3). |

**User decisions (2026-11-22, final):** D1 (a) oasdiff in PreToolUse with timeout and parser fallback, plus Stop and CI; D2 (b) context note at PostToolUse, finding at Stop; D3 yes, add `django>=4.2` to `requirements-dev.txt`; D4 no oasdiff in CI (fake binary only); D5, D6 and D8 as recommended. Reviewer PASS in round 3. Coordinator note on the round-3 optional item: `django-app:` drift annotations in CI use `::warning`, like every other contract break.

## 0. Summary

Part A adds `guardlib/contract_tools.py` (find a tool, cache what was found, run it, never raise) and two uses:

1. **oasdiff for the OpenAPI contract file** (`contracts.http`). `checks.check_http_file` calls
   `oasdiff breaking <base> <revision> -f json`, decides from the parsed JSON only, turns level-3 (ERR) changes into
   `Break`s and level-2 (WARN) changes into a context note. It stays in **PreToolUse**, under a hard per-run timeout
   (default 0.8 s); on any failure, exception, timeout or missing binary it falls back to today's parser
   (`contracts_parse.http_items`) with a note. The Stop audit and `ci.run` use the same code with a longer timeout.
   When the spec refers to sibling files, oasdiff runs inside the OS sandbox (no network, read-only tree) or not at all.
2. **Django model drift** (`makemigrations --check --dry-run`, non-interactive, no database). A new target-side script
   `runner/lg_migrations.py` runs it inside the worker's set-up (`lg_worker.prepare`: blocks, stubs, scratch, OS
   sandbox) with four patches (consistency check, formatter, two questioner methods, conflict detection) so it never
   touches a database, never starts a process and always runs detection to the end. It runs in the **PostToolUse**
   hook (it runs project code) when a models or migrations file was edited, in the **Stop** audit and in `ci.run`.
   Only drift that is new in the working tree (not in the baseline) is reported.

squawk, buf and code-first OpenAPI (`app.openapi()` in both trees) are sketched in section 10 and are not part of
this build.

## 1. Open decisions for the user (each with a recommendation)

| # | Decision | Recommendation |
|---|---|---|
| D1 | Where oasdiff runs. (a) PreToolUse with a hard timeout and fallback, plus Stop and CI; (b) only PostToolUse/Stop (can wake, cannot ask before the edit). | **(a)**. PreToolUse is the only hook that can ask or deny before a spec change lands (that is its job, `docs/ARCHITECTURE.md` line 23), oasdiff only reads two files, and the timeout plus fallback bound the cost. The tool runs only when the contract file itself is edited, so other edits pay nothing. If the implementer measures a median above 0.5 s on the test spec, report it and ask before shipping. |
| D2 | When drift is told to Claude. (a) Wake at PostToolUse; (b) context note at PostToolUse, a finding at Stop (blocks once, as other findings do); (c) Stop only. | **(b)**. Claude usually edits `models.py` and writes the migration in the next step, so a wake right after the model edit would mostly be noise. A context note costs nothing; the Stop audit catches drift still there at the end. |
| D3 | Add Django to `requirements-dev.txt` (dev venv and CI). | **Yes** (`django>=4.2`). There is precedent: FastAPI and Flask are there "only for testing" route diffs, and 7.5 (Django routes) needs it too. Without it every drift test is skipped everywhere. |
| D4 | Put oasdiff in CI. Options: none (fake binary only); one Linux job that runs `go install github.com/oasdiff/oasdiff@v1.39.0`; a pinned release download. | **None for now.** The fake-binary tests pin the documented JSON shape; the real-binary tests run wherever oasdiff is installed (skipped otherwise). Revisit after the reviewer looks at the shape pins. |
| D5 | A project's own `.oasdiff.*` config (severity levels, ignore files). oasdiff reads it from the cwd. | **Ignore it in Part A**: run oasdiff with a temp cwd and without `OASDIFF_CONFIG`, so results do not depend on a file Claude can edit. A later `contract_tools.oasdiff_args` key can opt in. |
| D6 | Finding kind for drift. (a) reuse `contract_break` with `contract_kind: "db"`, `source: "makemigrations"`; (b) a new kind. | **(a)**: waivers, the report section and the Stop logic already handle it. The audit must pass drift into `_contract_findings` before its `not_applied` sweep (section 6.4). |
| D8 | Spec files with sibling `$ref`s when no OS sandbox is available (Linux without bubblewrap, `os_sandbox: off`). (a) do not run oasdiff, parser with a note; (b) run it unsandboxed, trusting our `$ref` scan. | **(a)**. `ci.run` diffs pull-request specs, the untrusted case oasdiff's own docs warn about (2.1); the scan alone should not be the only guard. Specs with only internal refs are unaffected (they run with `--allow-external-refs=false`). |

(D7 from the review is not needed: test 20 avoids the formatter's name, see 7.4.)

## 2. Sources and confirmed claims

Every claim was read in the source given. "Inference" marks my own reasoning. oasdiff source files were read from
the tag (raw files at `https://raw.githubusercontent.com/oasdiff/oasdiff/v1.39.0/<path>`).

### 2.1 oasdiff

- **Current stable: v1.39.0**, published 2026-11-17 (v1.38.0 2026-11-14, v1.37.0 2026-11-08, v1.36.0 2026-11-07; none
  prerelease). — [GitHub releases API](https://api.github.com/repos/oasdiff/oasdiff/releases?per_page=8). Releases
  come several times a week, so pin the shape in tests, not the version (inference).
- **CLI.** `breaking base revision [flags]`: "Display breaking changes between base and revision specs"; base and
  revision "can be a path to a file, a URL, a git ref (e.g. main:openapi.yaml), or '-'" —
  [internal/breaking_changes.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/breaking_changes.go),
  [internal/handlers.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/handlers.go) (`specHelp`).
  Output format flag `--format`/`-f`, default `text`; values for the changelog output include `json`, `yaml`,
  `githubactions`, `junit`, `html`, `markdown`, `text`, `singleline` —
  [internal/cmd_flags.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/cmd_flags.go) line 102,
  [docs/BREAKING-CHANGES.md](https://github.com/oasdiff/oasdiff/blob/v1.39.0/docs/BREAKING-CHANGES.md).
  **Command to use:** `oasdiff breaking <base file> <revision file> -f json [--allow-external-refs=false]`.
- **What `breaking` reports:** `runBreakingChanges` calls `getChangelog(..., checker.WARN, true)`, i.e. changes at
  level WARN or higher — internal/breaking_changes.go;
  [internal/changelog.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/changelog.go). Levels per the doc:
  ERR "definite breaking changes which should be avoided", WARN potential breaks that "can't be confirmed
  programmatically", INFO "Non-breaking changes" — docs/BREAKING-CHANGES.md.
- **JSON shape.** `RenderChangelog` prints `NewChanges(...)` with `json.Marshal`; the bare array is the CLI shape
  (`adaptStructure` wraps it only for the service) —
  [formatters/format_json.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/formatters/format_json.go). An empty
  result prints `[]`, never `null` or nothing (`emptyDocFor`) — same file. Each element is `formatters.Change` —
  [formatters/changes.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/formatters/changes.go):
  `id`, `text`, `comment`, `disclaimers`, `sharedSchema`, `level`, `operation`, `operationId`, `path`, `section`,
  `attributes`, `baseSource`, `revisionSource`, `fingerprint`. **Only `level` is always present; every other field,
  `id` included, is `omitempty`.** `IsBreaking` is `json:"-"`, so the JSON has **no breaking flag**.
- **Level is an integer.** `type Level int`; `ERR = 3`, `WARN = 2`, `INFO = 1`, `NONE = 0`, `INVALID = -1`;
  `String()` gives "error"/"warning"/"info" but there is no `MarshalJSON` —
  [checker/rules/level.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/checker/rules/level.go); `checker.Level` is
  an alias — [checker/level.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/checker/level.go). I searched every
  non-test `.go` file in `checker/`, `checker/rules/` and `formatters/` for `MarshalJSON`/`MarshalYAML`/`MarshalText`:
  none. The project's own test expects `"level":3`
  ([formatters/format_json_test.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/formatters/format_json_test.go)
  lines 31 and 124), and the fingerprint doc's example shows `"level": 3` —
  [docs/FINGERPRINT.md](https://github.com/oasdiff/oasdiff/blob/v1.39.0/docs/FINGERPRINT.md).
  **How to tell breaking from not:** `level == 3` is a definite break; `level == 2` is a possible one; nothing else is
  printed by `breaking`. `IsBreaking()` is `ERR || WARN` (level.go).
- **Fingerprint:** "12-character hex", `SHA256("{id}:{operation}:{path}:{args}")[:12]`, "stable across commits", but
  can change when the base changes (arguments taken from the base, positions, renames) — docs/FINGERPRINT.md.
- **Exit codes** — [internal/errors.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/errors.go),
  [internal/run.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/run.go): 0 success; 1 only with
  `--fail-on` and a match (we never pass it); 100 general; 101 invalid flags; **102 "failed to load base/revision spec
  from ..."**; 103 glob load; 104 "diff failed"; 105 print failed; 106 severity file; 107 config file; 110 unsupported
  format; 121 ignore file; 122 flatten; **123 external `$ref` refused** (with `--allow-external-refs=false`).
- **Invalid spec:** the load fails with code 102 and the message "failed to load %s spec from %s: %w"
  (errors.go `getErrFailedToLoadSpec`); `breaking` does not run `validate`, so a spec that loads but is not valid
  OpenAPI is still diffed (inference from reading changelog.go: `calcDiff` then the checks, no validation step). The
  error goes to stderr through cobra (inference: `SilenceErrors` is not set in run.go); stdout gets nothing, so JSON
  parsing fails and we fall back.
- **External `$ref`s:** `--allow-external-refs` defaults to **true** (cmd_flags.go line 26), and "By default, oasdiff
  follows these external refs", URLs included; the doc's example is a cloud metadata URL, and it recommends `=false`
  or "a sandboxed environment without access to cloud metadata endpoints" for untrusted specs in CI. `=false` refuses
  them with exit 123, and the flag must be written with `=` —
  [docs/EXTERNAL-REFS.md](https://github.com/oasdiff/oasdiff/blob/v1.39.0/docs/EXTERNAL-REFS.md). With it false,
  "only the document the load was asked for may be read ... a sibling file included" —
  [load/ref_policy.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/load/ref_policy.go). **So a spec with sibling
  refs can only be diffed with external refs allowed**, which also allows URLs: hence the sandbox in 5.1 step 3.
- **Config file:** oasdiff reads `--config`, then `OASDIFF_CONFIG`, then `.oasdiff.{json,yaml,yml,toml,hcl}` or
  `oasdiff.*` **in the cwd** — [internal/viper.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/viper.go)
  lines 105–133.
- **`--open`** (off by default) "encrypt[s] the comparison and upload[s] it to oasdiff.com" — cmd_flags.go line 113.
  Only a flag or config file can turn it on; we pass neither and ignore project config (D5).
- **`oasdiff schema` exists** (group "Reference"): "Print a JSON Schema for the "--format json" output of the breaking
  and changelog commands", reflected from `formatters.Changes` with invopop/jsonschema and printed with
  `MarshalIndent` — [internal/schema.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/internal/schema.go),
  run.go. Inference: since `Level` is a Go `int`, the schema types `level` as `integer`; the exact `$defs` layout is
  unverified (not run).
- **Version string:** cobra's `Version = build.String()`; a release injects the tag without the "v"; a
  `go install ...@<tag>` build reports the module version —
  [build/build.go](https://github.com/oasdiff/oasdiff/blob/v1.39.0/build/build.go). Install routes: `go install
  github.com/oasdiff/oasdiff@latest`, `brew install oasdiff`, an install script, asdf, Docker `tufin/oasdiff` —
  [docs/README.md](https://github.com/oasdiff/oasdiff/blob/v1.39.0/docs/README.md) lines 17–53.
- **Timing: unverified.** oasdiff is not installed here (`which oasdiff`: not found) and nothing was downloaded. I found
  no published timing. Inference only: a Go binary loading two small specs should take well under 1 s, but specs with
  many files or large schemas are unknown.

### 2.2 Django `makemigrations`

Read on GitHub branches **stable/5.2.x, stable/6.0.x, stable/6.1.x** (current LTS and the two supported feature
releases) and **stable/4.1.x** (to date the `--check` change), plus a local Django **5.2.19** install (section 3).
Supported series per [djangoproject.com/download](https://www.djangoproject.com/download/): 5.2 LTS (5.2.19, extended
support to April 2028), 6.0 (6.0.10), 6.1 (6.1.3, the latest).

- **`--check`**: `dest="check_changes"`, help "Exit with a non-zero status if model changes are missing migrations and
  don't actually write them. Implies --dry-run."; `handle()` sets `self.dry_run = True` when `check_changes` —
  [makemigrations.py @ stable/5.2.x](https://github.com/django/django/blob/stable/5.2.x/django/core/management/commands/makemigrations.py),
  same in [stable/6.0.x](https://github.com/django/django/blob/stable/6.0.x/django/core/management/commands/makemigrations.py)
  and [stable/6.1.x](https://github.com/django/django/blob/stable/6.1.x/django/core/management/commands/makemigrations.py).
  On **stable/4.1.x** `--check` does **not** imply dry run and the files are written
  ([4.1.x](https://github.com/django/django/blob/stable/4.1.x/django/core/management/commands/makemigrations.py)); the
  4.2 release notes, "Backwards incompatible changes": "The makemigrations --check option no longer creates missing
  migration files" — [Django 4.2 release notes](https://docs.djangoproject.com/en/5.2/releases/4.2/). **So always pass
  `dry_run=True` as well.**
- **`call_command("makemigrations", check=True, dry_run=True, interactive=False)`**: `call_command` maps each option's
  shortest flag name to its dest (`opt_mapping`), so `check=True` reaches `check_changes`; it defaults
  `skip_checks=True` (no system checks) — `django/core/management/__init__.py` (local 5.2.19, lines 123–128 and
  194–195). `interactive=False` selects `NonInteractiveMigrationQuestioner` (makemigrations.py `handle`).
- **Exit behaviour (5.2, 6.0, 6.1 identical):** no changes: returns normally (prints "No changes detected"). Changes:
  `write_migration_files(changes)` (dry run: describes, writes nothing) then `if check_changes: sys.exit(1)`. Unknown
  app labels: `sys.exit(2)`. Conflicting leaf migrations: `loader.detect_conflicts()` then `raise
  CommandError("Conflicting migrations detected ...")` **before the autodetector runs** (6.1.x lines 167–188).
  Non-interactive questioner: `ask_not_null_addition` and `ask_auto_now_add_addition` log "Field '<f>' on model '<m>'
  not migrated: ..." and call **`sys.exit(3)`** (6.1.x lines 321–329 and 339–347), which **ends detection at the first
  such field**; `ask_not_null_alteration` returns `NOT_PROVIDED` (no exit); rename questions return `False` (so a
  rename shows as remove plus add) —
  [questioner.py @ stable/6.1.x](https://github.com/django/django/blob/stable/6.1.x/django/db/migrations/questioner.py).
  Docs: "If a suppressed prompt cannot be resolved automatically, the command will exit with error code 3" —
  [django-admin (5.2)](https://docs.djangoproject.com/en/5.2/ref/django-admin/).
  **Consequence:** without patches, a baseline that already has such a field or a conflict stops on the same item in
  the working tree, and any new drift after it is never seen. 6.1 patches both (measured in section 3).
- **`MigrationLoader.check_consistent_history(connection)`** builds a `MigrationRecorder(connection)` and reads
  `applied_migrations()`: **it needs a database connection** —
  [loader.py @ stable/6.1.x](https://github.com/django/django/blob/stable/6.1.x/django/db/migrations/loader.py).
  makemigrations calls it for the default alias (every alias when `DATABASE_ROUTERS` is set) unless the engine is
  `django.db.backends.dummy`, and catches only `OperationalError` (as a `RuntimeWarning`) — makemigrations.py on all
  three branches.
- **Does makemigrations touch the database?** Only there. The graph is built with `MigrationLoader(None,
  ignore_no_migrations=True)`, and with `connection is None` "self.applied_migrations = {}" (no recorder) — loader.py.
  `--update` (`write_to_last_migration_files`) and `--merge` use connections too; we pass neither. Measured: an
  unpatched run on a sqlite project **creates the database file** (section 3).
- **Conflicts per app:** `detect_conflicts()` returns `{app_label: sorted(conflicting leaf names)}` — loader.py.
- **Formatter subprocess:** `write_migration_files` ends with `run_formatters(self.written_files, stderr=...)` even in a
  dry run (an empty list). `run_formatters` looks up a formatter with `shutil.which` and, when found, runs
  `subprocess.run([<formatter>, "--fast", "--", *written_files])`; it does **not** return early on an empty list —
  [utils.py @ stable/6.1.x](https://github.com/django/django/blob/stable/6.1.x/django/core/management/utils.py);
  makemigrations imports it by name (`from django.core.management.utils import run_formatters`), so patching the
  name in the `makemigrations` module works (measured). On 4.1.x it is called as `run_formatters(self.written_files)`.
- Load of migration modules: `load_disk` imports each app's migrations package and **reloads** it if already imported
  — loader.py. This is project code (migration files), so it must run in the sandboxed worker.

### 2.3 This repository (file:line at c3fe2f0)

- **PreToolUse** `scripts/pre_edit.py` is synchronous and decides the edit: `.legacyguard/` edits denied (87–88),
  frozen files denied (97–103), then `checks.run_checks` (114) and `policy.contract_decision` → ask/deny/context
  (123–146). It never runs project code. `_http_map` parses the contract with `cp.http_items` on every Python edit
  (30–36). Budget: "< 1 s, blocks the edit if needed" (`docs/ARCHITECTURE.md` line 23); "PreToolUse total ~1 s → < 1 s
  (hard 10 s timeout, fail-open)" (line 264); `hooks/hooks.json` line 11: `"timeout": 10`. The impact call notes
  "this hook has a 1 s budget" (pre_edit.py 55).
- **PostToolUse** `post_edit.py`: async with `asyncRewake`, `"timeout": 150` (hooks.json 14). Lines 25–32 are one
  `with ledger.locked(...)` block: it marks applied contract breaks, **returns inside the lock** when `baseline_ok` is
  unset or the file is not a guarded source (29–30), and computes `targets` (32). Line 33 returns when there are no
  targets; **a models-only edit (a new class attribute) usually has none.** Lines 38–56: the `scratch.for_trees` block,
  the diff, the newer-edit check (40–41), `engine.record` under a second short lock, wake texts, affected tests in a
  `try`/`log_error` (49–53), `Wake` (55–56); else a context note from `format_note` and the skipped note (59–60).
- **Stop** `stop_audit.py` → `audit.run` (timeout 240, hooks.json 17). `audit.run` (124–134) calls
  `_contract_findings` first (129). Inside it, `seen_items` is a local (37); each `run_checks` break is added to it
  (47) and found or added as a `contract_break` finding with `code_hash = sha(after)` (45, 53–56); at the end (57–59)
  every open/applied `contract_break` not in `seen_items` becomes `not_applied`.
- **Waivers:** `waivers.is_waived(cfg, kind, file, item, code_hash)` matches all four (waivers.py 21–23).
- **CI:** `ci.run` checks `changed + contract_files` inside one `scratch.for_trees` block; each break goes to
  `out.breaks` as `(rel, Break)` unless waived with `sha(after)` (ci.py 160–162); `Outcome` has no notes field
  (22–29); breaks are annotated (203) and listed in the comment (258–259). `backtest.py` 61 also calls `run_checks`.
- `checks.check_http_file(rel, before, after)` (checks.py 43–55) wraps `cp.http_items` (contracts_parse.py 28–43:
  routes, params, component fields only) and returns `[]` on any parse error (44–47). `run_checks` sends the file there
  only when `rel == cfg.contract_path("http")` (227–231).
- **Django support today:** `autosetup._django_settings` finds `DJANGO_SETTINGS_MODULE` in pytest config
  (`DJANGO_SETTINGS_MODULE =`, `--ds=`) then in `manage.py`, and writes `django_settings:` to the config
  (autosetup.py 15–20, 45–57, 87–88); `config.DEFAULTS` has `django_settings: None` (config.py 14). The worker's
  `setup_django` sets the env var and runs `django.setup()` under `setup_phase()` (lg_worker.py 2043–2054);
  `prepare(request, with_django=True)` installs blocks, stubs, determinism, paths, protection and cwd (2057–2081).
  `indexer/routes.py` 18–108 already runs a Django-aware script in a subprocess through `lg_worker.prepare` and
  `os_sandbox.wrap`: **the pattern to copy.** Django routes are not diffed (7.5).
- The worker refuses subprocesses (`subprocess.Popen.__init__`, `subprocess.run`, `os.system` patched; audit hook) and
  `sqlite3.connect` (lg_worker.py 73–123, 140–141); `GuardBlocked` is a `BaseException` (50–51), so Django's
  `except OperationalError` cannot catch it.
- **OS sandbox:** `os_sandbox.wrap(cmd, python, writable, readonly, mode)` wraps any command; macOS denies outbound
  network and file writes except the writable paths, and a later `readonly` deny wins over `writable` (`_macos`,
  os_sandbox.py 32–44), Linux bwrap uses `--unshare-net` and a read-only root, read-only binds after writable ones
  (`_linux`, 46–54); `available(python)` probes once per process by starting `python -c pass` in the sandbox (64–77).
- `warm._generation(cfg)` is the baseline folder's inode and mtime (warm.py 42–48).
- Dev and CI dependencies: `requirements-dev.txt` has pytest, fastapi, httpx, flask, requests (no Django);
  `legacyguard/requirements.txt` has tree-sitter, hypothesis, coverage, PyYAML, mcp. CI
  (`.github/workflows/tests.yml`) installs `requirements-dev.txt` on ubuntu/macos × 3.10/3.12/3.13. No `pyproject.toml`.
  Neither Django nor oasdiff is in `.venv-dev` or CI.

### 2.4 Not confirmed

- oasdiff timing (no binary). The `oasdiff schema` text itself (not run). Whether older oasdiff versions printed
  `level` as a string (only v1.39.0 read): the parser accepts both (section 5.1).
- How oasdiff resolves percent-encoded or otherwise URL-escaped file refs (kin-openapi treats refs as URLs; inference).
  Such a ref we cannot copy makes oasdiff fail to load, and we fall back.
- Django before 4.1 (EOL; not read). Django on a real project: `django.setup()` cost there is unknown (fixture: 0.1 s).
- A project whose `DATABASES` engine driver (e.g. psycopg) is not installed in the target interpreter: indexing
  `connections[alias]` for the engine test builds the backend wrapper, which imports the driver (inference from
  makemigrations.py). Such a project cannot run its own tests either; the run fails and we report "not checked" (6.3).
- Returning `None` from the two questioner methods on **6.0** only. On 5.2 and 6.1 the autodetector calls both the
  same way: inside `if not preserve_default`, `field.default = self.questioner.ask_auto_now_add_addition(...)` (time
  fields with `auto_now_add`) or `ask_not_null_addition(...)`, then `if field.unique and field.has_default() and
  callable(field.default)` asks about unique callables — `django/db/migrations/autodetector.py` (local 5.2.19 lines
  1145–1156; stable/5.2.x lines 1136–1152 and stable/6.1.x lines 1178–1198 as read by the reviewer). `callable(None)`
  is False, so a `None` default goes straight to `AddField`, as measured on 5.2.19. 6.0 was not read.

## 3. Measurements (this Mac, Python 3.12.16, Django 5.2.19 already installed in another local venv; nothing installed)

Fixture: settings module, apps `contenttypes`, `auth`, `shop`; `shop/models.py` with `Item(name)`;
`shop/migrations/0001_initial.py`. Scripts in the scratch folder (not in the repo). All worker runs with `-B`.

| Case | Exit | Recorded | DB file created | Time |
|---|---|---|---|---|
| plain process, no patch, no drift | normal (0) | none | **yes** | setup 0.44 s cold, then 0.11 s; command 0.005–0.013 s |
| plain, consistency + formatter patches, no drift | normal | none | no | 0.11 s + 0.005 s |
| plain, same patches, `price = IntegerField(default=0)` added | `SystemExit(1)` | `shop: ["Add field price to item"]` | no | 0.11 s + 0.006 s |
| plain, same patches, `IntegerField(null=False)` (no default) | `SystemExit(3)` | **nothing**; log "Field 'price' on model 'item' not migrated: it is impossible to add a non-nullable field without specifying a default." | no | same |
| **worker `prepare()`, io=stub, no patch** | `AttributeError: 'FakeConnection' object has no attribute 'create_function'` | none | no | — |
| **worker, io=block, no patch** | `GuardBlocked: database is blocked` | none | no | — |
| worker, consistency + formatter patches, drift | `SystemExit(1)` | `shop: ["Add field price to item"]`, `BLOCKS == []` | no | **0.14–0.16 s whole process** (`/usr/bin/time`) |
| worker, consistency patch only, a fake formatter on PATH | `GuardBlocked: subprocess is blocked` | (recorded before the block) | no | — |
| worker, both patches, fake formatter on PATH | `SystemExit(1)` | as above | no | — |
| **worker, all four patches (6.1)**: `price = IntegerField()`, `made = DateTimeField(auto_now_add=True)`, `colour = CharField(default="red")` | `SystemExit(1)` | `shop: ["Add field colour to item", "Add field made to item", "Add field price to item"]`; questions recorded `[auto_now_add item.made, not_null item.price]` | no | — |
| same at verbosity 1 | | stdout lists the three operations only; **no migration text is serialised**; no file written in `migrations/` | | |
| same at verbosity 3 (for comparison) | | the full migration text is printed (`made` with `default=None, preserve_default=False`) | | |
| **all four patches, two `0002_*` leaves in `shop`** | `SystemExit(1)` | conflicts `{"shop": ["0002_a", "0002_b"]}` **and** the three operations (detection continued) | no | — |
| same without the conflict patch | `CommandError: Conflicting migrations detected ...` | nothing | no | — |

Notes: operations were recorded by wrapping `Command.write_migration_files` and reading `op.describe()` for each
operation of each migration. A run without `-B` failed in set-up with "file write is blocked" (the bytecode cache
write into the project): the script must run with `-B` (as `indexer/routes.py` does) and `PYTHONDONTWRITEBYTECODE=1`
(`clean_env` sets it). sandbox-exec start-up overhead on this Mac: about 0.01 s (`sandbox-exec ... python3 -c pass`
0.03 s vs 0.02 s plain, best of 3).

## 4. Current code paths that change

| File | Today | Change |
|---|---|---|
| `guardlib/contract_tools.py` | — | new (5.1) |
| `guardlib/checks.py` 43–55, 227–242 | parser only | `check_http_file(..., tools=None)` with a catch-all fallback; `run_checks(..., trees=None, notes=None, tool_timeout=None)` (5.2) |
| `scripts/pre_edit.py` 114 | `run_checks(...)` | pass `trees=(root, root)`, a `notes` list, the pre-edit timeout; add notes to context (5.3) |
| `guardlib/audit.py` 34–59, 124–134 | parser only | `trees=(baseline_dir, root)`; drift computed in `run` before `_contract_findings` and passed in (6.4) |
| `guardlib/ci.py` 22–29, 160–162, 203, 258 | parser only | `trees=(base_dir, root)`; drift breaks; `Outcome.notes` (6.4) |
| `runner/lg_migrations.py` | — | new, target side, stdlib + Django at run time (6.1) |
| `guardlib/migration_drift.py` | — | new, guard side (6.2) |
| `scripts/post_edit.py` 25–60 | returns before any check when no targets | drift step between lines 32 and 33, outside the lock (6.4) |
| `scripts/session_start.py` | — | `contract_tools.refresh(cfg, reset_counters=True)` after config load |
| `guardlib/config.py` 13–22 | — | `contract_tools` defaults (5.1) |
| `guardlib/ledger.py` 29–33 | — | `tool_notes: []`, `migration_drift_base: None` in `fresh` |
| `guardlib/report.py` | Isolation line | "Contract tools" line (5.5) |

## 5. Build plan: `contract_tools` and oasdiff

### 5.1 New `guardlib/contract_tools.py` (guard side)

Config (`config.DEFAULTS`, merged like the other dicts):

```yaml
contract_tools:
  oasdiff: auto            # auto = shutil.which("oasdiff"); false = never; or a path (relative to the repo root)
  makemigrations: auto     # auto = when django_settings is set; false = off
  pre_edit_timeout_s: 0.8  # one oasdiff run inside PreToolUse
  timeout_s: 20            # Stop audit, ci.run, PostToolUse
```

API (**every public function catches `Exception` and returns None or a safe value; none raises**):

- `find(name, cfg) -> Tool | None`. `Tool(name, path, version: str | None, source: "config"|"path")`. Order: the
  config value (`False` → None, reason "switched off"; a path → it must exist and be executable, else None with reason
  "configured path ... not found"), else `shutil.which(name)`. **Never spawns a process.** The result goes into
  `cfg.state_dir / "tools.json"`:
  `{name: {path, source, config_value, version, st: [st_mtime_ns, st_size, st_ino], checked, last_seconds,
  last_error, pre_edit_timeouts}}`, written atomically (tmp + `os.replace`, as the ledger does). A cached entry is
  reused when `config_value` is unchanged and `os.stat(path)` still gives the same `st`; otherwise it is rebuilt
  (with `pre_edit_timeouts = 0`). `find_reason(name, cfg) -> str` gives the reason when None.
- `refresh(cfg, reset_counters: bool)`: re-resolves every entry and runs `<path> --version` (timeout 2 s) to fill
  `version` (cobra prints `oasdiff version <v>`; take the last word; inference from cobra's default template). Version
  is informational (report line), never a gate. **Callers:** SessionStart calls `refresh(cfg, reset_counters=True)`,
  the only call that sets `pre_edit_timeouts` to 0; the Stop audit and `ci.run` call `refresh(cfg,
  reset_counters=False)`, which **keeps** `pre_edit_timeouts` (an entry rebuilt because the binary's `st` or the
  config value changed still starts at 0, as in `find`). Stop runs at the end of every turn, so resetting there would
  let a slow oasdiff cost two pre-edit timeouts per turn instead of per session.
- `run(cmd, timeout, cwd, env=None) -> Completed | None`: `subprocess.run(cmd, capture_output=True, text=True,
  timeout=timeout, cwd=cwd, env=env)`; any `OSError`/`TimeoutExpired`/`ValueError` → None with the reason kept in
  `last_error` (a timeout is marked as such); returns `(returncode, stdout, stderr, seconds)`. Records `last_seconds`
  in tools.json (best effort; a write failure is ignored).
- **Rule: decide from parsed JSON, never from exit codes.** A run counts only when stdout parses as the expected JSON
  shape. Because we never pass `--fail-on`, a non-zero exit always means failure, so a non-zero exit is used only to
  *reject* a run (fall back), never to conclude anything about breaks.
- `oasdiff_breaking(cfg, rel, before, after, trees, timeout, specs=None) -> OasResult | Refusal | None` where
  `OasResult(errs: list[dict], warns: list[dict], seconds)` and `Refusal(reason)` (a deliberate non-run, with the
  note text). `specs` optionally carries the already-parsed `(before_dict, after_dict)` so each text is parsed once
  per hook (5.2). The whole body is inside `try: ... except Exception as exc: return Refusal(f"internal error:
  {type(exc).__name__}")` and the temp folder is removed in `finally`. Steps:
  0. **Skip** (Refusal, no note) when `before` is empty or None (a new contract file: there is nothing to diff, and
     oasdiff would fail with exit 102).
  1. **Materialise the two specs** in `tempfile.mkdtemp(prefix="lg-oas-")`: `<tmp>/base/<rel>` = `before`,
     `<tmp>/rev/<rel>` = `after` (keep the file name and extension; keep the repo-relative layout so relative refs
     resolve the same way).
  2. **`$ref` scan** of both specs and of every file copied for them (a recursive walk of the parsed YAML/JSON, depth
     capped at 200; anything unparseable → Refusal "could not read <file>"). For each string value of a `$ref` key:
     split at the first `#`; an empty prefix → internal; otherwise:
     - a scheme (`re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", ref)`, so `HTTP://`, `https:`, `file://`, `C:/` all count) →
       Refusal "the spec refers to <ref>; LegacyGuard never lets a tool read URLs, so the built-in parser was used";
     - an absolute path (`os.path.isabs`, or starting with `/` or `\\`) → Refusal "absolute $ref <ref>";
     - a relative file: resolved against the referring file's folder with `os.path.normpath`; it must stay inside the
       side's tree root after `realpath` (symlinks followed and checked), else Refusal; copied from that side's tree root
       (`trees[0]` for base, `trees[1]` for rev) to the same relative path under `<tmp>/base` or `<tmp>/rev` and scanned
       too. Bounds: `MAX_REF_FILES = 50`, `MAX_REF_BYTES = 5 MB` in all, else Refusal. A ref file that cannot be read
       (`OSError`, `UnicodeDecodeError`) → Refusal naming it. The edited file itself always comes from the in-memory text.
     `$ref` values that are not strings are ignored. The scan over-approximates on purpose: a `$ref` inside an
     `example` or `x-` extension counts too.
  3. **Command:** `[path, "breaking", base_file, rev_file, "-f", "json"]`. cwd = `<tmp>` (so no project `.oasdiff.*`
     is read, D5); env = `os.environ` minus `OASDIFF_CONFIG`.
     - **No file refs found:** add `--allow-external-refs=false`; run directly (oasdiff can then read only the two
       files, ref_policy.go). No sandbox, no probe cost.
     - **File refs found** (external refs must stay allowed, 2.1): run only inside the OS sandbox:
       `os_sandbox.wrap(cmd, sys.executable, writable=[<tmp>], readonly=[tree roots],
       mode=cfg.behavioral_diff["os_sandbox"])` (network denied on both platforms; the tree roots read-only; `<tmp>`
       is only in `writable`, since a path in both lists ends up read-only, 2.3; oasdiff writes nothing anyway). If
       `os_sandbox.active(sys.executable, mode)` is False → Refusal "the spec refers to other files and no OS sandbox
       is available here, so oasdiff was not run" (D8). This applies in every hook, PreToolUse included; the probe is
       one Python start per hook process (about 0.03 s measured, section 3), paid only for specs with file refs.
  4. **Parse:** `json.loads(stdout)` must be a list whose elements are all dicts, else reject the run ("oasdiff printed
     output LegacyGuard could not read"). For each element, `level` is read by `_level(v)`: an int as is; a string
     mapped `{"ERR","err","error"}→3`, `{"WARN","warn","warning"}→2`, `{"INFO","info"}→1` (defensive for other
     versions; inference); anything else → reject the whole run (`level` is the one field the struct never omits, 2.1).
     `id`, `text`, `operation`, `path`, `fingerprint` may be missing (`omitempty`): a missing value reads as `""`;
     an element with neither `id` nor `text` gets the text "a change oasdiff did not describe". `errs` = level 3,
     `warns` = level 2, others ignored.
  5. Remove `<tmp>` in `finally`.

### 5.2 `guardlib/checks.py`

- `check_http_file(rel, before, after, tools=None)`. `tools` is a small object `ToolContext(cfg, trees, timeout,
  notes: list[str], allow_oasdiff: bool)` or None. With None (backtest, unit tests): today's behaviour exactly.
  With a context, the **whole tool path** (find, scan, run, parse, mapping to `Break`s) is wrapped in
  `try ... except Exception` that logs (`_common.log_error` is not importable from guardlib, so append the reason to
  `notes` as "oasdiff step failed: <type>") and falls back to the parser. On success **its result replaces the
  parser's** for this file:
  - each ERR → `Break("http", item, subject, "oasdiff", rel)` with
    `item = f"oasdiff:{id or 'change'}:{operation} {path}:{fingerprint or sha12(id + operation + path + text)}"` and
    `subject = f"{operation} {path}: {text}"` (just `text` when operation/path are empty, e.g. a components change);
  - WARNs → one note: "oasdiff also reports N possible breaking changes it cannot confirm: <first 3 texts>";
  - when the tool is missing, refuses, fails, times out or raises: the parser as today, plus a note (5.5).
- **One parse per text:** add `contracts_parse.http_items_from(spec: dict)` and make `http_items(text)` call it. The
  tool path parses `before` and `after` once (`yaml.safe_load`; a parse error there means the parser would also return
  `[]`, so return `[]` with no tool run, as today 44–47) and hands the dicts to both the scan and the fallback.
- `run_checks(cfg, rel, before, after, http, read_migrations=None, trees=None, notes=None, tool_timeout=None,
  allow_oasdiff=True)`: builds the context only when `trees` is given and the file is the http contract. Other kinds
  unchanged.
- `_http_map` (pre_edit) and `audit._baseline_http` keep using `cp.http_items`: they only feed `check_python_source`
  schema names and must stay cheap.

### 5.3 `scripts/pre_edit.py` and the pre-edit skip rule

`notes = []`; `checks.run_checks(cfg, rel, current, after, _http_map(cfg), ..., trees=(root, root), notes=notes,
tool_timeout=cfg.contract_tools["pre_edit_timeout_s"], allow_oasdiff=contract_tools.pre_edit_allowed(cfg))`. Both
sides use the working tree for sibling `$ref` files (the edit changes only the contract file; the "before" text is
`current`). Notes join the existing `notes` list, so they go out as context on the no-break path and are appended to
`context` on the break paths.

**Skip rule (concrete):** the counter is `tools.json["oasdiff"]["pre_edit_timeouts"]`, one integer for the tool (no
per-file or size key). A pre-edit run that times out adds 1; a pre-edit run that returns within the timeout (whatever
its result) sets it to 0. `pre_edit_allowed(cfg)` is False when the counter is **≥ 2**; then pre_edit does not run
oasdiff and adds, once per session (`tool_notes` entry `"oasdiff-slow"`), "oasdiff took longer than 0.8 s here twice
in a row; the pre-edit check now uses the built-in parser, and the Stop audit still runs oasdiff". Stop and CI runs
neither read nor change the counter (their `refresh(cfg, reset_counters=False)` keeps it, 5.1). **Reset:** only
`refresh(cfg, reset_counters=True)` at SessionStart (so each session starts at 0) and a rebuild of the entry (the
binary's `st` or the `contract_tools.oasdiff` config value changed). So a slow oasdiff costs at most two pre-edit
timeouts per session per binary.

### 5.4 Stop audit and CI (oasdiff part)

- `audit._contract_findings`: `trees=(str(baseline.baseline_dir(cfg)), cfg.root)`, `tool_timeout=timeout_s`; notes go
  to the session report (5.5).
- `ci.run`: `trees=(str(base_dir), root)`, `tool_timeout=timeout_s`; notes go to `Outcome.notes` (new field), printed
  in the comment under the contract section.
- Waiver identity: oasdiff items differ from parser items, so a waiver made under the parser does not cover the same
  break once oasdiff is installed (asks again). Documented limit.

### 5.5 What the user sees

- **Missing tool:** once per session per tool, a context line the first time the tool would have run, e.g.
  "LegacyGuard: oasdiff was not found, so the OpenAPI check used its built-in parser (routes, parameters and schema
  fields only). Install oasdiff or set contract_tools.oasdiff in .legacyguard/config.yaml." Stored in session.json
  `tool_notes: ["oasdiff"]` (add to `ledger.fresh`). `oasdiff: false` → silent.
- **Refusal/failure/timeout:** the same context line with the reason ("oasdiff could not load the spec: <first stderr
  line, 200 chars>; the built-in parser was used"). Once per session per reason kind (`tool_notes` entries
  `"oasdiff-refused"`, `"oasdiff-failed"`, `"oasdiff-slow"`). A new contract file (step 0) gives no note.
- **Report:** a "Contract tools" line next to Isolation: "oasdiff 1.39.0 (/opt/homebrew/bin/oasdiff), last run 0.12 s;
  makemigrations --check: Django <version>, 3 runs" or "oasdiff not found".

## 6. Build plan: Django model drift

### 6.1 New `runner/lg_migrations.py` (target side; stdlib, imports Django only at run time)

Run as `[py, "-B", lg_migrations.py, request.json]` under `os_sandbox.wrap(..., writable=[tempfile.gettempdir(),
tmp], readonly=[tree])`, `env=clean_env(cfg)`, cwd = the tree's throwaway folder (`scratch.current(cfg, tree)`; 7.4
rule: never the real tree as a writable cwd). Request: `{root, sys_path, protect, cwd, cwd_root, scratch,
django_settings, deterministic: False, io}` (as `indexer/routes.py` 87–89). The module exposes
`install_patches(record) -> None` and `main(request_path)`, so tests can call `install_patches` directly. Steps:

1. `sys.path.insert(0, RUNNER_DIR)`; `import lg_worker`; `root_real, err = lg_worker.prepare(request, with_django=True)`.
   `err` → print `LGMIG{"error": "setup", "detail": err}` and exit 0.
2. `install_patches(record)`, each patch only if the attribute exists (older Django):
   - `MigrationLoader.check_consistent_history = lambda self, connection: None` (no database: 2.2);
   - `django.core.management.commands.makemigrations.run_formatters = lambda *a, **k: None` (no formatter process);
   - `NonInteractiveMigrationQuestioner.ask_not_null_addition` and `.ask_auto_now_add_addition` → append
     `{"kind": "not_null"|"auto_now_add", "model": model_name, "field": field_name}` to `record["asked"]` and
     **return `None`** (a placeholder default), so detection continues past them. Measured (section 3): at
     verbosity 1 the dry run only describes operations; the placeholder is never serialised and nothing is written;
   - `MigrationLoader.detect_conflicts` → call the original, store its `{app: [leaves]}` in `record["conflicts"]`,
     **return `{}`**, so makemigrations does not raise and detection continues (measured: the operations of the
     conflicted app are still recorded).
3. Record: wrap `makemigrations.Command.write_migration_files` to store
   `{app_label: [op.describe() for m in migrations for op in m.operations]}` in `record["changes"]` before calling the
   original (dry run: writes nothing, measured). Also record, for each app label seen in `changes`, `asked` or
   `conflicts`, `record["apps"][label] = {"path": app_config.path, "models_file": getattr(app_config.models_module,
   "__file__", None)}`.
4. `with lg_worker.setup_phase(): call_command("makemigrations", check=True, dry_run=True, interactive=False,
   verbosity=1, stdout=buf, stderr=buf)`; catch `SystemExit` (keep `code`), `CommandError` (keep text),
   `BaseException` (keep type and message; includes `GuardBlocked`) — the last two are errors.
5. Print one line `LGMIG` + JSON: `{changes, asked, conflicts, apps, exit, error, blocks: lg_worker.BLOCKS,
   django: django.get_version(), seconds}`. `lg_worker.drop_scratch()`.

**Decide from the JSON, never from the exit code**: drift items come from `changes`, `asked` and `conflicts`; the exit
code is only shown in notes. `complete = error is None and not blocks`. A run that is not complete is "not checked",
never drift, never "no drift".

### 6.2 New `guardlib/migration_drift.py` (guard side)

- `applies(cfg, rel, after_text) -> bool`: `contract_tools.makemigrations` not false, `django_settings` set, `rel` in
  scope, and one of: basename `models.py`; a path part `models` (a models package); a path part `migrations` with
  `.py`; or the text contains `models.Model` or `from django.db import models`.
- `run_tree(cfg, tree, timeout) -> dict | None`: the subprocess above, parsed; None on any failure (wrapped in
  `try/except Exception`).
- `items(result) -> dict[str, Item]` (key = item string), each `Item(app, item, subject)`:
  - `drift:<app>:<op description>` for each recorded operation;
  - `drift:<app>:needs-default:<model>.<field>` for each `asked` entry (subject "model field cannot be migrated
    without a default: <model>.<field> (<kind>)"); the operation for the same field is also in `changes` and is
    reported as its own item;
  - `drift:<app>:conflict` per app in `conflicts`, with the leaves kept in the Item; **one item per app**.
- **New drift:** `new = [i for k, i in work_items if k not in base_items]`, except conflicts: a conflict item counts
  as new when the app had no conflict in the baseline **or** its leaf set is not a subset of the baseline's leaves
  (a new leaf in an already conflicted app). Because every run now goes to the end (6.1), an exit-3 field or a
  conflict in the baseline no longer hides new drift.
- **Baseline cache:** session.json `migration_drift_base: {key, result}`. Key = JSON of `baseline_commit`,
  `warm._generation(cfg)`, `django_settings`, the realpath of `config.target_python(cfg)` with its `st_mtime_ns`,
  `behavioral_diff.io`, `source_roots`, `behavioral_diff.env`. The Django version is known only after a run, so it
  is checked after the working-tree run: if `work["django"] != cached["django"]`, the baseline is run again (and
  re-cached). A cached result that was not `complete` is never reused. `ci.run` does not cache.
- **Hedges:** baseline run not complete but working run complete → the working tree's drift is reported as context
  only, with "the baseline could not be checked (<reason>), so some of this may predate the session"; no finding.
  Working run not complete → "not checked" (6.3); nothing is reported, and findings are left unchanged (6.4).
- **Identity of a drift finding** (the same in Stop and CI):
  - `file` = the repo-relative path of `apps[app]["models_file"]` when it lies inside the tree (e.g.
    `shop/models.py` or `shop/models/__init__.py`); else `<app path>/migrations/` relative when inside the tree; else
    `"django-app:<app>"` (third-party apps);
  - `item` = the item string above;
  - `code_hash` = `symbols.sha(item + "|" + "|".join(sorted(leaves)))` for conflicts, `symbols.sha(item)` otherwise.
    The item itself names the drift, so a waiver holds while that exact drift exists and does not die on unrelated
    edits; it ends when the drift changes (a different operation is a different item).
- `compute(cfg, base_root, work_root, base_cached=None) -> DriftOutcome` with
  `DriftOutcome(state: "checked"|"not_checked"|"hedged", breaks: list[tuple[str, Break]], note: str | None,
  base_result: dict | None)`. `Break("db", item, subject, "makemigrations", file)`; subjects: "model change without a
  migration: shop: Add field price to item", "model field cannot be migrated without a default: item.price",
  "conflicting migrations in shop: 0002_a, 0002_b". The note says the check ran `makemigrations --check --dry-run`
  with no database, and its time. The whole function is inside `try/except Exception` → `not_checked`.

### 6.3 Not checked (no finding, one note)

Settings did not import (the `setup_django` text), Django not importable in the target interpreter ("Django did not
import in <python>; the migration check was skipped"), a timeout, `GuardBlocked` during the command (names what was
refused), a `CommandError`, unreadable output, an internal exception. Once per session per reason (`tool_notes`
entry `"makemigrations-<reason kind>"`).

### 6.4 Hooks

**PostToolUse** (`post_edit.py`), exact placement:

- Lines 25–32 stay as they are (one lock block; the early returns at 29–30 still happen inside it, so drift never runs
  without a baseline or for a file that is not a guarded source). Inside that block, also copy
  `base_cached = data.get("migration_drift_base")` and `tool_notes = list(data.get("tool_notes", []))` into locals.
- **Between lines 32 and 33, outside the lock:** `drift_wanted = migration_drift.applies(cfg, rel, text)`. Line 33
  becomes `if not targets and not drift_wanted: return None`.
- The `scratch.for_trees(...)` block (38) is opened when either is wanted. Inside it, **drift runs first** (when
  wanted), then the diff (when there are targets): `drift = migration_drift.compute(cfg, baseline, root, base_cached)`
  wrapped in `try/except Exception` → `log_error(root, f"migration drift: {type(exc).__name__}: {exc}")` and
  `drift = None`. No subprocess ever runs under `ledger.locked`.
- After the newer-edit check (40–41, applied to drift too), one short `with ledger.locked(...)` stores
  `drift.base_result` (when new and complete) and the `tool_notes` entries; it is merged with the existing
  `engine.record` lock block when the diff ran.
- `drift_text` = the new-drift lines ("Model changes without a migration (makemigrations --check, no database): ...;
  run makemigrations if these were intended") or the hedge or a not-checked note (once per session), or None.
- Output per branch:
  - **no targets:** `return hook_output("PostToolUse", context=drift_text) if drift_text else None`;
  - **targets, no wake:** `note = "\n".join(x for x in (differ.format_note(results), drift_text, skipped_text) if x)`;
  - **wake:** `texts` gains `drift_text` after the diff and affected-test texts, before `skipped_text`; the drift text
    never causes a wake by itself (D2).
- No finding is written at PostToolUse (D2 b).

**Stop** (`audit.run` 124–134), exact order:

1. `changed = baseline.changed_files(cfg)` (as today). `contract_tools.refresh(cfg, reset_counters=False)`.
2. **New:** one `with scratch.for_trees(cfg, str(baseline.baseline_dir(cfg)), root, session_cache=True):` block now
   wraps steps 2 to the end of `_behavior_findings` in `run` (the block moves out of `_behavior_findings`, audit.py 67,
   into `run`; `for_trees` is re-entrant, so the working tree is cloned at most once per Stop). Inside it: `drift =
   None`; if any `rel` in `changed` `applies` (reading the current text): `drift = migration_drift.compute(cfg,
   baseline_dir, cfg.root, data.get("migration_drift_base"))` inside `try/except Exception as exc` →
   `drift = DriftOutcome("not_checked", [], note, None)` and `log` the error. **Logging route:** `audit.run` gains a
   keyword `log=None` (a callable taking one string); `stop_audit.py`, which already imports `log_error` from
   `_common` (line 5), passes `log=lambda m: log_error(cfg.root, m)`; with `log=None` nothing is logged. Store
   `base_result` in `data`.
3. `_contract_findings(cfg, data, changed, drift=drift)` (new keyword). Inside, after the `run_checks` loop and
   **before the `not_applied` sweep (57–59)**:
   - `state == "checked"`: for each `(file, b)` in `drift.breaks`: add `(file, b.item)` to `seen_items`; find a known
     `contract_break` with that file and item (as 48–52: `not_applied` → `open`) or add one with
     `code_hash = migration_drift.code_hash(b)`, `waivers.is_waived(cfg, "contract_break", file, b.item, code_hash)`,
     `extra={"contract_kind": "db"}`, `source="makemigrations"`, evidence "found by makemigrations --check --dry-run
     (no database), new since the baseline".
   - `state in ("not_checked", "hedged")`: add every existing `contract_break` finding with `source ==
     "makemigrations"` in state open/applied to `seen_items`, so **a failed run leaves drift findings unchanged**.
     (Step 2 always yields a `DriftOutcome` when a changed file applies, so `drift is None` means no file applies.)
   - `drift is None` because no changed file applies (the models and migrations match the baseline again): drift
     findings are swept to `not_applied` as usual (correct: nothing can be new).
4. The rest of `run` unchanged. The drift note goes to the session report.

**CI** (`ci.run`): `contract_tools.refresh(cfg, reset_counters=False)` at the start. After the per-file loop, inside
the same `scratch.for_trees` block:
`drift = migration_drift.compute(cfg, str(base_dir), root, None)` when any of `changed + contract_files` `applies`
(no session data; no cache), in `try/except Exception` → `Outcome.notes`. For each `(file, b)` in `drift.breaks`:
`if not waivers.is_waived(cfg, "contract_break", file, b.item, migration_drift.code_hash(b)): out.breaks.append((file,
b))`, so annotations (203) and the comment (258) show them like other breaks; `fail_on == "any"` counts them (287).
The drift note and any hedge or not-checked note go to `out.notes`. `ci.annotations` writes a break whose file starts
with `django-app:` without `file=`/`line=` (`::error title=LegacyGuard: ...::<text>`), since GitHub cannot attach it
to a path; the comment lists it as `` `django-app:<app>` ``.

**PreToolUse: never** (it would run project code, and it would need the edit applied on disk).

### 6.5 Timing per check

Measured on the fixture: about 0.15 s per tree per run, worker blocks on (section 3). PostToolUse pays one
working-tree run per qualifying edit (the baseline is cached per session); Stop pays at most one. On a real project
the cost is dominated by `django.setup()` (app imports), unmeasured here: `docs/ARCHITECTURE.md` lists "PostToolUse,
Django app 15–60 s" for the whole diff today. The implementer records the fixture numbers in `docs/PROGRESS.md`.

## 7. Tests

Run one file at a time (`.venv-dev/bin/python -m pytest -q tests/<file>`), never the suites in parallel.

### 7.1 `tests/test_contract_tools.py` (no real binary needed)

A **fake oasdiff**: a Python script written to `tmp_path/bin/oasdiff` with shebang `#!{sys.executable}`, mode 0o755.
It appends its argv and cwd as one JSON line to `tmp_path/calls.jsonl`, then behaves per a side file:
`ok` prints the pinned sample below and exits 0; `fail` prints "Error: failed to load revision spec from ..." to
stderr and exits 102; `garbage` prints `not json`; `object` prints `{}`; `string-levels` prints the sample with
`"level": "error"`; `no-id` prints the sample with `id` removed from the first element; `slow` sleeps 3 s. Pinned
sample (fields and types from `formatters/changes.go` and `format_json_test.go` at v1.39.0; the ids are illustrative,
the tests must not depend on real rule ids):

```json
[{"id":"api-path-removed-without-deprecation","text":"api path removed without deprecation","level":3,"operation":"GET","path":"/pets","section":"paths","fingerprint":"80a8c624dc40"},
 {"id":"response-property-became-optional","text":"the response property 'name' became optional","level":2,"operation":"GET","path":"/pets/{id}","section":"paths","fingerprint":"f8cd9af6b117"},
 {"id":"endpoint-added","text":"endpoint added","level":1,"operation":"POST","path":"/pets","section":"paths"}]
```

1. `find`: `oasdiff: false` → None, no note; a configured missing path → None, reason names it; `auto` with the fake
   on PATH → found, `source: "path"`; tools.json written; a second `find` with `shutil.which` monkeypatched to raise
   still returns it (cache); touching the fake (mtime/size change) re-resolves.
2. `run`: timeout → None and `last_error`; never raises on a missing file.
3. ERR → one Break (kind http, source oasdiff, item starts `oasdiff:api-path-removed-without-deprecation:GET /pets:`);
   WARN → note "1 possible breaking change"; INFO ignored; `string-levels` gives the same result; `no-id` gives a
   Break whose item starts `oasdiff:change:GET /pets:80a8c624dc40`.
4. argv: `breaking`, base and revision file names end with the contract's relative path, `-f json`,
   `--allow-external-refs=false` when the spec has only `#/` refs; cwd is not the repo; `OASDIFF_CONFIG` not passed.
5. Sibling ref (`$ref: './schemas.yaml#/Pet'`) with an OS sandbox available: both temp trees contain `schemas.yaml`
   from the right side's root (the baseline's copy for base at Stop); `--allow-external-refs=false` is absent; the
   command starts with the sandbox wrapper (`sandbox-exec` or `bwrap`). With `os_sandbox.active` monkeypatched False:
   the fake is not called, parser result, the D8 note.
6. Refused forms, each → the fake is **not** called (no line in calls.jsonl), parser result, a note naming the ref:
   `https://example.com/x.yaml`; `HTTP://EXAMPLE.COM/x.yaml` (upper case); `file:///etc/x.yaml`; `/etc/x.yaml`
   (absolute); `../../outside.yaml` (leaves the tree); a symlink in the tree pointing outside; a sibling file that
   itself contains `$ref: 'https://...'` (ref inside a referenced file); more than `MAX_REF_FILES` files; a ref file
   that cannot be read (mode 000 or invalid UTF-8).
7. `fail`, `garbage` and `object` (exit 0 with a JSON object, not a list) → parser result, note with the reason.
8. Missing tool note appears once per session (two edits of the contract → one note).
9. A Python edit that is not the contract never calls the fake. A new contract file (`before` None or empty) never
   calls it and gives no note.
10. **Fail-open:** with the fake on PATH, an "after" text that is broken YAML → `[]` as today, the fake not called, no
    exception; `contract_tools.oasdiff_breaking` monkeypatched to raise `RuntimeError` → `check_http_file` returns the
    parser's breaks plus a note, no exception.

### 7.2 `tests/test_pre_edit_oasdiff.py` (timing)

11. `pre_edit.handle` on a fixture repo (config, ledger, contract file with internal refs only, fast fake): a removed
    path asks (guarded mode) with the oasdiff subject; wall time printed and asserted `< 1.0 s` (record the number).
12. `slow` fake: `handle` returns within `pre_edit_timeout_s + 0.5 s`, with the parser's result and the timeout note;
    `pre_edit_timeouts == 1` in tools.json.
13. Skip rule: after two consecutive `slow` runs (`pre_edit_timeouts == 2`), a third `handle` does **not** call the
    fake (calls.jsonl unchanged) and gives the "twice in a row" note once. **Stop path:** after the two timeouts, run
    `audit.run` on the fixture (and, separately, `contract_tools.refresh(cfg, reset_counters=False)` as `ci.run` calls
    it): the counter is still 2 and the next `handle` still skips the fake. **SessionStart path:**
    `refresh(cfg, reset_counters=True)` resets the counter to 0 and the next `handle` calls the fake again. Separately,
    touching the fake (stat change) also resets it; a fast run after one timeout sets the counter back to 0.

### 7.3 Optional real binary (skipped when `shutil.which("oasdiff")` is None)

14. Two small specs: removing `GET /pets` gives a level-3 item with `path == "/pets"`; adding an optional query
    parameter gives no level-3 item. Print `oasdiff --version` and the run time.
15. `oasdiff schema` parses as JSON and the change object's properties include `id`, `text`, `level`, `operation`,
    `path`, with `level` typed integer (if this fails, the pinned sample is wrong: stop and report).

### 7.4 `tests/test_migration_drift.py` (skipped when `TARGET_PY -c "import django"` fails; see D3)

Fixture project built in `tmp_path` (like section 3): `settings.py` with sqlite `NAME` inside the project,
`shop/models.py`, `shop/migrations/0001_initial.py`; baseline tree = a copy; config `django_settings: settings`.

16. Field added without a migration → one new item `drift:shop:Add field price to item`; **no `db.sqlite3` in either
    tree afterwards**; `blocks == []`.
17. Same field plus a matching `0002` migration → no drift.
18. Drift already in the baseline (a field missing a migration in both trees) → not reported; a second new field → only
    the new one.
19. Non-null field without default → items `drift:shop:needs-default:item.price` and the operation; `exit == 1`.
20. **Baseline exit-3 field plus a new ordinary field in the working tree** → the new field's item is reported (never
    silence); the needs-default item is not new.
21. **Baseline conflict plus a new ordinary field** → the new field is reported; the conflict is not new. A new leaf in
    the same conflicted app → the conflict item is new; a new conflict in another app → only that app's item.
22. **Formatter patch, without naming any formatter:** a driver run with `TARGET_PY` that (a) monkeypatches
    `shutil.which` to return the path of a fake executable in `tmp_path` for every name, (b) calls Django's original
    `django.core.management.utils.run_formatters([])` outside the worker and asserts the fake wrote its marker file
    (proves the call happens), then (c) runs `lg_migrations.main` on the drift fixture with the same `shutil.which`
    patch and asserts the result is complete, `blocks == []`, and the marker was not written again.
23. Settings that raise on import → `not_checked`, the set-up note, no finding.
24. `post_edit` on a models-only edit with no function targets → the drift context note appears and no lock is held
    while `run_tree` runs (monkeypatch `run_tree` to assert `ledger` lock is free, e.g. by taking it with
    `LOCK_NB`); on a non-models edit `run_tree` is not called (count calls). With targets and no wake: the note
    contains both the diff note and the drift text. With a wake: the wake text ends with the drift text before the
    skipped note.
25. Stop audit: drift present → a `contract_break` finding with source makemigrations, `file == "shop/models.py"`,
    `code_hash == sha(item)`; a second audit → still open (not flipped to `not_applied` and back); after adding the
    migration → `not_applied`; **a failed run** (monkeypatch `run_tree` to return None) leaves the open drift finding
    open; findings from `run_checks` are not disturbed. A waiver on the drift finding survives an unrelated edit to
    another models file.
26. CI: `ci.run` on a base/head pair with a model field added → `out.breaks` has `("shop/models.py", Break(...))`;
    a waiver with the same `code_hash` hides it; a failed run puts a note in `out.notes` and no break.
27. Cache key: changing `django_settings` or `behavioral_diff.io` in the config runs the baseline again; a reported
    Django version different from the cached one runs it again; a cached incomplete result is not reused.
28. Timing: print seconds per tree; assert `< 10 s` only.

## 8. Acceptance criteria

1. **PreToolUse: under 1 s with a fast tool; at most `pre_edit_timeout_s + 0.5 s` (1.3 s by default) with a hanging
   one.** Test 11 asserts `< 1.0 s` for the whole `pre_edit.handle` with the fast fake; test 12 proves the bound with
   a hanging one; test 13 proves the skip rule removes even that after two timeouts; a non-contract edit never spawns
   a tool (test 9). With a real binary, the implementer records the run time on the test spec; above 0.5 s median →
   ask the user (D1).
2. Tool absent → falls back with a note: tests 1 and 8 (once per session); refused, failed and raising runs: tests 5,
   6, 7, 10.
3. Model drift without a migration is flagged **with no database**: test 16 (no database file created, no blocked
   operation, item reported); the patch is needed: without it the run fails under the worker (section 3).
4. Only new drift is reported, and earlier drift never hides new drift: tests 18, 20, 21.
5. Decisions come from parsed JSON, never exit codes: test 7 (non-JSON and wrong shape fall back even with exit 0),
   test 3 (string levels, missing `id`), test 19 (Django's exit 3 path is reported from the recorded fields).
6. Fail-open: tests 10, 23, 25 (failed run leaves findings unchanged), 26.
7. Existing behaviour unchanged without tools: the current `tests/test_checks.py` passes untouched; `check_http_file`
   with `tools=None` returns exactly today's breaks.
8. Docs: CLAUDE.md gets a "Sprint 7.6 Part A" section (limits from section 9); `docs/PROGRESS.md` gets the timings.

## 9. Known limits to document

- oasdiff runs only when the configured contract file itself is edited (`rel == contracts.http`); an edit to a sibling
  file it `$ref`s is not diffed at PreToolUse (the Stop audit sees it only if that file is the contract). Follow-up.
- Waiver items differ between the parser and oasdiff (a waiver made under one does not carry to the other); an
  oasdiff fingerprint can change when the base changes (docs/FINGERPRINT.md).
- Specs with remote, absolute or out-of-tree `$ref`s are never given to oasdiff; specs with sibling refs only under an
  OS sandbox (D8). Inside the sandbox oasdiff can still *read* files outside the temp folder if our scan missed a ref
  (the sandbox blocks the network and writes, not reads); the scan over-approximates to make that unlikely.
- `--open` (upload to oasdiff.com) is never passed, and project config that could set it is ignored (D5).
- A project's `.oasdiff.*` config is ignored (D5).
- Drift detection needs the project's settings to import inside the worker (stubs on, network and DB blocked) and its
  database driver to be importable; otherwise "not checked".
- The questioner patch answers "add with a placeholder default" for non-null and `auto_now_add` fields, so detection
  goes on; the reported item says the field needs a default. Rename questions answer "no" in non-interactive mode, so
  a renamed field shows as remove plus add.
- Third-party apps' drift appears in both trees and is filtered by "new only"; drift caused by a dependency upgrade
  inside the session is reported as new (inference).
- A drift waiver is keyed on Django's one-line description of the operation (`sha(item)`, e.g. "Add field price to
  item"), not on the field definition: it survives unrelated edits, but also a later change to that field's type or
  options while the description stays the same. A different Django version describing the same operation differently
  would ask again.
- Switching the check off during a session (`contract_tools.makemigrations: false`, or `django_settings` removed)
  makes `applies` false, so the next Stop sweeps existing drift findings to `not_applied`, which reads as "fixed" in
  the report although nothing was checked.
- In CI, drift in a third-party app (`file = "django-app:<app>"`) is not a real path: its annotation is written
  without `file=` (a plain `::error title=...::` line), and the comment lists it under the app name.

## 10. Later parts (sketched, not in this build)

None of these is nearly free; each needs its own sources checked first.

- **Part B: code-first OpenAPI.** A worker spec `kind: "openapi"` that loads the app with `lg_worker.load_app` in both
  trees (already used for routes, with `setup_phase()` and the sandbox) and returns `json.dumps(app.openapi())`
  (FastAPI; Flask has no built-in spec). Run at PostToolUse/Stop when a guarded file under `app:`'s package changed,
  then `oasdiff_breaking` on the two JSON texts (refs are internal `#/components/...`, so
  `--allow-external-refs=false`). Can wake, cannot block. Cost: one more worker round per check (two app imports).
  This would catch the schema-only change noted in sprint7.md (`body` → `text` in a model).
- **Part C: squawk** (latest release v2.74.0, 2026-11-23, per the GitHub releases API) for `.sql` files under
  `contracts.db`: earlier research says `squawk --reporter json --stdin-filepath`; flags and JSON fields **unverified**;
  would replace the regexes in `contracts_parse.dangerous_statements` for SQL when present. PreToolUse-capable if fast
  (single file, no project code).
- **Part D: buf** (latest v1.75.0, 2026-10-22, GitHub releases API) at Stop only: `buf breaking <tree> --against
  <baseline dir> --error-format json` per earlier research; flags and JSON **unverified**; needs a `buf.yaml` in the
  project.
- **Pydantic/SQLAlchemy model diffing** (ROADMAP C5): not started.
