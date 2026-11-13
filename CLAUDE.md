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
     read a changed module constant. `guardlib/engine.py::dependents` adds callers found by name (1 hop, at most 6).
   - `runner/inputs.py::build`: inputs come from the args harvested from tests (`runner/legacyguard_harvest.py`,
     a sys.setprofile pytest plugin), then literal calls in the repo (`runner/examples.py`), then a grid of
     edge values (ties, NaN, inf), then Hypothesis built from annotations and defaults.
   - `runner/lg_worker.py` runs in the **target project's interpreter**. It monkeypatches socket, sqlite3,
     open(write), subprocess and os.system to block them, and gives each call a 5 s SIGALRM. It runs old twice
     (self-diff, Diffy-style noise filter) and new once, as 3 parallel subprocesses.
   - `compare_one`: compares the return value or the exception type and message. A "verified" status needs at
     least one input to reach the changed lines (traced line gate).
   - A difference exits 2, which wakes Claude with a counterexample (`format_wake`). Findings live in
     `.legacyguard/session.json` (`guardlib/ledger.py`).
5. **Stop** (`scripts/stop_audit.py`, `guardlib/audit.py`): audits the whole working tree against the baseline
   (this catches shell edits) and writes `.legacyguard/reports/<session>.md` and `.html` (`guardlib/report.py`,
   `guardlib/sessionmap.py`).

MCP tools (`mcp_server/server.py`): `impact`, `contract`, `diff_report`. Skills (`skills/*/SKILL.md`):
index, status, impact, map, waive, pin, mode. All of them call `scripts/lg.py --skill <name>`.

## Statuses

`verified` (no difference on the inputs tried, and the changed lines were reached), `different`, `broken` (the
module no longer imports), `untested` (with a reason: DB, network, file write, uncallable, timeout, nothing
reached the changed lines, non-deterministic).

## Sprint 1 (branch sprint1-foundations, 2026-11-13)

- **Tests:** `tests/` (pytest; 106 tests after Sprint 4). Run with `.venv-dev/bin/python -m pytest -q`. To run target code on
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

## Known gaps still open

- `ARCHITECTURE.md` and `TESTING.md` from the hackathon are still missing. `docs/ARCHITECTURE.md` is the new
  design. IDs like FR-xx, D-0xx and NFR-xx point into the old docs.
- MAX_FUNCTIONS=12 targets per edit (routes and callers included). `/legacyguard:impact` and the MCP `impact` tool still use name edges.
- Hooks are still short-lived processes (no daemon yet). With Django, the working-tree server still runs `django.setup()` cold in every check.
- Not yet verified on Linux bubblewrap or on a Django project. The worker has no throwaway cwd yet.

## Conventions

- Python 3.10+. Hooks fail open. All state goes under `.legacyguard/`, which is git-ignored.
- `runner/lg_worker.py`, `runner/lg_codec.py` and `runner/legacyguard_harvest.py` run inside the *target*
  interpreter and must stay standard-library only.
- Messages shown to Claude are factual and replayable. They never claim proof.
- Roadmap and research: `docs/ROADMAP.md`.
