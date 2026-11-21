# LegacyGuard: project context

A Claude Code plugin (v0.5.1) that guards Python backends while Claude edits them. It began as a hackathon
project (USM Hatchathon 2026, Legacy Tech track, team turnback-lab). It makes no model calls and needs no API
key. Everything runs through Claude Code hooks, an MCP server and slash-command skills.

## What it does

1. **SessionStart** (`scripts/session_start.py`): sets up the guard's own venv, writes `.legacyguard/config.yaml`
   (`guardlib/autosetup.py`), creates the **baseline** as a git worktree at HEAD plus any dirty files
   (`guardlib/baseline.py`), and builds the SQLite index (`indexer/`).
2. **UserPromptSubmit** (`scripts/prompt_context.py`): looks up indexed functions the prompt names and adds
   their blast radius as context. It never blocks.
3. **PreToolUse Edit|Write** (`scripts/pre_edit.py`): applies the edit in memory (`guardlib/virtual.py`) and
   runs static checks (`guardlib/checks.py`): frozen files, OpenAPI, migration and event-schema breaks, removed
   routes or params, and weakened, deleted or skipped tests. The decision comes from `guardlib/policy.py`:
   advisory allows, guarded asks (at most 3 prompts per session), strict denies.
4. **PostToolUse Edit|Write** (`scripts/post_edit.py`, async with `asyncRewake`): the **behavioral diff**.
   - `runner/diff.py::changed_targets`: tree-sitter symbol diff picks the changed functions, plus functions that
     read a changed module constant. Functions whose edit is only comments, whitespace or their own docstring are
     skipped (`same_code`; docstrings count when the file reads `__doc__` or imports ply/docopt/click).
     `guardlib/engine.py::dependents` adds callers resolved through imports (`guardlib/callgraph.py`, up to 2 hops,
     at most `MAX_DEPENDENTS`=8). Callers likely to be callable go first: nearer and resolved, covered by tests,
     plain functions before methods, fewer required parameters.
   - `runner/inputs.py::build`: inputs come from the args harvested from tests (`runner/legacyguard_harvest.py`,
     a sys.setprofile pytest plugin), then literal calls in the repo (`runner/examples.py`), then a grid of
     edge values (ties, NaN, inf), then Hypothesis built from annotations and defaults.
   - `runner/lg_worker.py` runs in the **target project's interpreter**. It monkeypatches socket, sqlite3,
     open(write), subprocess and os.system to block them, and gives each call a 5 s SIGALRM. It runs old twice
     (self-diff, Diffy-style noise filter) and new once, as 3 parallel subprocesses.
   - `compare_one`: compares the return value or the exception type and message. A "verified" status needs
     every changed statement to be run by some input (traced line gate; reason e.g. "inputs reached 1 of 2 changed
     statements; line 45 never ran"). A caller carries the changed statements of every edited function it reaches
     (`diff.caller_gate`); the worker traces those lines in that file while running the caller (`gate_lines`), and the
     same rule applies. `Result.statements_reached/total` go into the pass note and the ledger evidence.
   - A difference exits 2, which wakes Claude with a counterexample (`format_wake`). Findings live in
     `.legacyguard/session.json` (`guardlib/ledger.py`).
5. **Stop** (`scripts/stop_audit.py`, `guardlib/audit.py`): audits the whole working tree against the baseline
   (this catches shell edits) and writes `.legacyguard/reports/<session>.md` and `.html` (`guardlib/report.py`,
   `guardlib/sessionmap.py`).

MCP tools (`mcp_server/server.py`): `impact`, `contract`, `diff_report`. Skills (`skills/*/SKILL.md`):
index, status, impact, map, waive, pin, mode. All of them call `scripts/lg.py --skill <name>`.

## Statuses

`verified` (no difference on the inputs tried, and every changed statement was run by some input; for a caller,
every changed statement of the edited functions it calls), `different` (for a caller whose inputs ran none of the
changed statements, the wake text and evidence say the difference may come from elsewhere), `broken` (the module no
longer imports), `untested` (with a reason: DB, network, file write, uncallable, timeout, not every changed statement
reached, non-deterministic; for a route also: every request answered the same 4xx before the handler ran, no request
entered the handler, or the route is new in this edit), `slowdown` (only the new code timed out where both old runs finished fast, confirmed
by a re-run under the full limit; never a detection, never fails CI, waivable). Comment-, whitespace- and docstring-only edits produce no result at all.

## Sprint 1 (branch sprint1-foundations, 2026-11-13)

- **Tests:** `tests/` (pytest; 467 tests after Sprint 7.2a). Run with `.venv-dev/bin/python -m pytest -q`. To run target code on
  another interpreter: `LG_TEST_TARGET_PY=/path/to/python3.x`. CI: `.github/workflows/tests.yml`.
- **Sandbox, 3 layers:** (1) OS sandbox around the worker (`runner/os_sandbox.py`: sandbox-exec on macOS,
  bubblewrap on Linux, probed once; config `behavioral_diff.os_sandbox: auto|off`). (2) PEP 578 audit hook in
  `lg_worker.py` (deletes, renames, os.open writes, rmtree, spawn/fork/kill, raw sockets, sqlite, ctypes during
  calls). (3) The original monkeypatches.
- **State oracle:** the worker reports `state` (canonical form of mutable args and `self` after the call).
  `compare_one` compares it, and the counterexample text shows `..., then items=[...]`.
- **Determinism:** seeded `random`/numpy, a frozen clock that ticks 1 ms per reading, and repeatable uuid1/4,
  reset before every input (config `behavioral_diff.deterministic`). Global module attributes are patched, plus
  `from x import y` names in the project's own modules and `django.utils.timezone`.

## Sprint 2 (same branch, 2026-11-13)

- **Methods (B3):** `lg_worker._instance_maker` builds `self` from constructor calls found in the repo
  (`examples.constructor_calls`, tests first), then from `__init__` type hints (4 rotating variants, offset per
  field), then a no-argument constructor. Methods get at least 4 inputs, so each variant runs.
- **Runtime types (B4):** the harvest plugin records `types` per parameter. `inputs.with_runtime_types` fills
  missing hints, but only with hints the builder can build (`BUILDABLE_HINT`).
- **Affected tests (C4):** `runner/affected_tests.py` plus `engine.affected_tests`, called from `post_edit.py`
  after the diff. It runs the tests the coverage map links to the changed functions, on the baseline (cached in
  `session.json` as `old_test_results`) and on the working tree, sandboxed. A test that passed before and fails
  now becomes a `test_regression` finding and a wake. Config `affected_tests: {enabled, max_tests, timeout_s}`.
- **Warm workers (A3, partial):** `runner/lg_server.py` (target interpreter) and `runner/warm.py` (client).
  There is one fork-server per tree on a Unix socket in the temp folder. The baseline server keeps project modules
  warm; the work server keeps only third-party ones. Any failure falls back to the cold worker. Config
  `behavioral_diff.warm`. Tests default to cold; `tests/test_warm.py` opts in. The hooks themselves are still
  separate processes: the full daemon in docs/ARCHITECTURE.md is not built.

## Sprint 3 (branch sprint3, 2026-11-13)

- **Confidence (B6):** `runner/mutants.py` plants up to 8 bugs in the old function's changed lines. They run
  in OLD-A only (`diff._with_mutants`); `lg_worker._run_mutants` runs an identity check first. The result is
  `Result.mutants_killed/total` and `diff.confidence_text`, shown in the pass note and the ledger. Config
  `behavioral_diff.mutants` (0 turns it off).
- **Time limits:** after 2 timeouts a function's calls get 0.5 s; after 10 timeouts or 10 s in total, the
  rest are `skipped_slow`.
- **Call graph (B7):** `guardlib/callgraph.py` resolves callers through imports (aliases, relative imports,
  module attributes, self/cls, Class.method), 2 hops. `engine.dependents` uses it; no index is needed. Name-only
  edges are kept only for unique function names.
- **Routes (C3):** `runner/routes_diff.py` plus `engine.route_targets`. Targets of kind "route" carry HTTP
  requests (literal test-client calls first, then generated). The worker's `run_route` uses Flask
  `test_client` or Starlette `TestClient`. It needs `app:` in the config and the routes table (Flask
  introspection added). Django routes are not diffed yet.
- **Fix:** `datetime.datetime`/`date` are swapped to the frozen versions only during calls
  (`frozen_datetime_module`). The global swap broke pydantic and FastAPI imports.

## Sprint 4 (branch sprint4, 2026-11-13)

- **Stand-ins (C1):** `runner/lg_stubs.py`, used in "stub and compare" mode. sqlite3, psycopg/pymysql/MySQLdb,
  requests, httpx and urllib are answered in process (no rows, HTTP 200 {}). Outcomes carry `io` (the SQL with
  params, HTTP method/url/body, COMMIT/ROLLBACK), and `diff._comparable` compares it. Params named
  conn/connection/db/cursor/cur get fakes via `lg_codec.FakeMarker`; ORM sessions are still untested. Clients
  with their own transport (Starlette TestClient) pass through. Config `behavioral_diff.io: stub|block`.
  Nothing is recorded from real services on purpose (side effects).
- **declare_change (D1):** `guardlib/intent.py` plus the MCP tool. Declarations live in `session.json`
  "declarations"; their examples run as inputs with `expect`, checked against the new code only.
  `engine.record` marks differences "intended" while the examples hold (callers too); a failing example wakes.
- **CI (E1):** `guardlib/ci.py`, `scripts/lg_ci.py`, root `action.yml` (composite). The base worktree goes in
  the session baseline dir so engine functions work unchanged. This repo dogfoods it in tests.yml (`guard` job,
  PRs only, fail-on none).
- **Fixes:** the worker's `open()` replacement is a callable object (Python 3.10 pathlib bound it as a
  method); socket binds to loopback are allowed (urllib3 probes `::1` at import).

