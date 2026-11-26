# Sprint 7.6 build spec, Part C: squawk for `.sql` migrations under `contracts.db`

Researcher agent, 2026-11-26, main at ab1edb3 (Part B at fe0d0c5, the process-group fix on top). **Reviewer PASS in round 3
(2026-11-26).**

**User decisions (2026-11-26, final):** D8 yes: the implementer may `pip install squawk-cli==2.74.0` once into a
throwaway venv in the session scratchpad to check flags, JSON, exit codes and timing. D7 (b): add `squawk-cli==2.74.0`
to `requirements-dev.txt` (dev venv and CI). D3 squawk's own break set (the 21 default-on "backwards compatibility"
rules plus `ban-drop-column`). D1, D2, D4, D5, D6, D9, D10 and D11 as recommended. Coordinator note on the round-3
optional items: say "every call except `--version`" in 6.1's first sentence, and have the fake's `alter table <t> drop
<x>` pattern exclude `constraint`, `default`, `not`, `identity`.

**Build note (2026-11-26, real squawk 2.74.0; build reviewer PASS round 2):** D6's trade-off text is wrong on one
point: with `assume_in_transaction` true, a bare `CREATE INDEX CONCURRENTLY` is **not** reported as
`ban-concurrent-index-creation-in-transaction`; only one after an explicit `BEGIN` is. Its effect on
`adding-required-field` holds.

**Round-2 corrections (reviewer, 2026-11-26) are folded in and override earlier text:** (1) the fake handles
`--version` first and logs it only to `versions.log`, so `refresh()` at Stop and in `ci.run` adds no `calls.jsonl`
entry; every mode and token the tests use is listed (6.1). (2) The unchecked kinds include `unstartable`, and the
`check_migration` exception branch records kind `failed`; every kind except `off` keeps findings open (5.4, D11).
(3) Section 0 matches 5.3, 5.4 and AC8 (key form, SQLite dedupe, time rule). (4) `_run`'s new signature is given;
oasdiff's call is unchanged and the UTF-8 decode is inside `_run` (5.2 step 4).

**Round-1 corrections (reviewer, 2026-11-26) are folded in and override earlier text:** (1) squawk's line index starts
a line at `\n`, `\r\n` and a lone `\r`; the prepared text normalises both to `\n` before writing and before any
offset is computed (2.4, 5.2, 5.3; tests 31, 31b). (2) Regex spans are computed on the same prepared text as the squawk
windows (5.3). (3) Item key = first 80 characters of the normalised statement plus a sha12 of the whole normalised
statement; `;` boundaries are found on comment-blanked text (5.3; test 18b). (4) One time rule per Stop or `ci.run`:
after the first squawk timeout or start failure the remaining `.sql` files of that run are not given to squawk (one
note), so a run costs at most one timeout (5.4, 5.6; test 24b). (5) D9 re-argued: 25 of the 27 opt-in rules are tagged
"backwards compatibility"; the real reasons are version coupling, noise and policy scope; the compatibility-relevant
opt-in rules are a listed gap (section 8). (6) D2 no longer overstates: squawk-only items exist only where squawk ran,
a PreToolUse timeout followed by a good Stop gives a new open finding that blocks the Stop once, and waivers hold only
for the same file hash (D2, section 8). (7) squawk runs with cwd = the temp folder and relative `before.sql`/`after.sql`
(they go through `glob::glob`); violations are split on exact `file` equality (5.2; test 7). (8) Tests that plant a
decoy `.squawk.toml` monkeypatch `tempfile.tempdir` to a folder under `tmp_path` (tests 7, 29). (9) Lifecycle: new
decision D11 (switched off sweeps squawk findings like drift; missing, failing or unparsable keeps them); test 24 covers
both (5.5). (10) Spec gaps: the configured-path refusal text (5.2 step 1), the singular/plural report text (5.5),
`squawk_pg_version` types, digits and caps (5.1), audit.py line numbers (2.9). (11) `ALTER TABLE t DROP c` (no `COLUMN`
keyword) gave both a squawk `ban-drop-column` item and the SQLite `column:t.c` item; a squawk `ban-drop-column` or
`renaming-column` break whose statement names a table and column of a new SQLite `column:` item is now dropped (5.3;
test 11b). Optional items adopted: (i) a PATH-lookup note in section 3; (ii) stdout decoded as UTF-8 with
`errors="replace"` (5.2); (iii) `assume_in_transaction: true` offered as the D6 alternative with its trade-off.
Self-contained: the implementer should not need the Part A or Part B plans, but this part reuses Part A's code
(`guardlib/contract_tools.py`, the `ToolContext` path through `checks.run_checks`, the pre-edit skip counter,
`take_notes`, the "Contract tools" report line) and the process-group runner from the Part B fix (`os_sandbox.run`).
Spec origin: Part A plan section 10 (unreviewed sketch), `docs/HANDOFF.md` "Next (start here)", `docs/ROADMAP.md` C5.

Fixtures in this plan are generic shapes (a `db/` folder with `0001_init.sql`, tables `t`, `accounts`, a view `v`).
None of the held-out cases in `docs/validation/holdout-set.md` or `holdout-set-2.md` is used or named.

Nothing was downloaded, installed or run. Every squawk claim below was read in squawk's source at the tag `v2.74.0`
(raw files at `https://raw.githubusercontent.com/sbdchd/squawk/v2.74.0/<path>`), its docs folder at that tag, the
GitHub releases API, PyPI's JSON API and the npm registry.

What the sketch got right, and what it missed (each checked below):

| Sketch (Part A section 10, HANDOFF) | What the sources say |
|---|---|
| latest release v2.74.0 | **Confirmed**: v2.74.0, published 2026-11-23T00:04:16Z (CHANGELOG says 2026-11-22); v2.73.0 2026-11-15, v2.72.0 2026-11-08. Releases come about weekly (2.1). |
| `squawk --reporter json --stdin-filepath` | **Both flags exist**, but `--stdin-filepath` only sets the name printed for stdin input; it does not make squawk read stdin. Squawk reads stdin only when **no path is given and stdin is not a terminal** (2.2). Recommended form here: two temp files as paths, no stdin, one process (5.2). |
| (implied) flags `--exclude`, `--pg-version`, `--assume-in-transaction` | All exist (top-level, `global = true`). `--exclude` takes rule names and **fails the whole run on an unknown name** (clap parses them into the `Rule` enum), so passing rule names couples us to the squawk version; we filter on our side instead (2.2, 5.2). |
| (not mentioned) config file | squawk reads **`.squawk.toml` found by walking up from the cwd to `/`**; it cannot be switched off, only replaced with `-c/--config <path>`. A temp cwd alone is **not** enough (a `.squawk.toml` in `/tmp` or any parent would be read). Pass `--config` pointing at our own file (2.3, 3). |
| (not mentioned) in-file ignores | `-- squawk-ignore <rules>` and `-- squawk-ignore-file` comments in the SQL suppress findings. Claude writes the SQL, so it could silence the check; neutralise them before linting (2.8, 3). |
| "replace the regexes" | squawk messages carry **no object names** ("dropping a column may break existing clients"), and a file with any syntax error gets **only** `syntax-error` entries and no lint at all. Keeping the regex items (stable waiver keys, names in the subject) and adding squawk's other breaking rules is the safer default (D2). |
| "PreToolUse-capable if fast" | Plausible (single static Rust binary, two small files), but **timing is UNVERIFIED**: no binary was run. Same 0.8 s cap and skip rule as oasdiff (5.4). |
| (implied) Python install route | Confirmed: `pip install squawk-cli` (PyPI `squawk-cli` 2.74.0, maturin `bindings = "bin"`, wheels for macOS x86_64/arm64, manylinux_2_28 x86_64/aarch64, Windows) and `npm install -g squawk-cli` (a JS shim that spawns a per-platform optional package), plus raw binaries on the releases page and a Docker image (2.1). |

## 0. Summary

When squawk is installed (config `contract_tools.squawk: auto|false|<path>`, else `PATH`), the static check of a
`.sql` file under `contracts.db` runs `squawk --reporter json --config squawk.toml before.sql after.sql` once
(only `after.sql` for a new file; all three are relative names in a fresh temp folder that is the cwd), an environment without `GITHUB_ACTIONS` or `SQUAWK_*` (plus
`SQUAWK_DISABLE_GITHUB_ANNOTATIONS=1`), stdin empty, through `os_sandbox.run` (process-group kill on timeout).
`squawk-ignore` comments are neutralised (same-length rewrite) before the text is written.

From the parsed JSON (decided from JSON, never from the exit code alone):

1. Any `syntax-error` entry for either side, a timeout, a missing binary, unreadable output: **the regex path alone,
   exactly as today**, with a once-per-session note. At Stop and in `ci.run`, after the first squawk timeout or failed
   start of that run, the remaining `.sql` files get the regex check only, with one note (time rule, 5.4).
2. Otherwise each violation is classified by rule name (D3): **break** (squawk's "backwards compatibility" rules that
   are on by default, plus `ban-drop-column`), **risk** (locking and transaction rules), **ignored** (style, timeouts,
   unknown names).
3. Only violations new in the edit count: an item key `squawk:<rule>:<window[:80]>:<sha12(window)>` (window = the
   normalised enclosing statement, 5.3) present after and not before.
4. **Supplement, not replace (D2):** the regex items (`stmt:<label>:<name>`) stay as they are. A new squawk break whose
   rule is one of the five the regexes already cover and whose statement contains a new regex match is dropped as a
   duplicate; so is a new `ban-drop-column` or `renaming-column` break whose statement names the table and column of a
   new SQLite `column:<t>.<c>` item from the same check (`ALTER TABLE t DROP c`, 5.3); every other new squawk break
   becomes a `Break("db", item, subject, "squawk", rel, line=...)`, i.e. a
   `contract_break` finding exactly like the regex ones (PreToolUse ask/deny per mode, Stop finding, CI `::warning`).
   New risk violations are a context note at PreToolUse and a report/CI note (D4), never a prompt.

Hooks (D1): PreToolUse (0.8 s cap, skip rule, regex fallback), Stop (`_contract_findings`, 20 s) and `ci.run` (20 s),
all through `checks.run_checks`. Not PostToolUse (nothing to add: the static check already ran before the write, and
the Stop audit catches shell edits). It catches what the regexes miss today: dropped views, functions, types, schemas,
sequences, domains, constraints, defaults, `not null`; `set schema`; renamed constraints and other objects; identity
changes; a new `not null` column without a default; `truncate ... cascade`; and statements the regexes misparse.

## 1. Open decisions for the user (each with a recommendation)