## Sprint 5 (on main, 2026-11-14), from validation run 1 (docs/validation/2026-11-bugsinpy.md)

Built with the researcher/implementer/reviewer agents (.claude/agents/, /sprint-loop); each task passed review.
- **Honest verified (A):** see the Statuses section. Every changed statement must be reached; callers are gated
  on the edited functions they reach (worker spec "gate", traced into "gate_lines"); no-op edits skipped.
- **Harness (C):** `tools/validate.py --fix-tests with|without|both`; "without" uses a synthetic base commit and is
  the headline number; records every input source behind a detection.
- **Planted bugs (D):** `runner/mutants.py` adds mutmut-style string operators and a MutRex subset of regex
  operators (CCR, RM, QC, M2C, CCN, CA); verbose/ignore-case aware; lookaround bodies, backreferences and their
  groups, and scoped-flag groups left alone; every mutant must compile with the call's flags.
- **Inputs (B):** examples.py resolves test arguments through variables, loops, parametrize, fixtures and module
  constants; `lg_codec.Construct` recipes for objects built in tests (decoded only in the worker); edit-derived
  inputs (source "edit", always weak): literals spliced into strings, dict values, numbers around changed
  constants, and regex samples from old vs new patterns in a child process under a 2 s budget (3 s floor).
- **Carving (E):** before the main runs, one baseline run records the arguments callers pass into the edited
  function (worker `run_capture`); added as source "carved" (not weak), counts on Result and in the note. A
  recorded `self` is dropped when its class, another class in the file, or an imported base changed
  (`_class_reshaped`; not covered: `import x; x.Base`, star imports, installed packages).
- **Methods judged on their own effect on self:** the worker snapshots self before each call and reports only
  attributes added, changed or removed by the call; `__init__`/`__post_init__`/`__setstate__`/`__new__` are
  judged on the whole instance. Counterexamples name what changed (`then self.last=1` vs `then self unchanged`).

## Sprint 6 (on main, 2026-11-15), from validation run 2 (docs/validation/2026-11-run2.md)

Same agent loop; every task passed review. Held-out set for run 3: docs/validation/holdout-set.md (never tuned on).
- **6.1** edit inputs also applied on carved calls; callers get edit inputs; guessed caller inputs inherit weak.
- **6.2** the function's own test files first; pytest fixtures and str.format resolved (cached, capped).
- **6.3** edit splices inside Construct-recipe string arguments (prefix, suffix, word positions).
- **6.4** guard inputs (`examples._edit_guards`, `inputs._guard_items`): values on both sides of constants in the
  if/elif/while conditions enclosing a changed statement; typed params filtered by hint, untyped by the default's
  type or the compared constants (`_fits_guard`); builtins/self skipped. `DATE_POOL` when the edit or examples look
  date-related (`DATE_LIKE`). All source "edit", weak, under MAX_EDIT_ITEMS. Callers do not get guards/dates yet.
- **6.5** callers unlikely to run rank last (session memory of callers whose old side cannot run); open differences
  are never cut; skipped callers are named in the note, wake, Stop report, session map and CI.