| # | Decision | Recommendation |
|---|---|---|
| D1 | Which hooks run squawk. (a) PreToolUse with the 0.8 s cap and regex fallback, plus Stop and CI; (b) Stop and CI only; (c) also PostToolUse. | **(a)**. PreToolUse is the only hook that can ask before a destructive migration lands, squawk reads only the two texts we give it, and the cap plus the existing skip rule (two pre-edit timeouts in a row skip it for the session) bound the cost. Only edits to `.sql` files under `contracts.db` pay anything. PostToolUse adds nothing the PreToolUse check and the Stop audit do not already see. If the implementer measures a median above 0.3 s on the fixture with the real binary (D8), report it before shipping. |
| D2 | Replace or add to the regex findings for SQL. (a) Supplement: regex items unchanged, squawk adds breaks for statements the regexes do not cover, duplicates dropped; (b) replace: when squawk ran cleanly, only squawk items; (c) (a) plus a veto: drop a regex item when squawk parsed the file and reports nothing on that statement (regex hit in a `/* */` comment or a string). | **(a)**. Regex items carry the object name (`stmt:column dropped:email`) in the subject and the waiver key; squawk messages carry none. With (a) the regex items, and a waiver on one, are the same whether squawk is installed, missing or timed out, so for the five statements the regexes cover the "parser and tool waiver items differ" limit Part A has for oasdiff does not arise. It does not remove it for squawk-only items: those exist only where squawk ran, so a PreToolUse timeout or skip followed by a successful Stop gives a new `open` finding at Stop that blocks the Stop once, and a CI run without squawk has none of them. A waiver holds only while the file's hash is unchanged (`waivers.is_waived` compares `code_hash`, waivers.py 21-23; `code_hash = sha(after)` of the whole file), for regex and squawk items alike. (b) loses names and changes keys per machine. (c) is a good follow-up once the real binary has been run on block comments and strings (UNVERIFIED today). |
| D3 | Which squawk rules are breaks, risks, ignored. | **Break** = the 21 rules that are on by default and that squawk's own site tags "backwards compatibility" (`docs/src/pages/index.js`), plus `ban-drop-column` (missing from that page; its message says "may break existing clients"). **Risk** = the default "locking" rules and the few that fail or misbehave at run time (`transaction-nesting`, `ban-concurrent-index-creation-in-transaction`, `ban-uncommitted-transaction`, `identifier-too-long`, `ban-duplicate-column-assignments`, domain-constraint rules). **Ignored** = `prefer-*`, `ban-char-field`, `require-lock-timeout`, `require-statement-timeout`, `require-enum-value-ordering`, `unused-ignore`, opt-in rules (never enabled) and any name not in our table. The full table is in 2.7. The classification comes from squawk's tags, not from tuning. Alternative: a narrower break set that moves `ban-drop-constraint`, `ban-drop-default`, `ban-drop-not-null`, `ban-alter-identity`, `ban-drop-generated-expression` and `ban-truncate-cascade` to risk, if the user expects those to be routine in their migrations (each break asks once in guarded mode; prompts are capped at 3 per session). |
| D4 | What a risk (locking) violation produces. (a) A context note at PreToolUse, a note in the report and in CI; (b) a `RISK` finding (state `recorded`, never blocks) at Stop. | **(a)**. Whether a lock matters depends on table size and the deploy process, which LegacyGuard cannot see; a finding lifecycle (like Part B's hedged one) is more code for little gain. The report shows at most 4 contract-tool notes today (`contract_tools.report_line`); risk notes are one line per file. |
| D5 | Honour the project's own squawk settings. (a) No: `--config` points at our own file, temp cwd, `squawk-ignore` comments neutralised; (b) read the project's `.squawk.toml`; (c) honour in-file `squawk-ignore` comments. | **(a)**, as Part A did for `.oasdiff.*` (D5 there). A `.squawk.toml` with `excluded_rules = ["ban-drop-column"]`, or `-- squawk-ignore-file` at the top of the migration, is one edit away for Claude and would silence the check. The project's intent is expressed through `/legacyguard:waive` instead. A later `contract_tools.squawk_config: <path>` opt-in can be added if users ask. |
| D6 | Postgres version and transaction setting. | **Pass nothing by default** (squawk assumes Postgres 15.0.0, `version.rs`; `assume_in_transaction` false). New config keys `contract_tools.squawk_pg_version` (a string like `"13"` or `"12.4"`, validated as in 5.1, else ignored with a note) and `contract_tools.squawk_assume_in_transaction` (bool, default false). Both only change risk rules, except that `adding-required-field` skips tables created earlier in the same transaction (2.7). Alternative: default `assume_in_transaction: true` (most migration runners wrap each file in a transaction). Trade-off: it changes `transaction-nesting` (an explicit `BEGIN` is then reported as nesting) and `ban-concurrent-index-creation-in-transaction` (every `CREATE INDEX CONCURRENTLY` is then reported), both risk notes only; in exchange `adding-required-field` no longer fires on a `NOT NULL` column added to a table created earlier in the same file. Recommended default stays false, because whether a runner wraps files is per project and the false default only costs that one break on a narrow pattern (section 8). |
| D7 | Install squawk in CI. (a) No (fake binary only, real-binary tests skip); (b) `squawk-cli==2.74.0` in `requirements-dev.txt`, so the real-binary tests run on every CI job and in the dev venv. | **(b) if D8 is yes, else (a).** Unlike oasdiff (a Go binary, Part A D4), squawk is a pinned pip wheel for both CI runner OSes, so installing it is one requirements line, and it would close the "real binary never run" gap Part A still has. It is a third-party binary in the dev venv and in CI, so it is the user's call. |
| D8 | May the implementer download the real squawk once (`pip install squawk-cli==2.74.0` into a throwaway venv in the session scratchpad, about a few MB, from PyPI) to check flags, the JSON shape, exit codes and timing against this plan? | **Yes, recommended.** Every claim in section 2 comes from reading source; none was run. One run settles the JSON bytes, `--config` with our minimal file, the stdin/paths behaviour, CRLF columns, timing, and whether a block comment or string fools the regexes (D2 c). The test list (6.6) says what to run. |
| D9 | Enable opt-in rules (`--include`, e.g. `ban-drop-index`, `ban-drop-trigger`, `ban-revoke`). | **No, for now.** Squawk's site tags 25 of the 27 opt-in rules "backwards compatibility" (`docs/src/pages/index.js` lines 281-449; the other two are `require-table-schema` and the `require-timeout-settings` alias), so scope is not the reason. The reasons: (1) `--include` parses into `Vec<Rule>` (main.rs lines 174-181), so a name an older installed squawk does not know fails the whole run with a usage error, and every rule added after some 2.x release would have to be gated on the probed version; (2) several are noisy in ordinary migrations (`ban-add-column` fires on every added column, `ban-drop-index` on routine index swaps, `ban-set-default`, `ban-add-enum-value`); (3) many are policy checks (row-level security, roles, policies, `ban-revoke`, system and database options) where "may break clients" depends on the deployment. The compatibility-relevant ones LegacyGuard therefore misses are listed in section 8. Revisit with D8 data, possibly with a version-gated `--include` of a short list. |
| D10 | Note when squawk is not installed. | **Once per session, only when a `.sql` under `contracts.db` is checked** (note kind `squawk`, like oasdiff's `MISSING_NOTE`), and a "squawk not found" part in the "Contract tools" report line only when `contracts.db` is set. Most projects have no SQL migrations and should see nothing. |
| D11 | Lifecycle of open squawk findings at Stop when squawk does not lint a file. (a) Switched off (`contract_tools.squawk: false`) sweeps them to `not_applied`, like Part A's drift; missing binary, timeout, failure, the time rule (5.4) or a file that stopped parsing keeps them (`seen_items`); (b) keep them in every case; (c) sweep in every case. | **(a)**. Turning the check off is the developer's choice and matches what drift does today (CLAUDE.md, Part A limits: "Turning the check off mid-session sweeps drift findings to `not_applied`"); the report then says "squawk switched off", so a swept finding is not read as fixed without context. A missing or failing tool says nothing about the SQL, so its findings stay open (sweeping would read as "fixed"). A file that stopped parsing is in the same position: squawk said nothing about its statements. (b) leaves findings open forever after the developer turned the check off; (c) turns any transient failure into "fixed". |

## 2. Sources and confirmed claims

"Inference" marks my own reasoning. Line numbers are at the tag `v2.74.0`.

### 2.1 Releases and distribution

- **Latest: v2.74.0**, `published_at` 2026-11-23T00:04:16Z, not prerelease; v2.73.0 2026-11-15T17:00:31Z, v2.72.0
  2026-11-08T05:05:03Z, v2.71.0 2026-10-29, v2.70.0 2026-10-15, v2.69.0 2026-10-14. Assets per release:
  `squawk-darwin-arm64`, `squawk-darwin-x64`, `squawk-linux-arm64`, `squawk-linux-x64`, `squawk-linux-musl-arm64`,
  `squawk-linux-musl-x64`, `squawk-windows-x64.exe`, plus VS Code `.vsix` files —
  [GitHub releases API](https://api.github.com/repos/sbdchd/squawk/releases?per_page=6). The CHANGELOG dates v2.74.0
  2026-11-22 — [CHANGELOG.md](https://github.com/sbdchd/squawk/blob/v2.74.0/CHANGELOG.md).
- **PyPI**: package `squawk-cli`, version 2.74.0, `requires_python >=3.8`; files
  `squawk_cli-2.74.0-py3-none-macosx_10_12_x86_64.whl`, `...-macosx_11_0_arm64.whl`, `...-manylinux_2_28_aarch64.whl`,
  `...-manylinux_2_28_x86_64.whl`, `...-win32.whl`, `...-win_amd64.whl`, sdist; uploaded 2026-11-23 —
  [PyPI JSON API](https://pypi.org/pypi/squawk-cli/json). Built with maturin, `[tool.maturin] bindings = "bin"` —
  [crates/squawk/pyproject.toml](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/pyproject.toml).
  Inference: a "bin" maturin wheel installs the `squawk` executable into the environment's scripts folder; UNVERIFIED
  (not installed).
- **npm**: `squawk-cli` 2.74.0 — [npm registry](https://registry.npmjs.org/squawk-cli/latest); `bin` is
  `js/bin/squawk`, a Node shim that resolves `@squawk-cli/<platform>` optional dependencies and spawns the binary —
  [package.json](https://github.com/sbdchd/squawk/blob/v2.74.0/package.json),
  [js/index.js](https://github.com/sbdchd/squawk/blob/v2.74.0/js/index.js).
- Install routes in the docs: `npm install -g squawk-cli`, `pip install squawk-cli`, release binaries, Docker image
  `ghcr.io/sbdchd/squawk` — [docs/docs/quick_start.md](https://github.com/sbdchd/squawk/blob/v2.74.0/docs/docs/quick_start.md).
- **Binary name** `squawk` (`[package] name = "squawk"`, `default-run = "squawk"`) —
  [crates/squawk/Cargo.toml](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/Cargo.toml).
  `#[command(version)]` on `Opts` — [crates/squawk/src/main.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/main.rs)
  line 143. Inference (clap's default): `squawk --version` prints `squawk 2.74.0`, so `contract_tools.refresh`'s
  "last word of the first line" rule reads `2.74.0`; UNVERIFIED (not run).

### 2.2 CLI flags (main.rs, cmd.rs, docs/docs/cli.md)

All from [main.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/main.rs),
[cmd.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/cmd.rs) and
[docs/docs/cli.md](https://github.com/sbdchd/squawk/blob/v2.74.0/docs/docs/cli.md) (its `squawk --help` block).

- Usage: `squawk [OPTIONS] [path]... [COMMAND]`; commands `check`, `format`, `server`, `upload-to-github`.
  `CheckArgs` (paths, `--debug`, `--reporter`, `--stdin-filepath`) is flattened into the top-level `Opts`, so
  `squawk --reporter json a.sql` works without a subcommand (main.rs lines 62-76, 144-146). Squawk's own test
  `top_level_paths` runs `squawk ../../example.sql` with no subcommand —
  [crates/squawk/tests/example_output.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/tests/example_output.rs).
- **The `check` subcommand is new in v2.73.0** ("linter: add check subcommand (#1476)") — CHANGELOG. Use the top-level
  form so older 2.x versions work too. With `check`, the check args must come after it, else a usage error (cmd.rs
  lines 128-149).
- `--reporter <tty|gcc|json|gitlab>` (`ignore_case = true`, default `tty`) — main.rs lines 70-72, 131-138.
- `--stdin-filepath <filepath>`: "Path to use in reporting for stdin" (main.rs lines 73-75). It only names stdin input
  in the output (reporter.rs line 191: `stdin.path.clone().unwrap_or_else(|| "stdin".into())`).
- **Input selection** (cmd.rs `read_input`, lines 36-61): paths matched by the patterns win; if patterns were given
  and none matched, squawk prints "Failed to find files for provided patterns" and exits 1 (unless
  `--no-error-on-unmatched-pattern`); stdin is read only when no file matched and `is_stdin` is true, where
  `is_stdin = !io::stdin().is_terminal()` ([config.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/config.rs)
  line 134). Empty or whitespace-only stdin gives no report (reporter.rs lines 187-190).
- Paths go through `glob::glob(pattern)` unless the path is a directory (walked for `*.sql`)
  ([file_finding.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/file_finding.rs) lines 25-62).
  Inference: a file name with `[`, `*` or `?` would be read as a pattern; we only pass our own temp names.
- `--exclude/-e <rule,...>` and `--include/-i <rule,...>` (`value_delimiter = ','`, parsed into `Vec<Rule>`), global;
  "`--exclude` takes precedence over `--include`"; each "completely overrides the configuration file" (main.rs lines
  156-181; config.rs lines 86-98). An unknown name is a parse error (`Rule::from_str` returns `UnknownRuleName`,
  [squawk_linter/src/lib.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/lib.rs) lines
  319-324); clap then exits with a usage error (inference from clap; squawk's own tests show usage errors exit 2).
- `--exclude-path <glob>` (repeatable) — main.rs lines 147-155.
- `--pg-version <VERSION>` (e.g. `13.0`), global; parsed as `major[.minor[.patch]]` integers — main.rs lines 182-187;
  [squawk_linter/src/version.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/version.rs).
  **Default Postgres 15.0.0** (`impl Default for Version`, same file).
- `--assume-in-transaction` (global) and a hidden `--no-assume-in-transaction` (conflicts with it) to override a config
  file — main.rs lines 196-207; config.rs lines 112-118.
- `-c/--config <path>`: "Path to the squawk config file (.squawk.toml)" — main.rs lines 193-195.
- `--verbose` enables info logging to **stderr** only (main.rs lines 252-266).
- `--no-error-on-unmatched-pattern` — main.rs lines 208-210.

### 2.3 Config file and environment

- `FILE_NAME = ".squawk.toml"`; `ConfigFile::parse(custom_path)`: with `--config` it reads exactly that path; without,
  `find_by_traversing_back()` walks from `env::current_dir()` up through **every parent to the root**, taking the first
  `.squawk.toml` — config.rs lines 14, 38-56, 177-194. There is no flag or variable that turns the search off; only
  `--config` replaces it. Docs: "By default, Squawk will traverse up from the current directory to find a
  `.squawk.toml` ... You may specify a custom path with the `-c` or `--config` flag" — cli.md.
- A config file that does not parse ends the run: `eprintln!("Configuration error: {e}")`, `process::exit(1)` —
  config.rs lines 79-84.
- Keys: `excluded_paths`, `excluded_rules`, `included_rules`, `pg_version`, `assume_in_transaction`,
  `[upload_to_github] fail_on_violations`, all `#[serde(default)]` — config.rs lines 16-36. Inference: a TOML file whose
  only line is `assume_in_transaction = false` parses to the defaults; an empty file most likely does too
  (UNVERIFIED, not run; the plan uses the one-line file).
- Environment read by the lint path: `GITHUB_ACTIONS` and `SQUAWK_DISABLE_GITHUB_ANNOTATIONS`, which only add GitHub
  annotation lines **for the tty reporter** in v2.74.0 (config.rs lines 151-154; reporter.rs `print_violations`
  lines 509-520). Before v2.57.0 annotations could clobber JSON output ("linter: fix github actions output clobbering
  json/gcc (#1192)", CHANGELOG v2.57.0). The `SQUAWK_GITHUB_*` variables belong to `upload-to-github` only (main.rs
  lines 26-60). Inference: the lint path makes no network calls (the linter crate depends only on `squawk-syntax`,
  `squawk-diagnostics`, `squawk-rowan`, `squawk-line-index`, serde and small utilities —
  [squawk_linter/Cargo.toml](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/Cargo.toml)).
  The `builtins.sql` cache write (`strategy.cache_dir().join("squawk/stubs")`) is in `squawk_ide_db`, used by the
  language server — [crates/squawk_ide_db/src/builtins.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_ide_db/src/builtins.rs)
  line 29; inference: not reached by a lint run.

### 2.4 JSON output (reporter.rs)

From [crates/squawk/src/reporter.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/reporter.rs):

- `fmt_json` (lines 379-385) serialises **one flat JSON array of violations across all files** with
  `serde_json::Serializer` + `collect_seq`, then a newline. No files and no violations print `[]` (inference from
  `collect_seq` on an empty iterator).
- Each element is `ReportViolation` (lines 320-337), fields in this order: `file` (string: the path as given, or the
  `--stdin-filepath` name, or `"stdin"`), `line` (integer), `column` (integer), `level` (string `"Warning"` or
  `"Error"`: a unit-variant enum with derived `Serialize`, lines 304-308), `message` (string), `note` (string or
  `null`), `help` (string or `null`), `rule_name` (string), `column_end` (integer), `line_end` (integer). `range` and
  `fix` are `skip_serializing`. All fields are always present (no `skip_serializing_if`).
- **Positions are zero-based**: `line` is the zero-based line, `column` the zero-based **UTF-8 byte** offset in that
  line — [crates/squawk_line_index/src/lib.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_line_index/src/lib.rs)
  lines 26-33 ("Zero-based." / "Zero-based UTF-8 offset."). The GitHub-annotation formatter adds 1 to lines
  (reporter.rs lines 398-408). A leading BOM is stripped before positions are computed (reporter.rs line 40; CHANGELOG
  v2.73.0 "fix BOM handling for offsets").
- **Line breaks:** squawk's line index is forked from rust-analyzer's and breaks lines at `\n`, at `\r\n` (one break)
  **and at a lone `\r`** ("`\r` and `\r\n` start a new line, not just `\n`", squawk_line_index/src/lib.rs lines 6-7;
  the scan at lines 537-541). Python's `str.splitlines` would also split on other characters (`\x0b`, `\x0c`,
  `\x1c`-`\x1e`, `\x85`, `\u2028`, `\u2029`), so the plan never uses it; it normalises `\r\n` and lone `\r` to `\n`
  and counts `\n` only (5.2, 5.3). Only PreToolUse `Write` content (and `Edit` strings) can carry `\r`: files the
  hooks read with `read_text()` already come through universal newlines.
- **Pinned sample** (squawk's own inline snapshot, test `display_violations_json`, reporter.rs lines 656-676), for
  the SQL `" \n   ALTER TABLE \"core_recipe\" ADD COLUMN \"foo\" integer NOT NULL;\nALTER TABLE \"core_foo\" ADD COLUMN \"bar\" integer NOT NULL;\nSELECT 1;\n"`:
  `[{"file":"main.sql","line":1,"column":3,"level":"Warning","message":"missing `set lock_timeout` before potentially slow `access exclusive` lock operations","note":"statement requires: `access exclusive` lock; blocking: reads, writes, schema changes","help":"configure a `lock_timeout` before this statement","rule_name":"require-lock-timeout","column_end":63,"line_end":1}, ...]`
  with eight entries (`require-lock-timeout`, `require-statement-timeout`, `adding-required-field`,
  `prefer-robust-stmts`, `prefer-bigint-over-int` on line 1; three on line 2). The fake (6.1) copies the field set,
  types and order of this snapshot. It also shows that a plain `ADD COLUMN ... NOT NULL` gives five violations, four
  of which LegacyGuard ignores or treats as risk.
- Violations are sorted by start offset within a file (`errors.sort_by_key`, lib.rs lines 733-743); files are sorted by
  path (reporter.rs line 218); files are linted in parallel (`par_iter`).
- The violation range starts at the first non-trivia token of the reported node (`Violation::for_node`, lib.rs lines
  436-455). For `ban-drop-column` the node is the `DROP COLUMN` action, not the whole statement; for
  `changing-column-type` it is the `SET TYPE`/`TYPE` part, which does **not** contain the column name —
  [rules/ban_drop_column.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/rules/ban_drop_column.rs),
  [rules/changing_column_type.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/rules/changing_column_type.rs).
  Hence the item key uses the enclosing statement, not the range (5.3).

### 2.5 Exit codes

- `lint_and_report` returns `ExitCode::SUCCESS` (0) when the total violation count is 0, else `ExitCode::FAILURE` (1)
  (reporter.rs lines 224-236). Squawk's test `example_sql_svg` asserts `.code(1)` with the comment "squawk returns 1
  when it finds violations" (example_output.rs).
- Exit 1 also for: a config file error (config.rs lines 79-84), no file matching a given pattern (cmd.rs lines 46-53),
  "Failed to find files" (cmd.rs lines 38-42). Errors returned from `main` through `anyhow` (e.g. a file that is not
  valid UTF-8: `read_to_string` in [file.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk/src/file.rs))
  end the process with Rust's standard failure code 1 (inference from Rust's `Termination` for `Result`; not run).
- Usage errors exit 2 (squawk's test `top_level_check_args_conflict_with_other_commands` asserts `.code(2)`).
- A panic prints "An internal error has occurred with Squawk v..." to stderr (main.rs lines 236-248); exit code 101
  is Rust's default for a panic (inference, UNVERIFIED).
- **Consequence:** exit 1 means either "violations found" (with a JSON array on stdout) or "failed" (nothing or text
  on stdout). Decide from the parsed stdout: a well-formed array is a result; anything else is a failure. Exit codes
  other than 0 and 1 are failures even if stdout parses. Exit 0 with a non-empty array, or 1 with `[]`, cannot happen
  per the source; treat both as failures (defensive).

### 2.6 Parse errors

- `check_sql` parses first; **if there is any parse error it returns only the parse errors** as violations with
  `rule_name: "syntax-error"`, `level: "Error"`, `message` from the parser, `help` from the parser's hint, and **no lint
  rules run for that file** (reporter.rs lines 41-75). Squawk's own snapshot:
  `[{"file":"test.sql","line":0,"column":36,"level":"Error","message":"missing comma","note":null,"help":null,"rule_name":"syntax-error","column_end":36,"line_end":0}]`
  for `ALTER TABLE foo ALTER CONSTRAINT bar RENAME TO quux;` (test `skip_lint_on_syntax_error`, reporter.rs lines
  544-551).
- All lint violations have `level: "Warning"` (reporter.rs line 94); `Error` is used only for syntax errors.
- `semantic-error` has a docs page ([docs/docs/semantic-error.md](https://github.com/sbdchd/squawk/blob/v2.74.0/docs/docs/semantic-error.md))
  but the CLI lint path in reporter.rs never produces it (inference: only `syntax-error` and `Rule` codes appear in
  `check_sql`). Treat any `level: "Error"` entry as a parse failure, whatever its rule name.
- squawk parses **PostgreSQL** only. Inference: SQL written for SQLite or MySQL (backticks, `AUTOINCREMENT`,
  `ENGINE=`) may fail to parse; the regex path then stands. The existing `contracts_parse.sql_schema` runs the scripts
  in SQLite, so some LegacyGuard users have SQLite-flavoured SQL; how often it fails squawk's parser is UNVERIFIED.

### 2.7 Rules (lib.rs) and LegacyGuard classes (D3)

The `Rule` enum has 78 variants; display names in `impl fmt::Display for Rule`, opt-in set in `Rule::is_opt_in`, the
default set is "every rule not opt-in" (`Linter::default_rules`), `--include`/`--exclude` adjust it (`with_rules`)
— [lib.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/lib.rs) lines 83-212, 326-413,
745-776. `require-timeout-settings` is an opt-in alias for `require-lock-timeout` + `require-statement-timeout`.
`prefer-timestamptz` is displayed as **`prefer-timestamp-tz`** (both spellings parse). Tags from the docs site's rule
list — [docs/src/pages/index.js](https://github.com/sbdchd/squawk/blob/v2.74.0/docs/src/pages/index.js) ("BC" =
"backwards compatibility", "L" = "locking", "S" = "schema", "Q" = "queries"); messages from each
`rules/<name>.rs`.

**Default-on rules (51: 78 variants minus 27 opt-in) and the class this plan gives them (22 break, 17 risk, 12 ignored):**

| Rule name (as printed in `rule_name`) | Squawk tag | Class | LegacyGuard regex label it overlaps |
|---|---|---|---|
| `ban-drop-column` | (not on the page; message "dropping a column may break existing clients") | break | column dropped |
| `renaming-column` | BC | break | column renamed |
| `renaming-table` | BC | break | table renamed |
| `ban-drop-table` | BC | break | table dropped |
| `changing-column-type` | BC, L | break | column retyped |
| `adding-required-field` | BC | break | |
| `ban-drop-database` | BC | break | |
| `ban-drop-not-null` | BC | break | |
| `ban-truncate-cascade` | BC | break | |
| `ban-drop-view` | BC | break | |
| `ban-drop-function` | BC | break | |
| `ban-drop-type` | BC | break | |
| `ban-drop-default` | BC | break | |
| `ban-drop-schema` | BC | break | |
| `ban-drop-sequence` | BC | break | |
| `ban-drop-domain` | BC | break | |
| `ban-drop-constraint` | BC | break | |
| `ban-drop-generated-expression` | BC | break | |
| `renaming-object` | BC | break | |
| `ban-set-schema` | BC | break | |
| `ban-alter-identity` | BC | break | |
| `ban-drop-extension` | BC | break | |
| `adding-field-with-default` | L | risk | |
| `adding-foreign-key-constraint` | L | risk | |
| `adding-not-nullable-field` | L | risk | |
| `adding-serial-primary-key-field` | L | risk | |
| `constraint-missing-not-valid` | L | risk | |
| `disallowed-unique-constraint` | L | risk | |
| `require-concurrent-index-creation` | L | risk | |
| `require-concurrent-index-deletion` | L | risk | |
| `require-concurrent-partition-detach` | S, L | risk | |
| `require-concurrent-reindex` | L | risk | |
| `transaction-nesting` | L | risk | |
| `ban-concurrent-index-creation-in-transaction` | S | risk (fails at run time) | |
| `ban-uncommitted-transaction` | S | risk | |
| `ban-create-domain-with-constraint` | S, L | risk | |
| `ban-alter-domain-with-add-constraint` | S, L | risk | |
| `identifier-too-long` | S | risk (Postgres truncates the name) | |
| `ban-duplicate-column-assignments` | Q | risk | |
| `prefer-robust-stmts` | L | ignored (rerunnability, not compatibility) | |
| `prefer-repack` | L | ignored | |
| `prefer-text-field` | L | ignored | |
| `require-lock-timeout` | L | ignored (fires on almost every DDL file, see 2.4) | |
| `require-statement-timeout` | L | ignored | |
| `prefer-bigint-over-int` | S | ignored | |
| `prefer-bigint-over-smallint` | S | ignored | |
| `prefer-identity` | S | ignored | |
| `prefer-timestamp-tz` | S | ignored | |
| `ban-char-field` | S | ignored | |
| `require-enum-value-ordering` | S | ignored | |
| `unused-ignore` | (none) | ignored (we neutralise ignore comments) | |
| `syntax-error` | (none) | parse failure: regex fallback for the file | |

Opt-in rules (never enabled here, D9; the site tags 25 of these 27 "backwards compatibility", all but
`require-table-schema` and the `require-timeout-settings` alias, index.js lines 281-449): `require-table-schema`, `require-timeout-settings`, `ban-drop-trigger`,
`ban-alter-generated-expression`, `ban-drop-index`, `ban-set-default`, `ban-disable-trigger`, `ban-replica-identity`,
`ban-drop-policy`, `ban-revoke`, `ban-replace-view-function`, `ban-alter-policy-condition`, `ban-alter-policy-roles`,
`ban-create-policy`, `ban-alter-function-options`, `ban-alter-view-options`, `ban-alter-role-options`,
`ban-alter-database-options`, `ban-alter-row-level-security`, `ban-new-write-restriction`, `ban-add-enum-value`,
`ban-add-composite-attribute`, `ban-add-column`, `ban-detach-inheritance`, `ban-alter-sequence-values`,
`ban-alter-system-options`, `ban-alter-extension` (lib.rs lines 169-200). Any `rule_name` not in the table above
(a newer squawk's rule) is **ignored** and counted in `tools.json` `squawk.unknown_rules` for the report.

Settings that change rule results (grep of `pg_version|assume_in_transaction` over `rules/*.rs`):
`adding-field-with-default`, `adding-foreign-key-constraint`, `adding-not-nullable-field`,
`adding-serial-primary-key-field`, `adding-required-field` (transaction only: tables created earlier in the same
transaction are skipped, [transactions.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/transactions.rs)
`TablesCreatedInTransaction`, which tracks creates only inside `BEGIN` or with `assume_in_transaction`),
`ban-concurrent-index-creation-in-transaction`, `constraint-missing-not-valid`, `disallowed-unique-constraint`,
`prefer-repack`, `prefer-robust-stmts`, `require-concurrent-*`, `transaction-nesting`. So of the break rules only
`adding-required-field` depends on a setting (D6).

### 2.8 In-file ignore comments

- `-- squawk-ignore <rule>[, <rule>...]` ignores the next statement's violations of those rules; `-- squawk-ignore-file
  [<rules>]` ignores them (or, with no names, everything) for the whole file; unknown names produce `unused-ignore`
  violations — [docs/docs/cli.md](https://github.com/sbdchd/squawk/blob/v2.74.0/docs/docs/cli.md);
  [squawk_linter/src/ignore.rs](https://github.com/sbdchd/squawk/blob/v2.74.0/crates/squawk_linter/src/ignore.rs)
  (`IGNORE_LINE_TEXT = "squawk-ignore"`, `IGNORE_FILE_TEXT = "squawk-ignore-file"`, matched with `strip_prefix` on the
  trimmed comment body, so case-sensitive).
- `-- squawk-disable-assume-in-transaction` turns `assume_in_transaction` off for that file (ignore.rs
  `DISABLE_ASSUME_IN_TRANSACTION`; lib.rs `lint` lines 501-505).
- Ignores are applied after linting by range (`Linter::errors`, lib.rs lines 733-743).

### 2.9 This repository (file:line at ab1edb3)

- `guardlib/contracts_parse.py` 69-87: `_DANGEROUS` (nine regexes, five SQL ones: `DROP COLUMN`, `RENAME COLUMN`,
  `DROP TABLE`, `ALTER COLUMN ... TYPE`, `ALTER TABLE ... RENAME TO`; four Django ones) and `dangerous_statements`
  (strips `--` comments with `re.sub`, which shifts offsets; returns `{"<label>:<name>": label}`). Block comments
  `/* */` and string literals are not stripped. 90-105: `sql_schema` runs all scripts in an in-memory SQLite.
- `guardlib/checks.py` 108-123: `check_migration` (new regex items as `Break("db", "stmt:<item>", "<label>: <name> in a
  migration", "schema", rel)`; SQLite schema diff as `column:<t>.<c>` items, deduplicated against new regex items by
  column name). 277-298: `run_checks` builds a `ToolContext` only for `contracts.http`; any file under `contracts.db`
  (and any `*/migrations/*.py`) goes to `check_migration` without tools.
- `scripts/pre_edit.py` 16-28 `_read_migrations` (top-level `*.sql` of `contracts.db` only); 115-123: tool kwargs only
  for `contracts.http`; 125-163: findings, waiver check `waivers.is_waived(cfg, "contract_break", rel, b.item, code_hash)`
  for every break, decision via `policy.contract_decision`.
- `guardlib/audit.py` 23-34 `_migrations`; 36-67 `_contract_findings` (calls `run_checks` with `trees`, `notes`,
  `tool_timeout`; adds `contract_break` findings; the `not_applied` sweep at 68-70, after `_drift_findings` and
  `_oas_findings` at 66-67); 273-277 `_report_notes`. `audit.run` is called **under the ledger lock**
  (`scripts/stop_audit.py` lines 15-16; `ledger.locked` takes a blocking `flock`, ledger.py lines 45-51), and the Stop
  hook's limit is 240 s, PreToolUse's 10 s (`hooks/hooks.json`).
- `guardlib/ci.py` 158-168 (contract checks with tools for every changed file), 248-269 `annotations`
  (`::warning file=<rel>,title=LegacyGuard contract::...`, no `line=`).
- `guardlib/contract_tools.py` 47 `TOOLS = ("oasdiff",)`; 98-108 `settings`; 211-239 `refresh` (runs `--version` with
  plain `subprocess.run`, 2 s); 242-255 `pre_edit_allowed` and `_bump_timeouts` hard-code `"oasdiff"`; 260-269 `_run`
  (uses `os_sandbox.run`); 486-499 `take_notes`; 520-548 `report_line`.
- `runner/os_sandbox.py` 129-152 `run(cmd, timeout, *, input=None, capture_output, text, cwd, env, ...)`: own session,
  `killpg` SIGKILL and reap on timeout or any exception. With `input=None` the child inherits the hook's stdin.
- `guardlib/config.py` 22 (`contract_tools` defaults), `guardlib/ledger.py` 22 (`SOURCES`), `guardlib/report.py`
  163-168 (the "Contract tools" line is shown only when `contracts.http`, drift, notes or app OpenAPI is present).
- **No test covers the SQL regex path today** (`grep` for `DROP COLUMN`, `dangerous_statements`, `check_migration`,
  `.sql` over `tests/`: no hit outside the Django drift fixtures). Tests 1-3 below pin it before anything changes.

## 3. Security

- **What squawk reads:** the files we pass, and one config file. With `--config <path>` it reads that file and does not
  search (config.rs lines 39-45). Without it, it walks from the cwd up to `/` (2.3). A temp cwd alone is therefore not
  enough: Claude can write `/tmp/.squawk.toml` or `$TMPDIR/../.squawk.toml`. **Always pass `--config
  squawk.toml`** (relative to the temp cwd), a file we write containing exactly `assume_in_transaction = false` (or `true` per D6), and run
  with that temp folder as cwd.
- **What it is given:** copies of the two texts, written by LegacyGuard into a fresh `tempfile.mkdtemp(prefix=
  "lg-squawk-")` as `before.sql` and `after.sql`, passed as the **relative** names `before.sql` and `after.sql` with
  that folder as cwd (paths go through `glob::glob`, file_finding.rs line 41, so an absolute path would make `$TMPDIR`
  part of a glob pattern). squawk never sees the project tree, so no project file (including a
  `.squawk.toml` in the repo, or another migration) can change the result. Inference: squawk has no include mechanism
  (it is not psql; the linter crate has no file I/O).
- **In-file suppressions:** before writing, replace every `squawk-ignore` with `squawk_ignore` and every
  `squawk-disable-assume-in-transaction` with `squawk_disable_assume_in_transaction` (same lengths, so byte offsets and
  line/column positions are unchanged). squawk then sees plain comments. The same rewrite on both sides keeps
  before/after comparable. (A replacement inside a string literal changes the literal, which no break rule reads;
  inference.)
- **Environment:** `os.environ` minus `GITHUB_ACTIONS` and every `SQUAWK_*` variable, plus
  `SQUAWK_DISABLE_GITHUB_ANNOTATIONS=1` (belt and braces for versions before 2.57.0, 2.3). No `--verbose`.
- **stdin:** pass `input=b""` to `os_sandbox.run` so squawk never reads the hook's stdin (paths win anyway when given,
  2.2; this also covers the "pattern matched nothing" path).
- **No OS sandbox wrap.** squawk reads only our temp folder and makes no network calls on this path (2.3); Part A ran
  oasdiff without the sandbox for the same "reads only the two files" case. Wrapping would add sandbox start-up time
  inside the 0.8 s cap. The binary itself is trusted the same way oasdiff is: it is the user's install (PATH or config
  path).
- **Binary location (optional item i):** with `squawk: auto` the binary comes from `shutil.which`, i.e. `PATH`. If
  `PATH` contains a project-local folder Claude can write (`./node_modules/.bin`, `./bin`, a project venv's `bin`),
  Claude could plant a `squawk` there. Under D2 (a) the regex items survive anything squawk prints, so such a binary
  could hide only squawk-only breaks (or slow the hook down to the cap). Same exposure as oasdiff today; a fix for both
  (refuse a resolved path inside `cfg.root`, with a note) is a small follow-up and is listed in section 8, not built
  here.
- **Process control:** `contract_tools._run` already uses `os_sandbox.run` (process-group SIGKILL and reap on timeout).
  `refresh`'s `--version` probe still uses plain `subprocess.run`; switch it to `os_sandbox.run` too (one line), since
  it now probes two tools.
- **Output handling:** temp paths never reach messages: the `file` field is used only to tell `before.sql` from
  `after.sql`; subjects use the repo-relative `rel`.

## 4. Current code paths that change

| File | Change |
|---|---|
| `guardlib/contract_tools.py` | `TOOLS = ("oasdiff", "squawk")`; `settings` defaults `squawk: "auto"`, `squawk_pg_version: None`, `squawk_assume_in_transaction: False`; `pre_edit_allowed(cfg, name="oasdiff")` and `_bump_timeouts(cfg, timed_out, name="oasdiff")`; `refresh` probes with `os_sandbox.run`; new `squawk_lint`, `parse_squawk`, `SquawkResult`, `SQUAWK_CLASSES`, `squawk_items`; `report_line` gains a squawk part. |
| `guardlib/contracts_parse.py` | New `dangerous_statement_spans(text) -> dict[item, list[int]]` (comments blanked with spaces, so offsets are the original text's); `dangerous_statements` unchanged in output (it becomes `{k: label}` of the spans). |
| `guardlib/checks.py` | `Break` gains `line: int | None = None` (default keeps every existing constructor valid); `check_migration(..., tools=None)`; new `_squawk_breaks`; `run_checks` builds a `ToolContext` for `.sql` under `contracts.db` when `trees` is given, with `allow_squawk`. |
| `scripts/pre_edit.py` | Tool kwargs also for `.sql` under `contracts.db` (`allow_squawk=contract_tools.pre_edit_allowed(cfg, "squawk")`). |
| `guardlib/audit.py` | `_contract_findings`: one `tool_state` per Stop; files in `squawk_unchecked` with a kind other than `off` keep their `source == "squawk"` findings in `seen_items` (no false "fixed", D11). |
| `guardlib/ci.py` (run) | One `tool_state` per `ci.run` (time rule, 5.4). |
| `guardlib/ci.py` | `annotations`: `line=<n>` when `b.line` is set. |
| `guardlib/ledger.py` | `SOURCES` gains `"squawk"`. |
| `guardlib/config.py` | `contract_tools` defaults gain `squawk: auto` (comment: `auto | false | <path>`). |
| `guardlib/report.py` | Show the "Contract tools" line also when `contracts.db` is set. |
| `tests/squawk_fake.py`, `tests/test_squawk.py`, `tests/test_sql_migration_checks.py` | New (section 6). |

Nothing changes in the target-side runner files (squawk never runs project code).

## 5. Build plan

### 5.1 Config and discovery

- `contract_tools.squawk: auto | false | <path>`; resolution, caching in `.legacyguard/tools.json`, stat check and
  version probe exactly as for oasdiff (`_resolve`, `_entry`, `find`, `find_reason`, `refresh`). `refresh` now loops
  over both tools; SessionStart resets both pre-edit counters.
- `squawk_pg_version` (squawk parses `major[.minor[.patch]]`, each part with `str::parse::<i32>`, version.rs; an
  overflow or non-number is a clap usage error, exit 2, which would fail every run):
  - YAML `null`/missing: not passed.
  - A `bool` (YAML `true`): refused (bool is an `int` subclass in Python; check it first).
  - An `int` (YAML `13`): `str(value)`, then the string rule.
  - A `float` (YAML `13.4`, and `13.10`, which YAML loads as `13.1`): **refused** with the note, because the float has
    lost what the user wrote (`13.10` and `13.1` are different versions to squawk: minor 10 vs 1). The note asks for a
    quoted string.
  - A `str`: stripped, accepted only if it matches `^[0-9]{1,4}(\.[0-9]{1,4}){0,2}$` (ASCII digits only, `[0-9]` not
    `\d`, which matches other Unicode digits in Python; at most 4 digits per part, far below `i32::MAX`).
  - Refused values are not passed; note kind `squawk-config`: "contract_tools.squawk_pg_version must be a quoted
    version like "13" or "13.4"; squawk used its default, Postgres 15."
- `squawk_assume_in_transaction`: `True` only for the YAML boolean true.

### 5.2 `contract_tools.squawk_lint(cfg, rel, before, after, timeout, pre_edit=False) -> SquawkResult | Refusal`

Never raises. Steps:

1. `find("squawk", cfg)`; none -> `Refusal` kind `off` (switched off, no note) or `missing` (note kind `squawk`, text
   "squawk was not found, so the SQL migration check used its built-in patterns. Install squawk (`pip install
   squawk-cli`) or set contract_tools.squawk in .legacyguard/config.yaml."). A configured path that is not an
   executable file (`find_reason` starts with "configured path") -> `missing` with the text f"squawk {reason}, so the
   SQL migration check used its built-in patterns." (as `oasdiff_breaking` does), i.e. "squawk configured path <value>
   not found or not executable, so the SQL migration check used its built-in patterns."
2. `tmp = tempfile.mkdtemp(prefix="lg-squawk-")`. Prepare each text (`prepare(text)`, also used by 5.3): strip one
   leading `\ufeff`; replace `\r\n` with `\n`, then any remaining `\r` with `\n` (2.4); apply the ignore
   neutralisation (3); encode UTF-8 (an encode error -> `Refusal("failed")`). Write `after.sql`; write `before.sql`
   only when `before` has non-whitespace text. Write `squawk.toml` (`assume_in_transaction = <bool>`).
3. Command: `[path, "--reporter", "json", "--config", "squawk.toml"]` + (`["--pg-version", v]` if configured) +
   (`["--assume-in-transaction"]` if configured) + `["before.sql"]` (if written) + `["after.sql"]`, all relative to
   the cwd `tmp` (step 4), so no `$TMPDIR` text reaches squawk's glob.
   Never `check`, `--exclude`, `--include`, `--stdin-filepath`, `--verbose`.
4. `_run(cmd, timeout, cwd=tmp, env=<3>, input=b"", text=False)`. New signature:
   `_run(cmd, timeout, cwd, env, input=None, text=True) -> (Completed | None, reason, timed_out)`; it passes `input`
   and `text` to `os_sandbox.run(cmd, timeout, input=input, capture_output=True, text=text, cwd=cwd, env=env)`. The
   oasdiff call (`_run(cmd, timeout, tmp, env)`, contract_tools.py 448) and `run()` keep the defaults, so oasdiff is
   unchanged (text mode, inherited stdin). When `text=False`, `_run` itself decodes `res.stdout` and `res.stderr` with
   `.decode("utf-8", errors="replace")` before building `Completed`, so `Completed.stdout`/`stderr` are always `str`
   and a stray byte never raises (optional item ii). The `TimeoutExpired` branch needs no decode (it discards output). Update `tools.json` `squawk.last_seconds`/`last_error`. With `pre_edit=True`, a timeout bumps
   the squawk counter, a run in time resets it (as oasdiff).
5. Timeout -> `Refusal("squawk took longer than {t} s on {rel}; the built-in patterns were used.", "timeout")`.
   Start failure (`_run` could not start the process) -> kind `unstartable` (note kind `squawk-failed`).
6. `parse_squawk(stdout)`; `None` -> `failed` with stderr's first line when the exit code is non-zero (e.g.
   "Configuration error: ..."), else "printed output LegacyGuard could not read". Exit code not in (0, 1), or 0 with a
   non-empty list, or 1 with `[]` -> `failed` ("squawk exited with code N").
7. Split violations on exact equality: `file == "before.sql"` / `file == "after.sql"` (squawk prints the path as
   given, reporter.rs line 210); any other value -> `failed`.
   Any `level == "Error"` entry on a side -> `Refusal(f"squawk could not parse {rel} as PostgreSQL (line {line+1}:
   {message[:120]}); the built-in patterns were used.", "parse")`, naming the side ("the new text" / "the old text").
8. Return `SquawkResult(before=[...], after=[...], seconds)`.
9. `finally`: `shutil.rmtree(tmp, ignore_errors=True)`.

`parse_squawk(stdout) -> list[dict] | None`: `json.loads`; must be a list of dicts; per element require `file`
(str), `line` and `column` (int, not bool, >= 0), `level` in `{"Warning", "Error"}`, `rule_name` (str), `message`
(str); `line_end`/`column_end` optional ints (default to `line`/`column`), `note`/`help` str or None. Any element that
fails -> the whole output is rejected (as Part A's `parse_changes`).

### 5.3 Items, classes and dedupe (`contract_tools.squawk_items`, `checks._squawk_breaks`)

- **One text for everything:** squawk windows, regex spans and SQLite-item matching all use the prepared text of 5.2
  step 2 (BOM stripped, `\r\n`/`\r` normalised to `\n`, ignore comments neutralised). In particular
  `cp.dangerous_statement_spans(prepare(after))`, not the raw text, so its character offsets and squawk's line/column
  refer to the same string. (The neutralisation does not change what the regexes match; normalised newlines only change
  `\s` runs.) The regex *items* reported as breaks still come from `dangerous_statements(after)` on the raw text, as
  today, so their keys do not change.
- **Offsets:** for one side's prepared text `s`, `b = s.encode()`; line starts are the byte offsets after each `\n`
  (the only line break left in `s`); `start = line_starts[line] + column`, `end = line_starts[line_end] + column_end`, clamped to
  `len(b)`. Character offsets for the regex overlap: `len(b[:start].decode(errors="ignore"))`.
- **Statement window:** first build `blank`, a copy of `b` where every `--...` (to end of line) and every `/* ... */`
  (non-nested; an unclosed one runs to the end) is overwritten with spaces of the same byte length, newlines kept. Find
  the boundaries on `blank`: from `start`, back to just after the previous `;` (or 0), forward to the next `;` at or
  after `end` (or the end). So a `;` inside a comment never splits a statement. Normalise the slice of `blank`: decode,
  remove `"` and `` ` ``, collapse whitespace, lowercase, strip. Empty -> `"line-<line+1>"`. (Heuristic: a `;` inside
  a string literal or a dollar-quoted body still moves the window; it only affects key stability, see section 8.)
- **Item key:** `f"squawk:{rule_name}:{window[:80]}:{_sha12(window)}"` with the whole normalised window hashed
  (`contract_tools._sha12`, as Part A's oasdiff items), so two long statements that share their first 80 (or 120)
  characters get different keys, and the key stays readable in a waiver. **Class:** `SQUAWK_CLASSES[rule_name]` from the table in 2.7; missing
  -> ignored (count in `tools.json` `squawk.unknown_rules`, capped list of names).
- **New in the edit:** `new = {key for after violations} - {key for before violations}` (set difference, as the
  regex path does). For a new file every after violation is new. When the same key appears twice in `after`, one item
  is reported (its first line).
- **Dedupe against the regexes (D2 a):** `spans = cp.dangerous_statement_spans(prepare(after))` restricted to new
  regex items.
  `OVERLAP = {"ban-drop-column": "column dropped", "renaming-column": "column renamed", "ban-drop-table": "table
  dropped", "changing-column-type": "column retyped", "renaming-table": "table renamed"}`. A new squawk break whose rule
  is in `OVERLAP` is dropped when some new regex item with that label has a span offset inside the squawk statement
  window (character offsets in the prepared after text). 
- **Dedupe against the SQLite schema diff (correction 11):** Postgres accepts `ALTER TABLE t DROP c` and `ALTER TABLE t
  RENAME c TO d` (no `COLUMN` keyword). The `_DANGEROUS` regexes require `DROP\s+COLUMN` / `RENAME\s+COLUMN`
  (contracts_parse.py 69-70), so no regex item exists, squawk reports `ban-drop-column` / `renaming-column`, and
  `check_migration`'s SQLite diff also reports `column:t.c` ("column dropped: t.c", checks.py 113-119) when the scripts
  run in SQLite. A new squawk break with rule `ban-drop-column` or `renaming-column` is dropped when some `column:<t>.<c>`
  item that `check_migration` reports in this call has both `<t>` and `<c>` as whole words (case-insensitive,
  `\b...\b`) in the normalised window; the SQLite item, which names table and column, stands. When the SQLite diff
  did not run (`sql_schema` returned None, e.g. Postgres-only syntax), the squawk item is the only report and is kept.
- Every other new break is kept.
- **Break:** `Break("db", item, subject, "squawk", rel, line=line + 1)` with
  `subject = f"{message} (squawk {rule_name}, line {line + 1}): {window[:80]}"`, e.g. "dropping a view may break
  existing clients (squawk ban-drop-view, line 3): drop view v". The line is 1-based in the prepared text, which equals
  the line an editor shows (universal newlines).
- **Risk note** (kind `None`, shown every time, one per file): `f"LegacyGuard: squawk also reports {n} locking or
  run-time risk{'s' if n != 1 else ''} in {rel}: {rule} (line {l}); ..."` (first 3, then "+N more").

### 5.4 `checks.check_migration` and `run_checks`

```text
check_migration(rel, scripts_before, scripts_after, before, after, tools=None):
    out = <today's regex and SQLite items, unchanged>
    if tools is not None and rel.endswith(".sql"):
        try:   out += _squawk_breaks(rel, before, after, tools)   # [] on any refusal, with a note
        except Exception: note kind "squawk-failed" ("squawk step failed: <type>"), mark rel unchecked, kind "failed"
    return list(out.values())
```

- `_squawk_breaks`: `if not tools.allow_squawk`: note kind `squawk-slow` ("squawk took longer than {t} s here twice in a
  row; the pre-edit check now uses the built-in patterns, and the Stop audit still runs squawk."), mark unchecked,
  return `[]`. Otherwise call `squawk_lint`; a `Refusal` adds its note (kinds `squawk`, `squawk-failed`,
  `squawk-timeout`, `squawk-parse`; `unstartable` uses `squawk-failed`; `off` adds none), marks `rel` in `state["squawk_unchecked"]` with its kind,
  returns `[]`.
- `ToolContext` gains `allow_squawk: bool = True` and `state: dict | None = None`.
- **Run state across files:** `run_checks(..., allow_squawk=True, tool_state=None)`. `tool_state` is a dict the caller
  creates once per run and passes for every file: `audit._contract_findings` (one per Stop) and `ci.run` (one per
  run); `pre_edit.py` passes a fresh one (one file anyway). Keys: `squawk_unchecked: set[str]` (files squawk did not
  lint this run, with the reason kind in `squawk_unchecked_kind[rel]`: `off`, `missing`, `timeout`, `unstartable`,
  `failed`, `parse`, `slow`, `stopped`; the exception branch of `check_migration` records `failed`. Every kind except
  `off` counts as "not off" for D11, so those files' open squawk findings stay open at Stop) and `squawk_stopped: str | None`. When `trees is not None` and `rel` is under `contracts.db` and
  ends with `.sql`, `run_checks` builds the `ToolContext` with `state=tool_state` and passes it to `check_migration`.
  Django `.py` migrations never get it. `run_checks` still returns breaks only.
- **Time rule (correction 4), chosen: stop after the first bad run.** In `_squawk_breaks`, if
  `state["squawk_stopped"]` is set, do not start squawk: mark `rel` unchecked (kind `stopped`) and add one note kind
  `squawk-stopped` (added only for the first skipped file of the run: "LegacyGuard: squawk timed out or could not start
  on <first rel>, so it was not run on the remaining SQL migrations of this run; the built-in patterns were used for
  them."). A `Refusal` of kind `timeout` or `unstartable` sets `squawk_stopped` to that file's `rel`. Bad output, a parse error or an exit-code mismatch does not (they
  are about one file's text or are cheap). Why this and not the alternatives: one squawk process over all files would
  need per-file before/after names, makes one unreadable file fail the batch, and changes the PreToolUse code path for a
  Stop-only problem; a total budget still allows several near-timeouts under the lock. With this rule a Stop or
  `ci.run` costs at most one full timeout (20 s, plus up to 5 s of reap) plus fast runs, inside the Stop hook's 240 s
  and under the ledger lock no longer than that. A slow binary that finishes each file just under 20 s is not caught
  (each run "in time"); with tens of SQL files that could still be long, so section 8 lists it.

### 5.5 Hooks

- **PreToolUse** (`pre_edit.py`): for `rel` under `cfg.contract_path("db")` ending `.sql`, pass `trees=(root, root)`,
  `notes=tool_notes`, `tool_timeout=settings["pre_edit_timeout_s"]` (0.8), `allow_squawk=pre_edit_allowed(cfg,
  "squawk")`, `pre_edit=True`. Everything after is today's code: squawk breaks are `contract_break` findings with
  `source="squawk"`, `contract_kind: "db"`, `code_hash=sha(after)`, waived per item, decision per mode (advisory
  context, guarded ask, strict deny); notes shown through `take_notes`.
- **PostToolUse:** no change (D1).
- **Stop** (`audit._contract_findings`): no new call; it creates one `tool_state` and passes it with `trees`,
  `notes`, `tool_timeout=20` to every `run_checks`. `before` is the baseline text (None for a new file -> after only).
  After the loop, before the sweep (D11 a): for each `rel` in `squawk_unchecked` whose kind is **not** `off`, add
  `(f["file"], f["item"])` of every `contract_break` finding with `source == "squawk"`, that file, and state
  `open`/`applied` to `seen_items`, so a missing, timed-out, failed, stopped or unparsable squawk run does not sweep
  them to `not_applied`. Kind `off` (switched off) adds nothing, so the sweep closes them, as for drift. A squawk that
  ran and no longer reports an item sweeps it as today. Notes go through `_report_notes`.
- **CI** (`ci.run`): `contract_files` already includes changed `.sql` files; `run_checks` gets tools and one
  `tool_state` for the run (time rule above); breaks after the
  waiver key; notes in `out.notes`. `annotations`: `::warning file=<rel>,line=<b.line>,title=LegacyGuard
  contract::<text>` when `b.line` is set. Counts toward failure only under `fail_on: any` (unchanged).
- **Ledger and report:** `SOURCES += ("squawk",)`. `report_line`: a part `squawk <version> (<path>), last run
  <s> s` / `last run failed (...)` / `squawk switched off` / `squawk not found`, the last only when `contracts.db` is
  set; plus `, 1 unknown rule ignored` / `, <n> unknown rules ignored` (n >= 2; `rule` when n == 1, `rules`
  otherwise, as the existing `run`/`runs` parts do) when `unknown_rules` is non-empty. `report.py` shows the line also when
  `contracts.db` is set.

### 5.6 Time budget and fail-open

- PreToolUse: one squawk process per `.sql` edit, capped at `pre_edit_timeout_s` (0.8 s); the regex and SQLite checks
  run as today (they are not skipped). Worst case per edit: 0.8 s plus the 5 s reap bound in `os_sandbox._reap` if a
  killed process does not exit (same as oasdiff). Skip rule: two timeouts in a row -> squawk skipped at PreToolUse for
  the session (reset at SessionStart or when the binary or config changes); Stop and CI still run it with 20 s.
- Stop and `ci.run`: at most one squawk timeout per run (the time rule in 5.4); the Stop audit holds the ledger lock
  meanwhile (2.9), so a PreToolUse hook of a parallel session waits at most that long for the lock.
- Every failure path returns the regex result unchanged plus at most one note per kind per session. No path raises out
  of `check_migration`. A config value that is not a number for the timeouts already makes `run_checks` fall back to
  `tools=None` (today's behaviour, checks.py 287-288).

## 6. Tests (run one file at a time, never in parallel)

### 6.1 `tests/squawk_fake.py`

A Python script installed at `<base>/bin/squawk`, like `tests/oasdiff_fake.py`. It appends `{"argv", "cwd", "env":
sorted names starting with SQUAWK or GITHUB, "config": <text of the --config file or None>, "files": {name: text}}` to
`<base>/calls.jsonl`, then behaves per `<base>/mode`:

- `--version` is handled **first**, before anything else (as `tests/oasdiff_fake.py` does): append a line to
  `versions.log`, print `squawk 2.74.0`, exit 0. It never writes `calls.jsonl`, so the `refresh()` probes at
  SessionStart, Stop and `ci.run` (contract_tools.py 216-234) do not count as runs (tests 4 and 24b count
  `calls.jsonl` entries).
- `lint` (default): for each path argument (refuses if a path is missing: stderr "Failed to find files", exit 1),
  reads the text and emits violations in the v2.74.0 shape (all ten fields in snapshot order, `level` `"Warning"`,
  zero-based `line`, zero-based UTF-8 byte `column`), for simple case-insensitive patterns, each range covering the
  matched words: `drop column <x>` -> `ban-drop-column`; `drop view <x>` -> `ban-drop-view`; `drop table <x>` ->
  `ban-drop-table`; `rename column` -> `renaming-column`; `create index` without `concurrently` ->
  `require-concurrent-index-creation`; `add column <x> <type> not null` without `default` -> `adding-required-field`
  and `prefer-robust-stmts`; `alter table <t> drop <x>` without `column` -> `ban-drop-column` (test 11b);
  `alter table` -> `require-lock-timeout`; the token `FAKE_NEW_RULE2` -> `ban-other-new` (checked before the shorter
  token) and the token `FAKE_NEW_RULE` -> `ban-something-new` (test 14); the token `SYNTAXERR` -> only `{"rule_name": "syntax-error", "level": "Error", ...}` for that
  file. Honours `-- squawk-ignore-file` like squawk (drops that file's violations) so test 7 proves the neutralisation.
  Prints the flat array; exit 1 if non-empty, else `[]` and exit 0.
- Failure modes: `garbage` (prints `not json`, exit 1), `object` (`{}`), `rc2` (usage error text on stderr, exit 2),
  `config-error` (stderr `Configuration error: ...`, exit 1, no stdout), `bad-field` (a `line` of `true`),
  `rc0-nonempty`, `slow` (sleeps 3 s, then behaves as `lint`; tests 22, 24, 24b), `hang-child` (forks a child that
  sleeps, then sleeps; test 20).
- Variant modes (behave as `lint` plus one change): `drop-attr` also reports `ban-drop-column` on `alter type <x> drop
  attribute <a>` (test 11); `abs-file` prints every violation with an absolute `file` (the path joined to the cwd)
  instead of the name as given (test 7).
- Modes and tokens used by the tests, in one list: modes `lint`, `drop-attr`, `abs-file`, `garbage`, `object`, `rc2`,
  `config-error`, `bad-field`, `rc0-nonempty`, `slow`, `hang-child`; tokens `FAKE_NEW_RULE`, `FAKE_NEW_RULE2`,
  `SYNTAXERR`.
- The module docstring says: shape pinned from squawk's own `display_violations_json` and `skip_lint_on_syntax_error`
  snapshots at v2.74.0 (reporter.rs); the patterns are a stand-in, not squawk's rules.

### 6.2 `tests/test_sql_migration_checks.py` (no squawk; pins today's behaviour first)

1. `check_migration` on a new `.sql` with `ALTER TABLE t DROP COLUMN c;` gives exactly one break,
   `stmt:column dropped:c`, subject "column dropped: c in a migration".
2. `RENAME COLUMN`, `DROP TABLE IF EXISTS`, `ALTER COLUMN x TYPE`, `ALTER TABLE t RENAME TO`: one item each; a statement
   already in `before` is not reported; a `-- DROP COLUMN c` comment is not reported.
3. `dangerous_statement_spans(text)` keys equal `dangerous_statements(text)` keys on a corpus of the above plus a
   Django `RemoveField`; each span offset points at the matched keyword in the original text (comments blanked, not
   removed).
4. `run_checks` with `trees` but `contract_tools.squawk: false`: identical breaks to `trees=None` for every case above
   (no process started: the fake's `calls.jsonl` stays empty).

### 6.3 `tests/test_squawk.py` (fake binary only)

5. `parse_squawk` accepts squawk's pinned snapshot strings from 2.4 and 2.6 verbatim; returns 8 and 1 elements.
6. `parse_squawk` rejects: not a list, a non-dict element, missing `rule_name`, `line: true`, `level: "Info"`,
   negative `column`.
7. Command and isolation: argv is exactly `["--reporter", "json", "--config", "squawk.toml", "before.sql",
   "after.sql"]` (relative; no `check`, `--exclude`, `--include`, `--stdin-filepath`); the recorded cwd is a folder
   named `lg-squawk-*` that holds those three files; the config text is `assume_in_transaction = false`; env has
   `SQUAWK_DISABLE_GITHUB_ANNOTATIONS` and no `GITHUB_ACTIONS` even when the test sets `GITHUB_ACTIONS=true` and
   `SQUAWK_GITHUB_TOKEN=x`. Decoy configs: the test monkeypatches `tempfile.tempdir` to `tmp_path / "tmpdir"` (so the
   real `$TMPDIR` is never written) and writes a `.squawk.toml` there (the temp folder's parent) and in the fixture
   project root; the config the fake reads is ours. The temp folder is gone afterwards. The fake splits its output on
   exact `file` values `before.sql`/`after.sql`; a fake mode `abs-file` that prints an absolute `file` gives a
   `squawk-failed` refusal.
8. Ignore neutralisation: after text `-- squawk-ignore-file\nDROP VIEW v;` still gives the `ban-drop-view` break; the
   text the fake received contains `squawk_ignore-file`, has the same byte length, and the reported line is 2.
9. New file (before empty): only `after.sql` is passed; `DROP VIEW v;` -> one break, item
   `"squawk:ban-drop-view:drop view v:" + sha256("drop view v")[:12]`, source `squawk`, `line == 1`, subject contains "ban-drop-view".
10. Not new: the same `DROP VIEW v;` in before and after (with different whitespace and case) -> no break.
11. Dedupe (D2 a), keys in the new form `squawk:<rule>:<window[:80]>:<sha12>`: `ALTER TABLE t DROP COLUMN c;` new -> exactly one break, the regex item `stmt:column dropped:c`;
    `ALTER TABLE t DROP COLUMN c; ALTER TABLE u DROP COLUMN d;` -> two regex items, no squawk item. A squawk
    `ban-drop-column` on a statement the regex does not match (fake mode `drop-attr` reports `ban-drop-column` on
    `ALTER TYPE x DROP ATTRIBUTE a`, as squawk does, ban_drop_column.rs) is kept as a squawk item.
11b. SQLite dedupe (correction 11): before scripts `CREATE TABLE t (a int, c int);`, a new file `ALTER TABLE t DROP c;`
    (the fake reports `ban-drop-column` on `drop c`): exactly one break, `column:t.c` ("column dropped: t.c"); no regex
    item, no squawk item. The same statement with a table the SQLite run does not know (scripts fail in SQLite, so
    `sql_schema` is None): the squawk `ban-drop-column` item is kept.
12. Risk only: `CREATE INDEX i ON t (c);` -> no break; a note naming `require-concurrent-index-creation (line 1)`.
13. Ignored: an `ALTER TABLE` that only triggers `require-lock-timeout` and `prefer-robust-stmts` -> no break, no note.
14. Unknown rule (`FAKE_NEW_RULE`) -> ignored, `tools.json` `squawk.unknown_rules == ["ban-something-new"]`, the report
    line says "1 unknown rule ignored"; with a second unknown name (`FAKE_NEW_RULE2` -> `ban-other-new`) it says
    "2 unknown rules ignored".
15. Parse failure: `SYNTAXERR` in after -> regex items only, note kind `squawk-parse` naming "the new text" and the
    line; in before only -> regex items only, note naming "the old text".
16. Missing binary -> regex items only, note kind `squawk` once per session (`take_notes` twice -> shown once);
    `contract_tools.squawk: false` -> no note, no run; configured path that does not exist -> note exactly
    "LegacyGuard: squawk configured path <value> not found or not executable, so the SQL migration check used its
    built-in patterns."; a configured path to a non-executable file -> the same note.
17. Failure modes `garbage`, `object`, `rc2`, `config-error`, `bad-field`, `rc0-nonempty` -> regex items only, note
    kind `squawk-failed`; the `config-error` note includes "Configuration error".
18. UTF-8 columns: `DROP VIEW "vé";` preceded on the same line by `/* é */ ` -> the window is `drop view vé`; a file
    starting with a BOM gives the same item as without.
18b. Long statements (correction 3): two new statements `DROP VIEW <name>;` whose names are 130 characters long and
    share the first 125 (so the normalised windows share a 120-character prefix) give two
    distinct items whose first 80 characters are equal and whose sha12 parts differ. A `;` inside a `--` comment and
    inside a `/* */` comment before the violation does not change the window (same key as without the comments).
19. `squawk_pg_version: "13"` -> `--pg-version 13` in argv; YAML `13` (int) -> `--pg-version 13`; YAML `13.4` and
    `13.10` (floats) -> not passed, note kind `squawk-config`; `"13.10"` (string) -> `--pg-version 13.10`; `"thirteen"`,
    `"１３"` (full-width digits), `"99999"`, `"13.4.1.2"`, `true` -> not passed, note kind `squawk-config`;
    `squawk_assume_in_transaction: true` -> `--assume-in-transaction` and the config file says `true`.
20. Process group: mode `hang-child` with timeout 0.3 s -> `Refusal` kind `timeout` in under 1.5 s and both pids gone
    (as `tests/test_process_group.py` checks).

### 6.4 Hooks (fake binary; `tests/test_squawk.py`, hook tests in the style of `tests/test_pre_edit_oasdiff.py`)

21. PreToolUse, guarded mode, `db/0002.sql` written with `DROP VIEW v;`: decision `ask`, reason contains "ban-drop-view";
    the finding has `source == "squawk"`, `contract_kind == "db"`, item as in test 9.
    Strict -> `deny`; advisory -> context only. After `/legacyguard:waive` on that finding the same edit is allowed.
22. PreToolUse timing: `slow` fake -> the hook returns in under 1.2 s wall time with the regex result and a
    `squawk-timeout` note; two such edits -> the third does not start squawk and shows the `squawk-slow` note once;
    a Stop and a `ci.run` keep the counter; SessionStart resets it; the oasdiff counter is untouched by squawk
    timeouts and vice versa.
23. PreToolUse on a Django migration `.py` under `contracts.db`: squawk not called (`calls.jsonl` empty).
24. Stop: a shell-written new `db/0003.sql` with `DROP FUNCTION f;` -> a `contract_break` finding, source `squawk`,
    state `open`, evidence "found by the session audit"; a second Stop adds no duplicate; switch the fake to `slow` and
    Stop again -> the finding stays `open` (not swept); restore `lint`, delete the statement, Stop -> `not_applied`.
    D11 cases (each from a fresh open finding on `DROP FUNCTION f;`): (a) remove the fake from the config path
    (missing) and Stop -> stays `open`; (b) add `SYNTAXERR` to the file (it stops parsing) and Stop -> stays `open`,
    with a `squawk-parse` note; (c) set `contract_tools.squawk: false` and Stop -> `not_applied`, and the report line
    says "squawk switched off".
24b. Time rule (correction 4): three new `.sql` files under `db/`, fake `slow` (sleeps 3 s), Stop with
    `timeout_s: 1`: the fake is started once (`calls.jsonl` has one entry), the audit returns in under 5 s, all three
    files get their regex results, and exactly one `squawk-stopped` note names the first file; the other two are in
    `squawk_unchecked` with kind `stopped`. The same through
    `ci.run`. With the fake in `garbage` mode instead, all three files are given to squawk (bad output does not stop
    the run).
25. CI: `ci.run` on a git fixture whose PR adds `db/0002.sql` with `DROP VIEW v;` -> `out.breaks` has the squawk break;
    `annotations` has `::warning file=db/0002.sql,line=1,title=LegacyGuard contract::`; a risk note is in `out.notes`
    and in the markdown report's "Contract tool notes".
26. Report: `report_line` shows `squawk 2.74.0 (<path>), last run <s> s`; with no binary and `contracts.db` set,
    "squawk not found"; with no `contracts.db`, no squawk part. `ledger.add_finding(source="squawk")` is accepted.

### 6.5 Real binary (each test skips when `shutil.which("squawk")` is None)

27. Run `squawk_lint` on a fixture pair (before: `CREATE TABLE t (id bigint);`; after: adds `ALTER TABLE t DROP COLUMN
    c;`, `DROP VIEW v;`, `CREATE INDEX i ON t (c);`): parses; `ban-drop-column` and `ban-drop-view` on the expected
    zero-based lines; `require-concurrent-index-creation` present; every element has exactly the ten keys of 2.4.
28. Syntax error (`ALTER TABLE foo ALTER CONSTRAINT bar RENAME TO quux;`, squawk's own example) -> `parse` refusal.
29. A `.squawk.toml` with `excluded_rules = ["ban-drop-view"]` in the fixture project root and in the temp parent
    (`tempfile.tempdir` monkeypatched to a folder under `tmp_path`, as in test 7; the real `$TMPDIR` is never written)
    does not hide `ban-drop-view`; `-- squawk-ignore-file` in the SQL does not hide it.
30. Timing: 10 runs of test 27's pair, print median and max; assert max < `pre_edit_timeout_s`.
31. CRLF (fake and real binary): an after text written by a PreToolUse `Write` with `\r\n` line ends gives the same
    items, lines and windows as the LF version, and the file squawk received has no `\r`.
31b. Lone `\r` (old Mac line ends, fake and real binary): `CREATE TABLE t (id bigint);\rDROP VIEW v;\r` gives
    `ban-drop-view` on line 2 with the window `drop view v`, the same as the LF version. (Without the normalisation the
    real squawk would count the `\r` as a line break while the old code counted `\n` only, so the line would be wrong.)

### 6.6 If D8 is yes: one manual verification run (implementer, scratchpad venv, nothing committed)

`python -m venv <scratch>/sq && <scratch>/sq/bin/pip install squawk-cli==2.74.0`; then run tests 27-31 with
`PATH=<scratch>/sq/bin:$PATH`, plus by hand: `squawk --version`; `printf 'select (;' | squawk --reporter json`
(stdin path, exit code); `squawk --reporter json --config <empty file> x.sql` (empty config accepted?);
`squawk --reporter json --exclude=no-such-rule x.sql` (exit code 2?); a block comment `/* ALTER TABLE t DROP COLUMN
c; */` (does squawk report it? input for D2 c). Record the outputs in the build report and fix the fake if any shape
differs.

## 7. Acceptance criteria

1. With squawk absent or `contract_tools.squawk: false`, every SQL migration result (breaks, items, subjects) is
   byte-identical to today's on tests 1-4; the full suite passes with the same skip count plus the new real-binary
   skips (tests 27-31).
2. With the fake: tests 5-26 (with 11b, 18b, 24b) pass; a `DROP VIEW` in a new `.sql` under `contracts.db` asks in guarded mode at
   PreToolUse, is a `contract_break` finding at Stop and a `::warning` with `line=` in CI.
3. squawk is never started without `--config squawk.toml` and a fresh `lg-squawk-*` temp folder as cwd, with an
   absolute path argument, with the project root as cwd, with `GITHUB_ACTIONS` or
   another `SQUAWK_*` variable (except `SQUAWK_DISABLE_GITHUB_ANNOTATIONS`), with `check`, `--include` or
   `--exclude`, or through anything but `os_sandbox.run` (test 7, 20; a grep in review).
4. A `squawk-ignore` comment written into the migration does not hide a break (test 8; test 29 with the real binary).
5. Every failure (missing, off, timeout, bad output, parse error, internal exception) leaves the regex result
   unchanged and adds at most one note per kind per session (tests 15-17, 22, 24b); none raises out of the hook.
6. PreToolUse with a hanging squawk returns within 1.2 s (test 22); after two timeouts squawk is skipped at PreToolUse
   only.
7. A squawk failure, a missing binary or a file that stopped parsing at Stop does not turn an open squawk finding into
   `not_applied`; switching squawk off does (D11; test 24). A Stop or `ci.run` starts at most one squawk process that
   times out (test 24b).
8. Waiver keys: regex items keep their `stmt:<label>:<name>` keys whether or not squawk ran (tests 4, 11); squawk items
   are `squawk:<rule>:<first 80 characters of the normalised statement>:<sha12 of the whole>` (tests 9, 18b).
9. `CLAUDE.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md` and `docs/HANDOFF.md` describe Part C, its limits and what is
   unverified (written by the coordinator; part of the review).

## 8. Known limits to document

- squawk lints PostgreSQL only. A file it cannot parse (other dialects, psql meta-commands, a newer syntax than the
  installed squawk knows) gets the regex check alone, with a note.
- squawk names no objects in its messages; squawk items use the normalised enclosing statement (`;` to `;`), so a `;`
  inside a string or a dollar-quoted function body before the violation moves the window. A reworded statement is a
  new item (a waiver on it asks again), as the regex keys do for renamed objects.
- Squawk-only items exist only where squawk ran: a PreToolUse timeout or skip followed by a successful Stop gives a new
  `open` finding at Stop that blocks the Stop once (the edit was never asked about); a CI job without squawk reports
  none of them. A waiver holds only while the file's hash is unchanged (regex and squawk items alike), so any later edit
  of that migration asks again.
- At Stop and in `ci.run`, after the first squawk timeout or start failure the remaining SQL files of that run get the
  regex check only (one note). A binary that is slow but finishes each file just under `timeout_s` is not caught by
  that rule; with many SQL files a Stop can then hold the ledger lock for a long time (bounded by the hook's 240 s).
- Switching squawk off sweeps open squawk findings to `not_applied` (D11), as drift does; a missing, failing or
  unparsable run keeps them open until squawk runs again.
- Compatibility-relevant opt-in rules are not enabled (D9), so LegacyGuard does not report what they would: dropped
  triggers (`ban-drop-trigger`), indexes (`ban-drop-index`), policies (`ban-drop-policy`); `create or replace` of a view
  or function (`ban-replace-view-function`); changed generated expressions (`ban-alter-generated-expression`); new
  defaults (`ban-set-default`); disabled triggers (`ban-disable-trigger`); replica identity (`ban-replica-identity`);
  revokes (`ban-revoke`); policy, role, function, view, database, system and extension option changes and row-level
  security (`ban-create-policy`, `ban-alter-policy-condition`, `ban-alter-policy-roles`, `ban-alter-function-options`,
  `ban-alter-view-options`, `ban-alter-role-options`, `ban-alter-database-options`, `ban-alter-system-options`,
  `ban-alter-extension`, `ban-alter-row-level-security`); new write restrictions, enum values, composite attributes,
  columns, detached inheritance and sequence changes (`ban-new-write-restriction`, `ban-add-enum-value`,
  `ban-add-composite-attribute`, `ban-add-column`, `ban-detach-inheritance`, `ban-alter-sequence-values`).
- `squawk: auto` takes the binary from `PATH`; a project-local folder on `PATH` lets the edited project supply the
  binary (section 3). Only squawk-only breaks could be hidden that way; refusing a resolved path inside the project is
  a follow-up for squawk and oasdiff alike.
- `squawk_pg_version` written as an unquoted YAML float (`13.4`) is refused with a note (YAML may have changed it).
- Only the five regex-covered rules are deduplicated (plus `ban-drop-column`/`renaming-column` against SQLite `column:`
  items); if the regex and squawk disagree on where a statement is
  (e.g. a regex hit inside a `/* */` block comment), both can be reported, or a regex false positive stands (D2 c is
  the follow-up).
- `adding-required-field` fires on `ADD COLUMN ... NOT NULL` without a default to a table created earlier in the same
  file unless the file uses `BEGIN` or `squawk_assume_in_transaction` is set.
- Risk (locking) rules are notes only; they never prompt or block, and the report shows at most 4 contract-tool notes.
- `.squawk.toml` and `squawk-ignore` comments are deliberately not honoured; projects that rely on them see more
  findings than their own squawk CI would show (waive them).
- Only `.sql` files under `contracts.db` are checked; Django `RunSQL` strings and Alembic `op.execute` are not.
- Rules added in later squawk versions are ignored until added to `SQUAWK_CLASSES` (the report counts them).
- squawk's own test shows a plain `ADD COLUMN ... NOT NULL` gives five violations; LegacyGuard reports one of them
  (`adding-required-field`) as a break.

## 9. What is unverified

- **The real binary was not run.** Unverified: the exact JSON bytes from a real run (read only from
  `fmt_json`/`ReportViolation` and squawk's inline snapshots), the timing (start-up and per file) and whether it fits
  the 0.8 s cap, the `--version` text, the exit code for an `anyhow` error (1 by Rust convention) and for a panic
  (101), and that a one-line `--config` file (and an empty one) parses.
- That `pip install squawk-cli` puts a runnable `squawk` on the venv's `bin/` (maturin `bindings = "bin"`, inference).
- JSON shape and flag set of squawk versions before 2.74.0 (the parser rejects other shapes, so they fall back; the
  annotation clobbering fix is in 2.57.0).
- Line breaks were read in the line index source (`\n`, `\r\n`, lone `\r`, 2.4) but not run; the normalisation makes
  the plan independent of it. Whether the parser treats other characters (`\x0b`, `\x0c`, `\u2028`) as anything but
  whitespace was not read; they are not line breaks in squawk's index or in ours.
- That the CLI lint path makes no network call and writes no file (inference from the linter crate's dependencies and
  reporter.rs; the binary also links the language server and GitHub upload code).
- That an unknown `--exclude`/`--include` name exits 2 (clap default; squawk's tests show 2 only for an argument
  conflict). Not used by this plan.
- How often real SQL migrations (especially SQLite-flavoured ones) fail squawk's parser, and whether squawk reports
  statements inside block comments or strings (D2 c).
- Linux arm64 and musl builds; Windows is out of scope.
- No measurement of the whole PreToolUse hook with the real binary.