- **6.6** `slowdown` status (`diff.slowdown_candidates`, `confirm_slowdowns`, at most 2 re-runs; worker reports
  `seconds`/`limits`); shown in ledger, map ("slow" callers), report, CI comment; `validate.py` counts it as checked
  but never as a detection.

## Sprint 6.8 (on main, 2026-11-15), general runnability from validation run 3 (docs/validation/2026-11-run3.md)

Plan in docs/ROADMAP.md section 3d. Success metric: the unchecked share on both sets.
- **6.8b** bytes, bytearray and memoryview parameters get inputs (`inputs.BYTES_HINTS`, `BYTES_POOL`, hint
  rewrite of `a|b` unions, edit splicing and guard values for bytes constants). The codec tags bytearray and
  memoryview separately so values round-trip and print as Python literals. Known weak spot: `_grid_values` and
  `_fits_hint` still strip `|None` by substring, so `list[int|None]` loses element Nones there.
- **6.8a** writes during module import, `setup_django`, `self` construction and input building are redirected,
  not refused: `lg_worker.setup_phase()` turns a `SETUP` flag on, and a write to a cache-like path (a path part
  below the project or home containing "cache", or a `.pickle`/`.pkl`/`.cache` suffix) goes to
  `<run scratch>/shadow/<real path>` and is read back from there. The flag is forced off around every compared
  call, so writes during a call stay blocked; writes into the project or home outside cache-like paths stay
  blocked at import. Each run (and each warm-server child) gets a fresh scratch folder. HOME, XDG_CACHE_HOME and
  TMPDIR are not changed. The audit hook no longer mistakes the int mode of `os.mkdir`/`chmod`/`truncate` for a
  file descriptor. Gaps: `os.path.exists`/`stat`/`listdir` do not see shadow files; `_cache_like` is a broad
  name heuristic; Linux bubblewrap not verified.
- **6.8c** `self` as a last resort (`lg_worker._instance_maker` after the repo-call, type-hint and no-argument
  paths): a recorded instance from tests or carved calls (`_carved_selves`, not weak), then placeholder
  constructor arguments (`_placeholder_maker`: None, 0, 1, '', 'x', [], {}, a namespace, a no-op function, True,
  ordered by annotation, harvested types and name hints; at most 24 attempts, stops after 2 hung ones, cached per
  class per run in `_LAST_RESORT`, chosen by count only so time never decides), then an instance made without
  `__init__` (`_bypass_maker`: `object.__new__` plus literal and parameter assignments from `__init__`). Inputs on
  a placeholder or bypass `self` are weak: both-raise is dropped; a difference still wakes, with a hedge, but CI
  fails only when some difference is on a real instance (`diff.placeholder_only`; placeholder-only is a
  `::warning` and its own report section). If old and new build the placeholder differently the inputs are not
  compared. `tools/validate.py` counts placeholder-only results apart (never as detections or quiet; the case
  stays unchecked).
- **6.8d** old-vs-old disagreement names its cause (`runner/noise.py::causes`, guard side, explains only, never
  changes what is compared; labels for heuristic causes are hedged: "values that look like random tokens"). The
  untested reason reads e.g. "non-deterministic: old code disagreed with itself on 2 of 3 inputs: object addresses
  in the return value (2)"; causes are also in the ledger evidence and `tools/validate.py` records and metrics.
  Benign noise is frozen at the source, identically for old and new, only when the caller is project code
  (`lg_worker._from_project`): `time.monotonic`, `perf_counter`, `process_time`, `thread_time` (+ `_ns`; 1 ms per
  reading from 1000.0), thread ids and default thread names. Process ids are **not** faked (a fake pid 4242 broke code
  that asks the OS about its own process, e.g. `psutil.Process(os.getpid())`, found in run 4): the project gets the
  real pid and ppid, and `lg_codec.canon` writes them as `<pid>`/`<parent pid>` (`lg_codec.set_pids`, set by
  `install_determinism`; ints equal to a pid and whole-number occurrences in strings, reprs and exception messages;
  only ids >= `PID_MASK_MIN`=1000, since smaller ones are common constants and an unmasked pid only makes old disagree
  with itself). A warm-server child also masks the server's pid, which modules imported before the fork read; known
  limit: the server is the child's real parent, so under warm workers `os.getppid()` also reads `<pid>` (a getpid ->
  getppid edit is hidden whenever the new side runs warm, even if the old side fell back to cold; an unchanged
  `getppid()` with one side warm and the other cold gives a false "different"). In
  containers with pids below 1000 (e.g. Docker CI) stored pids are not masked, so those functions are untested again
  (process-ids cause). Caveat: thread ids are still faked, and they carry the same OS-handoff risk the fake pid had
  (see HANDOFF follow-ups).
  `time.sleep` and timed waits
  that run out (`Event.wait`, `Condition.wait`, `Queue.get`, `Thread.join`, `select.select`) advance the frozen
  clocks by the requested timeout, so deadline loops end. Not covered: `Lock`/`Semaphore.acquire(timeout)`,
  selectors, socket timeouts (a timed-out call after 200+ frozen readings is reported as a deadline loop on a
  frozen clock). An edit that only adds or removes a clock read stays a difference with a `clock_note` hedge.
  `id()` values and the order of id-hashed sets are classified but not normalised.
- **6.8e** unreached changed statements are diagnosed and fed back into inputs (`runner/feedback.py`, guard side).
  `diagnose` walks the function's ast from each unreached statement up to its def: if/elif/else, while, for (iterable
  not empty), except, try-else, match case, nested defs, and the early exits before it (`if c: return`, `assert`); a
  line after an unconditional return/raise, or under `if False`, is named dead. Comprehension filters are not
  diagnosed: the line gate is per statement, so the tracer cannot tell whether a filter inside one ran. The untested reason gets the chain in
  words, plus where the inputs stopped ("line 7 only runs when the `if not table` on line 2 does not return and ...; the
  inputs reached line 4 but the early `return` there always happened"). `diff.second_pass` then runs one bounded extra round (at most
  `feedback.MAX_FEEDBACK_ITEMS`=40 inputs per edited function, not callers or routes) when the result is untested for
  an unreached reason and the check's limit (3 s floor to build, 1.5 s left after building to run; the run limit is
  what the check's limit has left, never more) and the function's own 10 s budget have time left: bases are the
  first-pass inputs that got furthest along the chain (the worker reports each input's own lines, spec `input_lines`),
  and each new input changes the tested parameters to values built to meet the whole chain, then one parameter at a
  time (compared constants and neighbours, None, truthiness, isinstance samples, `in`/`not in`, len, str methods,
  startswith/endswith, regex strings from a walk of the pattern, dict keys, two-parameter relations, defaults).
  Conditions over parameters, literals and whitelisted builtins/str methods are evaluated in the guard process on
  plain decoded values only (never project code; no growing operators on large operands; values over
  `feedback.MAX_SIZE`=10,000 items are neither read nor built, and a condition needing one is skipped with a note;
  the regex walk stops past `MAX_REGEX_TEXT`; regex off the main thread only on short text; the 2 s build deadline is
  checked inside each plan too) to rank candidates. Inputs are source "feedback", weak; outcomes are appended and
  `compare_one` runs again (`diff._second_compare`), so status rules are unchanged: a changed statement counts as
  reached by the second pass only when a second-pass input that `compare_one` compared ran it (`Result.compared`);
  both-raise guesses, timeouts, set-up failures and old-vs-old disagreements do not count, and are named in the
  reason (`feedback_raised`, `feedback_dropped`). A difference only on second-pass inputs (`feedback_only`) still
  wakes and fails CI like other edit guesses, with a hedge in the wake and ledger, and is listed apart by
  `tools/validate.py`. Result fields `unreached_chains`, `feedback_tried/reached/seconds/note` (also in
  `tools/validate.py` records). Fixtures: about
  0.11 to 0.28 s added per check (one more round of the three worker processes; on a project with slow imports that
  round costs its import time again unless warm workers are on). Cannot aim at: `self` attributes, results of other
  calls, globals, files or other external data; conditions with more than 3 tested parameters are only built step by
  step, not searched.
- **6.8f** held-out set 2 (`docs/validation/holdout-set-2.md`, 9 BugsInPy bugs in ansible, fastapi, scrapy, chosen
  before any run) and validation run 4 (`docs/validation/2026-11-run4.md`). `tools/validate.py` has a third set,
  `holdout2` (`HOLDOUT2`, "Held-out set 2"; `HELD_OUT` = both held-out sets), reported apart in metrics, tables and the
  top summary lines. Run 4: held-out set 2 had 8 of 9 unchecked without fix tests and found 1 of 9 (2 of 9 with fix
  tests). The frozen-pid regression it found (thefuck-1) is fixed; see 6.8d. Open findings: home-directory writes at import that are not cache-like are still refused; one function was "verified" on a
  placeholder `self`, possibly without the input that shows the bug (not checked). Do not tune on set 2 without a set 3.

## Sprint 7.4 Part A (on main, 2026-11-20), A2 throwaway working folder

Plan: docs/research/2026-11-sprint7-4-plan.md (with the reviewer's round-2 corrections).
- `runner/scratch.py` (guard side): every process that runs project code for a check (cold worker, warm-server child,
  affected-tests pytest) gets a working folder in the system temp folder, never the real tree as a writable cwd.
  `behavioral_diff.cwd_mode: auto|clone|copy|empty|off` (default auto) and `behavioral_diff.cwd: <subdir>`. Auto: a
  copy-on-write clone file by file (`clonefile(2)` via ctypes on macOS, the FICLONE ioctl on Linux; never `cp -c`,
  whose fallback is silent) when a probe clone works and the tree has at most `MAX_CLONE_FILES`=20,000 files; else a
  copy when at most `MAX_COPY_FILES`=2,000 files and `MAX_COPY_BYTES`=50 MB; else `off` (the real tree, read-only
  under the OS sandbox) when an OS sandbox is active, else `empty`. A clone that fails on one file after a good probe
  copies that file, but the copied bytes are capped at 50 MB, then off/empty with the reason; there is no uncapped
  copy. Only the probe result is cached (`.legacyguard/cwd_strategy.json`, keyed by tree, devices, platform, mode).
- Listing rule, both trees: walk the tree; prune `.git` (folders, and a worktree's `.git` pointer file), caches,
  `node_modules`, `.eggs`, `*.egg-info`, `htmlcov`, `build`/`dist`/`_build` at the tree's top level only (`pkg/build/`
  stays), folders holding `pyvenv.cfg` or `conda-meta`, nested `.git` folders, and sub-folders on another device
  (mount points). The working tree's folder has git-ignored files; the baseline never
  had them (as before).
- Phase 1 is safety only: imports still come from the real tree (absolute `sys_path`), and the worker adds the folder
  to `SCRATCH["protect"]` (`lg_worker.use_cwd`, also in the warm child; with `cwd: <subdir>` the whole folder root
  is protected too, request `cwd_root`, so `../x.txt` is refused as it was in the tree), so writes there during a call are refused
  and setup cache writes redirected, exactly as for the tree. pytest has no in-process guard, so a test's relative
  writes land in the folder. pytest runs through a launcher (`affected_tests.LAUNCH`) that drops only `''` and `.`
  from `sys.path`, with absolute test paths and `--rootdir=<tree>`; results stay keyed by relative nodeids.
- One folder per tree per check: `scratch.for_trees` (re-entrant, prepares on first use, releases at the outermost
  exit); `post_edit` opens it once around `differ.run` and `engine.affected_tests`; the Stop audit and `ci.run` (so
  `tools/validate.py`) open one block for their whole run; `scratch.current` prepares on demand only for direct
  callers. OLD-A and OLD-B share the baseline's folder. Part A clones both trees per check (2k-file fixture: clone
  0.27 s, copy 0.42 s per tree on this Mac).
- The Markdown session report (not the session-map HTML) has an "Isolation" line (OS sandbox and working-folder labels with file and byte counts); without
  an OS sandbox it says absolute-path writes are stopped only by the in-process guards. A cwd change does nothing for
  absolute-path writes: 6.8a's redirect/refusal and the OS sandbox handle those.
- The working folder has no `.git`, so project code (or a test) that runs `git` from the cwd finds no repository.
- Limits: a write that gets past the in-process layers in OLD-A can be seen by OLD-B; in a Stop audit or a `ci.run`
  (so `tools/validate.py`) one folder per tree is shared across all files of the run, so such a write can also be
  seen by later files' checks; `lg_codec.canon` does not mask the folder path (a result holding the cwd differs
  between trees, as tree paths already did; the hook pid in the folder name is base 36, so pid masking leaves it
  alone); `_sweep` can misjudge pids across containers that share /tmp with separate pid namespaces; the warm server itself keeps the tree as its cwd, so a module storing
  `os.getcwd()` at import gives untested or a difference on a warm/cold mix, never a hidden one; `empty` mode makes
  relative reads fail (if old and new raise the same error, no difference is seen); `indexer/routes.py` and
  `coverage_map.py` still run in the tree; target code calling `os.chdir` changes the cwd for later inputs.
- Tuning-set run after 7.4 (`--fix-tests both`, 2026-11-20): 8 of 9 detected without the fix's tests, 9 of 9 with
  them; thefuck-1 quiet without, detected with; every function's status and reason identical to run 4 plus the
  thefuck-1 fix. Every case used `clone` (clonefile, 18 to 927 files) under sandbox-exec, one folder per tree per
  case and mode. Held-out sets not run.
- Follow-ups: Part B (baseline folder once per session), 7.4b bwrap overlay, phase 2 imports from the folder, the
  empty-mode relative-read rule, masking cwd paths in `lg_codec.canon` (see HANDOFF).

## Sprint 7.2a (on main, 2026-11-20), route-diff correctness

Plan: docs/research/2026-11-sprint7-2-plan.md (section 0 has the user's decisions and the reviewer's corrections).
- **(d) one route-decorator parser** (`guardlib/route_decorators.py::parse`, ast-based) for `checks._routes_of` and
  `indexer/routes.from_decorators`; both regexes are gone. `get/post/put/patch/delete` and Flask `route`/`api_route`
  (no `methods` = GET; HEAD/OPTIONS dropped; methods upper-cased; non-literal `methods` gives no rows). The static
  "route removed" check now covers `.route` handlers and narrowed `methods=[...]`; the index fallback finds them.
- **(a) decorators count as code:** `Symbol.decorators_hash` and a separate `decorators_changed` kind in `symbol_diff`
  (not in the signature hash, so no stale-caller findings). `changed_targets` accepts it; when only the decorators
  changed the gate is the first body statement (`examples.entry_gate`, docstring skipped; `Target.entry_gate`), so a pass
  needs an input that got past the decorators. The code hash adds the decorators' hash. A wrapper without
  `functools.wraps` hides the body from `_span`: untested, never a false verified.
- **(b) routes are gated:** `engine.route_targets` gives each route the handler's gate (`caller_gate([h])`) or, for a
  handler that is only a caller, that caller's callee gate; plus `Target.handler` {module, qualname}. Routes never get
  `via`. The worker's `run_route` traces on every thread (`threading.settrace` only, previous hook restored; Starlette
  runs the app on a portal thread and sync endpoints on a worker thread) with `_route_tracer`: per-request buckets
  (`request_gate`, `entered`, `statuses`); a frame records into the bucket current when it started, and threads alive
  when a request timed out are not traced again. Rules (`diff._route_facts`, `_route_verdict`): N counts changed
  statements run by **compared** requests only (stricter than functions); M > 0: the statement rule decides (verified
  only when N == M; "none of the R requests reached the changed lines of h ... (reached 0 of M ...)" plus "every request
  was answered 401 before the handler ran, in both versions" when that holds; "requests reached N of M ...; line X never
  ran (k of R requests reached any)"); M == 0 (handler only reads a changed constant): same early 4xx in both trees ->
  untested, else some compared request must have entered the handler in the new tree. Entry unknown (handler not
  resolved) -> status alone, with a hedge; a pass on responses alone says "entry unknown" in its reason. The new-route
  rule comes before differences: every compared request 404/405 from routing in the old tree (the worker's per-request
  `routed`: Flask's own request adapter, matched without pushing the context; Starlette's `router`/`route`/`endpoint`
  scope keys, where a mount whose router matched nothing and a partial match (method not in the route's) are unmatched;
  `routed` is None when the router did not run, e.g. a middleware answered first or passed a copied scope, and None
  never fires the rule) and entered in the new -> untested "the route is new in this edit" (a
  path moved in a decorator, head-tree index). A 404 from a dependency, hook or decorator the edit removed stays
  different. Differences stay different; the "may come from elsewhere" hedge applies to routes too, except for a route
  gated on its own handler's entry gate; callers and routes reached through a caller keep it, worded "never got past
  the changed decorators of f, or never called it". A route gated on `callee_old_gate` uses the old tree's requests.
  Result fields `route`, `requests_reached`, `requests_entered`, `status_counts` (old tree); `reached_text` adds "(k of
  R requests reached them)"; `tools/validate.py` records `statements` and `route`. Routes previously "verified" on
  all-401/all-422 responses now show as untested (intended). Trace cost: about 1.2 ms per request on a trivial
  FastAPI endpoint (1 ms to 2.2 ms).
- **(e) factory apps:** `lg_worker.load_app` (`module:name` or `module:factory(literal args)`, Flask CLI's rule; a bare
  function name is called with no arguments; no eval, attribute chains or starred args -> ValueError, untested with
  that text), built under `setup_phase()`, cached per process (`_APPS`, failures too). `_http_caller` and index
  introspection use it. INTROSPECT now runs with `lg_worker.prepare` (blocks, run scratch folder, tree protected),
  `setup_phase()` and the OS sandbox (`os_sandbox.wrap`, tree read-only); any failure falls back to the decorators.
  Limit: handlers defined inside `create_app` have `<locals>` qualnames that match no symbol, so they get no route
  target (and their entry cannot be traced); blueprints and module-level views work.
- Tests: `tests/test_route_decorators.py`, `tests/test_route_gates.py` (A1-A4 with both index shapes, method decorator,
  C, D1-D4, rule order, handler's own 404 in Flask and FastAPI, a dropped 404 dependency or decorator and a dropped 405 dependency stay
  different, a middleware 404 before the router is not read as routing,
  the caller hedge with an entry gate, E1-E6, bare factory, leftover threads).
- Next: **7.2b** (deleted and renamed handlers: `baseline_routes` in session and CI, lookup order, delete and rename
  targets). Follow-ups: "only compared inputs count" for functions and callers; harvesting literal test-client
  `headers=` (header names only in printed text).

## Known gaps still open

- `ARCHITECTURE.md` and `TESTING.md` from the hackathon are still missing. `docs/ARCHITECTURE.md` is the new
  design. IDs like FR-xx, D-0xx and NFR-xx point into the old docs.
- MAX_FUNCTIONS=12 targets per edit (routes and callers included). `/legacyguard:impact` and the MCP `impact` tool still use name edges.
- Hooks are still short-lived processes (no daemon yet). With Django, the working-tree server still runs `django.setup()` cold in every check.
- Not yet verified on Linux bubblewrap or on a Django project. Throwaway cwd: see Sprint 7.4 (phase 1, safety only).

## Conventions

- Python 3.10+. Hooks fail open. All state goes under `.legacyguard/`, which is git-ignored.
- `runner/lg_worker.py`, `runner/lg_codec.py` and `runner/legacyguard_harvest.py` run inside the *target*
  interpreter and must stay standard-library only.
- Messages shown to Claude are factual and replayable. They never claim proof.
- Roadmap and research: `docs/ROADMAP.md`.
