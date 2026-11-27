# Overnight progress log

Running the roadmap in a loop: the researcher, implementer and reviewer agents (all Opus); nothing counts
until the reviewer passes it. Newest entries at the bottom.

## 2026-11-14

- Sprint 5 planned in docs/ROADMAP.md 3c from validation run 1 (2 of 7 real bugs caught without the fix's
  own tests).
- Phase 1 started in parallel: Task A (honest "verified": callers and every changed statement must be
  reached; skip comment-only edits; rank callable callers first), Task C (harness "without fix tests" mode
  on a synthetic base commit), research for tasks B, D and E.
- Task A implemented (120 tests on Python 3.12 and 3.10) and sent to review.
- Task C implemented in its own worktree (113 tests) and sent to review.
- Research for B, D, E done (EvoSuite/Pynguin seeding, MutRex, mutmut, test carving); sent to review.
- Task D (string and regex planted bugs) started in its own worktree.
- Task C review: REVISE (deciding-input source was biased by the display sort; fixture would break once
  Task B lands). Corrections sent back.
- Task A review: REVISE (caller kept the first edge, not the best; multi-callee callers gated on one
  callee; CLAUDE.md stale). Corrections sent back, plus: label caller differences whose inputs never
  reached the edit, and keep docstrings when the file reads `__doc__`.
- Research review: PASS (all 9 sources confirmed; three of our heuristics labelled as inference). Design
  decisions recorded in docs/ROADMAP.md 3c.
- Task A round 1 done (125 tests), Task C round 1 done (114 tests); both back with their reviewers.
- Task D done in its worktree (133 tests; regex-only edit now gets 7 of 8 planted bugs caught); sent to review.
- Task A: reviewer PASS in round 2. Committed f365ae2.
- Task B (inputs from tests and the edit) started in the main checkout.
- Task C: reviewer PASS in round 2. Committed 8525ba8.
- Task D: reviewer REVISE (the real youtube-dl js_to_json regex, triple-quoted and verbose, still got no
  planted bugs; noise from verbose comments and ignore-case), then PASS in round 2. Committed 7dcb259.
- Task B implemented (176 tests; all five real-miss fixtures detected, 15 of 16 new tests fail on the old
  code). Found while merging D: with B's regex sampling, one end-to-end test took 42 minutes on the real
  js_to_json pattern. Sent back to bound input-building time before review.
- Task B: time bounded (regex work in a child process, 2 s budget); review REVISE (spliced edit inputs
  caused a false alarm on an error-message refactor), then PASS in round 2. Committed 89b1149 (182 tests).
- Task E (carve the arguments callers pass to the edited function) started.
- Task E implemented (192 tests). On real code: youtube-dl 1 is now detected through a carved call from
  match_str (with fix tests); tqdm 4 still unreached because every caller instance comes from the only literal
  constructor in the tests (total=10). Review REVISE: a carved `self` restored from the old code skips
  `__init__`, causing a false alarm when the edit adds a field; replay counts inflated; slow capture could
  cause silent timeouts; carved inputs could exceed max_inputs. Also one Task B test flaked on 3.10 under
  load (regex-sampling time budget). All sent back.
- Task E round 2: coordinator decision, a method is judged on what the call does to `self` (attributes added,
  changed, removed); `__init__` and other builders on the whole instance. Reviewer REVISE once more (false
  alarm through a changed base class), then PASS in round 3. Fixed a src-layout gap the reviewer noted.
  Committed 2de7f4a (202 tests on Python 3.12 and 3.10). CLAUDE.md updated for Sprint 5.
- Phase 4 started: validation run 2 on the same nine BugsInPy bugs, both modes.
- Validation run 2 done and reviewed (PASS with write-up corrections): 4 of 9 real bugs caught without the
  fix's own tests (run 1: 2), 6 of 9 with them (run 1: 5), no false alarms. Published in the README. Committed
  4a94c10. Sprint 5 complete.
- Plan revised (docs/ROADMAP.md 3d): Sprint 6 (input quality from run 2, plus a held-out set of new BugsInPy
  bugs that is never used for tuning, to avoid overfitting to the nine), Sprint 7 (open roadmap items), Sprint
  8 (daemon, recorded DB answers). TypeScript deferred with reason.
- Sprint 6 phase 1 started in parallel: held-out corpus research, 6.1 (edit inputs from carved calls), 6.2
  (pick the function's own test files, fixtures and str.format), 6.5 (skip uncallable callers).
- Held-out set: reviewer REVISE (tornado 9 cannot import on Python 3.10; two bugs need direct calls), then PASS
  in round 2: 10 bugs in 5 projects plus a control case. Recorded in docs/validation/holdout-set.md (1b5cb32).
- Task 6.2 (own test files first; fixtures and format strings): review REVISE (exponential fixture cost,
  warning flood), PASS in round 2. Committed 1ae7a83.
- Task 6.1 (edit inputs on carved calls; callers get edit inputs; weak inheritance for guessed caller
  inputs): review REVISE (weak rule wider than asked lost real detections), PASS in round 2. Committed d1cd5ea.
  Wiring for 6.2 in diff.py committed 343c7c0 (230 tests on both Pythons).
- Task 6.5 (skip uncallable callers): review REVISE (memory blamed the wrong side; an open difference could
  be cut; static signal looked only in tests; skipped callers hidden on wakes); fixed plus session map and
  Stop report; PASS in round 2. Merged afb8af4 (246 tests on Python 3.12 and 3.10).
- Sprint 6 phase 2 started: 6.3 + 6.4 (object splices; guard-condition inputs and a date pool) and 6.6
  (guarded slowdown result).
- Tasks 6.3 + 6.4 implemented (the three fixtures thefuck 7, tqdm 4, youtube-dl 5 detected without fix tests);
  sent to review. Task 6.6 still being built.

## 2026-11-15

- Tasks 6.3 + 6.4: review REVISE (guard values ignored an untyped parameter's default and caused a false alarm on
  an f-string refactor; DATE_LIKE matched update/validate/time; builtins filled the guard cap), PASS in round 2.
  Committed f766cb8 (260 tests).
- Task 6.6 (slowdown result): review REVISE (harness dropped slowdown cases from the denominators; Stop re-ran
  them; map, report, CI comment and backtest did not know the status; waive refused it), PASS in round 2. Merged
  de6f5a2 (278 tests on Python 3.12 and 3.10).
- Next: Task 6.7, validation run 3 on the tuning set and the held-out set.
- Task 6.7 (validation run 3): harness reviewed PASS and committed (2236b5d). Run 3 done: held-out 1 of 2 checked
  detected without fix tests (8 of 10 unchecked), tuning 8 of 9 / 9 of 9, no false alarms. The guard does not
  generalize yet: five general failure classes (import file write, `self` not buildable, non-deterministic old
  code, inputs not reaching the edit, no `bytes` inputs). Report docs/validation/2026-11-run3.md (draft, review
  pending); README headline now the held-out number. Sprint 7 research sent to review.
- Task 6.8b (bytes, bytearray, memoryview inputs): review REVISE (nested `|None` hints lost inner None), PASS in round 2. 298 tests on Python 3.12 and 3.10.
- Task 6.8a (redirect import- and setup-time cache writes): review PASS in round 1 (symlink, .., rename, hardlink and flag bypasses probed, none found). 309 tests on Python 3.12 and 3.10.
- Task 6.8c (stand-in `self`): review REVISE (search not cached and time-dependent; bypass policy; harness counted placeholder results as detections; CI policy), PASS in round 2. 325 tests on Python 3.12 and 3.10.
- Task 6.8d (name the cause of old-vs-old disagreement; freeze benign noise): review REVISE (frozen monotonic deadline loops regressed, cause labels too confident, added clock read false-alarm class), PASS in round 2. 339 tests on Python 3.12 and 3.10.
- Task 6.8e (diagnose unreached changed lines, second pass of inputs aimed at their conditions): built, review REVISE in round 1 (unbounded value building, run limit, uncompared lines counted), corrections made, review PASS in round 2. 381 tests on Python 3.12 and 3.10.
- Task 6.8f (held-out set 2 and validation run 4): set chosen and reviewer-checked before any run (`holdout-set-2.md`;
  9 bugs in ansible, fastapi, scrapy). At setup, ansible-4 was replaced by ansible-5 under the replacement rule (it
  cannot import in the sandbox because `~/.ansible/tmp` is created at import), and lxml/zope.interface pins were raised
  to the lowest arm64 cp310 wheels. `tools/validate.py` gained a `holdout2` set, and its summary names both held-out
  sets. Run 4 (all three sets, both modes, 203 s): held-out set 2 had 8 of 9 unchecked without fix tests (7 of 9 with)
  and found 1 of 9 (2 of 9). Held-out set 1 had 7 of 10 unchecked (run 3: 8) and found 2 of 10 (black-17 is new).
  Tuning found 8 of 9; thefuck-1 regressed to unchecked because of 6.8d's frozen pid. Write-up:
  docs/validation/2026-11-run4.md; README headline now uses held-out set 2. 382 tests on Python 3.12 and 3.10.
- Fix for the 6.8d frozen-pid regression found in run 4 (thefuck-1): project code now gets its real pid and ppid,
  so OS lookups work (C extensions too), and `lg_codec.canon` writes pids as `<pid>`/`<parent pid>` so the three
  runs still agree; warm-server children also mask the server's pid. thefuck-1 is back to run 3's outcome (quiet
  without fix tests, detected with). 386 tests on Python 3.12 and 3.10. Reviewer PASS.
- Task 7.4 Part A (A2 throwaway working folder): every process that runs project code for a check (cold worker,
  warm-server child, affected-tests pytest) now runs in a throwaway folder in the temp folder (`runner/scratch.py`):
  a copy-on-write clone (clonefile/FICLONE, at most 20,000 files), else a copy (at most 2,000 files and 50 MB), else
  the real tree read-only when an OS sandbox is active (`off`), else an empty folder. Phase 1 is safety only: imports
  still come from the real tree, and the folder is protected like the tree, so in-process statuses do not change.
  One folder per tree per check, prepared on first use and shared by the diff and the affected tests (post_edit opens
  `scratch.for_trees` once); the Stop audit and `ci.run` (and so `tools/validate.py`) open one block for their whole
  run. The per-file copy fallback after a failed clone is capped at 50 MB. The report shows an "Isolation" line.
  Part B (baseline cloned once per session) split out as the first 7.4 follow-up. Built after the reviewer's round-2
  corrections to the plan. 420 tests on Python 3.12 and 3.10 (the 3.10 venv had lost its `pyvenv.cfg` to temp
  cleanup and was recreated; the old one is kept as `/private/tmp/claude-501/venv310.broken-20261120`).
  Timings on this Mac (APFS), test fixture of 2,000 files (1.3 MB) in 40 x 7 folders, three runs:
  clone (clonefile) 0.27 s each run; copy 0.42 to 0.43 s. Part A clones both trees per check, so up to about 0.55 s
  per check at 2k files (about 5 s at the 20k cap, per the plan's measurement).
- Task 7.4 review round 1: REVISE. Fixed: with `cwd: <subdir>` the whole throwaway folder is protected (request
  `cwd_root`, cold and warm; `../x.txt` during a call is refused as before). Also: `sandboxed()` fails open to "not
  sandboxed"; the hook pid in the folder name is base 36 (pid masking leaves it alone); `build`/`dist`/`_build`
  pruned only at the top level; a worktree's `.git` pointer file is left out of the folder (found in the tuning run:
  the baseline clone had one file more than the working tree's). 423 tests on Python 3.12 and 3.10.
  Tuning-set run (tools/validate.py, tuning manifest only, `--fix-tests both`, workdir
  /private/tmp/claude-501/lg-validation, out `out-run74`): 8 of 9 detected without the fix's tests, 9 of 9 with;
  thefuck-1 quiet without, detected with; 0 changes in any verdict or any function's status or reason against run 4
  plus the thefuck-1 fix (52 functions, untested share 71.2% in both modes). Every case used `clone` (clonefile) under
  sandbox-exec, 18 to 927 files per tree, 0.01 to 0.13 s, one folder per tree per case and mode (36 for 18 runs).
  The cached thefuck venv had been gutted by the midnight temp cleanup (setup failed); it was moved aside to
  `envs/thefuck-1848729c.broken-20261120` and rebuilt by the harness. Held-out sets not run.
- Sprint 7.2a (route-diff correctness; plan `docs/research/2026-11-sprint7-2-plan.md`, section 0 has the user's
  decisions and the reviewer's corrections), built in the order (d), (a), (b), (e). (d) one ast-based route-decorator
  parser (`guardlib/route_decorators.py`) for the static route check and the index fallback: Flask `.route`/`api_route`
  (no methods = GET), narrowed `methods=[...]` reports "route removed". (a) decorator-only edits are a
  `decorators_changed` change (not a signature change), gated on the first body statement (`examples.entry_gate`).
  (b) route targets carry the handler's gate (or its callee gate) and the handler; `run_route` traces each request on
  every thread (`threading.settrace` only) into its own bucket; N of M counts statements run by compared requests;
  with M > 0 the statement rule decides, with M == 0 the same-early-4xx rule then the entered rule; the new-route
  rule (404/405 from routing in the old tree, entered in the new) is untested, never a finding; no "may come from
  elsewhere" hedge for a route gated on its own handler's entry gate. (e) `lg_worker.load_app` for `module:factory(literal args)` and bare
  factory names, cached per process; index introspection uses it with the worker's set-up under the OS sandbox.
  460 tests on Python 3.12 and 3.10 before review. Cross-thread trace cost: about 1.2 ms per request on a trivial FastAPI
  endpoint. Tuning-set run after 7.2a (`--fix-tests both`, tuning manifest only, out `out-run72a` in /private/tmp/claude-501/lg-validation): 8 of 9 detected without the fix's tests, 9 of 9 with; 0 changes in any verdict or any function's status or reason against `out-run74` (52 functions, untested share 71.2% in both modes). No tuning case has a route (no `app:`), so this shows only that functions are unchanged; the route rules are covered by the fixtures. Held-out sets not run.
- Task 7.2a review round 1: REVISE. Fixed: (1) the new-route rule fired on status and entry alone, so dropping a
  `Depends(get_thing_or_404)` or a 404-aborting decorator was erased as "new route"; the worker now reports per request
  whether a route of the app matched the method and path (`routed`: Flask URL map match; Starlette: the endpoint the
  router writes into the scope, a mount whose router matched nothing and a 405 partial match count as unmatched), and
  the rule needs the old answers to come from routing. A worker flag was chosen over the static decorator check because
  it also sees router prefixes, mounts and `add_url_rule` routes, and it was checked on FastAPI, an included router,
  a mounted sub-app and a plain Starlette mount. (2) The hedge exemption is only for a route gated on its own handler's
  entry gate; callers and caller routes with an entry gate keep the note, reworded. Optional: a pass on responses alone
  with entry unknown says "entry unknown" (also in the ledger evidence). 464 tests on Python 3.12 and 3.10.
  Tuning set re-run after the fixes (out `out-run72a-r1`): 8 of 9 / 9 of 9, 0 changes against `out-run74` in any verdict or function status or reason. Re-run because `compare_one` (shared with functions) changed.
- Task 7.2a review round 2: REVISE, fixed. The Starlette `routed` probe read a missing endpoint as "unmatched" and
  rewrote any 405 to unmatched. It now reads what Starlette's router records: `routed` is None when the router did not
  run (no `router` in the scope: a middleware answered first, or passed a copy); False only when the router matched
  nothing (no endpoint, or a mount whose router matched nothing) or the matched route's methods lack the request's
  (partial); True otherwise (an endpoint without a route: None on 405, else True). The 405 status rewrite is gone.
  Flask matches with its own request adapter (`test_request_context(...).match_request()`, not pushed, so no session
  or teardown hook runs; SERVER_NAME and subdomains covered). Tests: a dropped 405 dependency on a matched route stays
  different; a middleware 404 before the router does not fire the new-route rule; a scope-reading unit test. 467 tests
  on Python 3.12 and 3.10.
- Fix after the 7.2a CI failure (1c78663 failed on ubuntu 3.12/3.13): (1) `test_regex_only_edit_detected_by_from_regex_sample`
  got no samples. Cause: on Python 3.12+ a thread that ends with a trace function still set leaves the interpreter's
  tracing instrumentation on for the whole process (sys.gettrace() reads None), so all later Python runs about twice as
  slow. `run_route` and two in-process tests in test_route_gates.py set `threading.settrace`, and their threads ended
  traced; the regex draw's cold Hypothesis utf-8 codec table (a loop over every code point) went from 0.75 s to 1.35 s
  locally and past the 2 s draw cap on CI. 3.10 is unaffected. Fix: `lg_worker._thread_trace` wraps the trace function
  and calls `sys.settrace(None)` when the thread's outermost run() frame returns; run_route and the tests use it. Test:
  a thread (and one with its own failing run()) ends with no trace function set. This also removes the same slowdown
  from a route worker after its first request thread. (2) `test_real_non_determinism_names_its_cause` flaked: a random
  `os.urandom(8).hex()` that happens to be all digits (about 1 in 900 per pair) did not read as a random token, so the
  cause was "unclassified". Pre-existing (6.8d). `noise._token_pair` now also accepts two hex strings of which one mixes
  letters and digits; 0 misses in 200,000 random pairs (was 0.115%). Labels only; no status changes, so the tuning set
  was not re-run. 468 tests on Python 3.12 and 3.10.
- Task 7.2b (2026-11-21): deleted and renamed route handlers. `baseline_routes` table (SessionStart copies `routes`
  after refresh; `ci.run` introspects the base tree with `introspect(root=)`, old rows deleted and committed first).
  Old-handler lookup: baseline_routes -> routes -> the old function's decorators (source "decorator", prefix caveat in
  the reason). `engine.removed_route_targets`: one target per old METHOD path with requests from the old params; a
  rename (or a delete plus an add serving the same METHOD path) is gated on the new function's entry, a delete on the
  old body as one range. A decorator-only path change keeps 7.2a's A4 behaviour. Review round 1: REVISE, fixed: a
  deleted handler that answered 404 itself for unknown ids came out verified, because its 404 matched the router's own;
  a deleted route now passes only when the edited code still routes the path (`routed` True or a handler entered in
  the new tree), else untested with the reason. Also: "the old handler ran (k of R requests)" wording, stale-row-safe
  `ci._baseline_routes`. Round 2: PASS. Optional note left: a new catch-all route answering 404 could still let a
  deleted 404-answering handler pass. 490 tests on Python 3.12 and 3.10. Tuning set not re-run (route-only change; no
  tuning case has a route).
- Task 7.4 Part B (2026-11-21): the session baseline's working folder is built once per session and reused across
  checks (`scratch.session_baseline`, `for_trees(..., session_cache=True)` from the post-edit hook and the Stop audit);
  only the working tree is cloned per check. Manifest `.legacyguard/cwd_cache.json`, folder `lg-cwd-base-*`, flock
  `.legacyguard/cwd_cache.lock`, replaced folders retired and deleted by `drop_session_cache` at SessionStart. `ci.run`
  and `tools/validate.py` keep Part A. Deviation from the plan: `_sweep` skips `lg-cwd-base-*` explicitly ("base"
  parses as a base-36 pid). Measured (2,000-file fixture, 1.3 MB, this Mac, best of 3, two runs): baseline folder per
  check, Part A: clone 0.254 s, copy 0.41 s; Part B reuse: 0.013 s (clone), 0.012 to 0.014 s (copy), almost all of it
  the fingerprint (one os.walk + lstat: 0.012 to 0.013 s at 2k files; 0.117 s at 20k files). So a check's folder cost
  goes from about 0.51 s to 0.27 s with clone and from about 0.83 s to 0.43 s with copy, after the first check of a
  session. 517 tests on Python 3.12 and 3.10 (27 new in tests/test_baseline_cache.py).
- Task 7.4 Part B review round 1: REVISE. Fixed: (1) the session cache reuses or deletes only an `lg-cwd-base-*`
  folder whose parent is the temp folder (realpath), that is not a symlink and is outside the project
  (`scratch._is_cache_folder`, used by `_reuse` and `drop_session_cache`); a manifest entry with the right prefix
  elsewhere survives (test). (2) The limit that two overlapping post-edit hooks share the cached folder (a baseline
  affected-tests relative write by one can be seen by the other) is in CLAUDE.md and the scratch.py docstring. Label
  now "built for this session, kept for later checks". Optional SessionStart window: the drop now runs just before
  `baseline.create`, not after; holding the cache lock across the rebuild was not done because SessionStart would take
  it before the ledger lock, the reverse of the Stop audit's order (deadlock risk). 518 tests on Python 3.12 and 3.10.
- Task 7.4 Part B review round 2: REVISE. Fixed: dropping the cache only before `baseline.create` let an old-session
  hook build a folder after `git worktree add` but before the dirty files were copied, under a key the new session
  accepts (same commit, final generation, old ledger's baseline_ok), so the session reused a baseline folder without
  its dirty files. SessionStart now drops before the rebuild and again after the ledger reset; `session_baseline` reads
  the key again under the lock before building. Test (fails without the second drop: the folder is reused): a folder
  built from inside the dirty-file copy is deleted and the first check rebuilds with the dirty file. 519 tests on
  Python 3.12 and 3.10.
- Task 7.1 (2026-11-22): impact on the resolved call graph. The MCP `impact` tool, `/legacyguard:impact` and the
  session map find callers through imports (`impact.compute(mode="resolved")` over `callgraph.callers`); pre_edit and
  prompt_context stay on name matching. `Edge.callee_file`, truncation notes (candidate cap, limit), tree nested by
  callee file, fallback to names with a note if callgraph raises. 536 tests on Python 3.12 and 3.10 (17 new in
  tests/test_impact_resolved.py).
- Task 7.1 review round 1: REVISE. Fixed: (1) the Stop-time session map went from 0.14 s to 7.93 s on this repo
  (8 changed functions) with no bound, under the ledger lock; now one parse cache and a 3 s budget per build
  (`RESOLVE_BUDGET_S`), functions past the budget fall back to names with a counted note; measured 2.7 to 2.8 s.
  The MCP tool and the skill get a 20 s budget. (2) A bare name matching several definitions listed all their
  callers under one header; `render` now adds "also matched" and tags callers of other matches `-> file::qualname`.
  Optional items done: routes join on every matched definition; tree test checks adjacency and indent; a fallback
  test raises partway. 543 tests on Python 3.12 and 3.10.
- Task 7.1 review round 2: PASS. Tuning set not re-run (impact views only; no effect on the behavioural diff).
- Task 7.6 Part A (2026-11-22): contract tools. Plan docs/research/2026-11-sprint7-6-plan.md went through three
  review rounds before building (round 1: drift hidden behind a baseline exit-3 field or conflict, Stop sweep order and
  finding identity, oasdiff following URL refs from sibling files outside a sandbox, fail-open gaps, undefined skip
  rule, incomplete cache key; round 2: the skip counter was reset at every Stop). User decisions D1 (a), D2 (b), D3 yes,
  D4 no, D5/D6/D8 as recommended. Built: `contract_tools`, oasdiff in `check_http_file` (PreToolUse 0.8 s, Stop, CI)
  with parser fallback, Django drift via `runner/lg_migrations.py` (four patches, new-only items, PostToolUse note,
  Stop and CI findings). Fixed an existing crash on Django migration edits with no `contracts.db`. Measured: pre_edit
  with a fast fake 0.03 to 0.13 s, hanging fake 0.82 s; drift 0.19 s per tree on the fixture. 589 passed, 2 skipped
  (real oasdiff absent) on Python 3.12 / Django 6.1.3 and 3.10 / Django 5.2.19.
- Task 7.6 Part A review round 1: REVISE, docs only (criterion 8). Optional items taken: a hook-side test for the
  migration fix; Stop and CI refresh no longer re-run `oasdiff --version` unless the binary or config changed. 591
  passed, 2 skipped on 3.12 and 3.10.
- Task 7.6 Part B (2026-11-26): code-first OpenAPI. Plan docs/research/2026-11-sprint7-6b-plan.md went through three
  review rounds (round 1: the noise hedge turned real breaks into notes on apps with import-time defaults, the trigger
  missed models outside the app package and in CI, the wake sat after the no-targets return, worst-case time, half-built
  baseline cached, coarse dedupe key, silent parser drop; round 2: a baseline that never builds re-ran on every edit,
  hedged findings had no lifecycle). User decisions D1 FastAPI only, M1 block once, D10 restart on key change, rest as
  recommended. Built `runner/lg_openapi.py`, `guardlib/app_openapi.py`, wiring in post_edit, audit, ci, session_start.
  Measured: spec build 0.22 to 0.23 s per tree; PostToolUse check 0.38 s first, 0.26 s cached. 630 passed, 2 skipped on
  Python 3.12 and 3.10 (the 3.10 venv was recreated: corrupt, no pyvenv.cfg).
- Task 7.6 Part B review round 1: REVISE. Fixed: (1) test 18's lock probe raced with itself across the three parallel
  builds (1 in 25 runs failed); it now probes with a shared lock; (2) a finding's evidence kept its first hedge text when
  its status changed; evidence is now rebuilt on every transition and test 11c checks it. Round 2: PASS. 630 passed, 2
  skipped on Python 3.12 and 3.10; test 18 passed 40 of 40 solo runs. Tuning set not re-run (no tuning case has `app:`).
- CI after 7.6 Part B (fe0d0c5): 3 Ubuntu jobs failed (one regex-draw test out of time; suite 217 s -> 400 s) and
  macOS 3.13 once (a one-off; green on re-run). Not reproducible on this Mac or in an Ubuntu 24.04 container (bwrap
  0.9.0). A temporary CI branch with `--durations` and `ps` samples showed three orphaned spec builds (PPID 1, ~90% CPU)
  from test 17 under bwrap 0.11.1: `subprocess.run`'s timeout killed bwrap but not the Python inside. Fix:
  `os_sandbox.run` kills the process group, used by every runner of project code. On the diagnostic branch: 633 passed,
  2 skipped in 270 s, no orphans. Reviewer: code correct; docs and limits (own session per child, `os.setsid` escape on
  Linux) recorded. 633 passed, 2 skipped on Python 3.12 and 3.10.

- Task 7.6 Part C (2026-11-26): squawk for `.sql` migrations under `contracts.db`. Plan
  docs/research/2026-11-sprint7-6c-plan.md went through three review rounds (round 1: lone-CR line breaks, spans and
  windows on different texts, a 120-char key that could hide a new break, no time bound at Stop under the ledger lock,
  D9's reason contradicted squawk's tags, absolute temp paths globbed, decoy configs in the real temp folder, undecided
  lifecycle when squawk is off, a double finding with the SQLite diff; round 2: fake `--version` calls counted, a missing
  unchecked kind, stale summary, `_run` text mode). User decisions: D8 one real-binary check, D7 squawk-cli 2.74.0 in
  dev requirements and CI, D3 squawk's own break set, the rest as recommended. Built in `contract_tools`, `checks`,
  `pre_edit`, `audit`, `ci`; regex items unchanged, squawk adds breaks. Real binary: JSON shape and positions confirmed,
  median 0.0046 s per run. 704 passed, 2 skipped on Python 3.12 and 3.10.
- Task 7.6 Part C build review round 1: REVISE (test 7 asserted no `GITHUB_*` names reach squawk, which fails on
  every Actions runner). Fixed, plus a BOM escape, the pg-version note only when squawk runs, a warm-up in the timing
  test. Round 2: PASS. 705 passed, 2 skipped on Python 3.12 and 3.10.
- Task 7.6 Part D (2026-11-26): `buf breaking` for `.proto` contracts. Plan docs/research/2026-11-sprint7-6d-plan.md
  went through three review rounds (round 1: a misread v1.53.0 changelog behind the minimum version, folder `.` and
  scope broke the prefix rules, an undefined unset-key note and lifecycle cases, invented `line=1`, folders named
  `buf.yaml`, a miscounted rule set, version parsing; round 2: `_proto` order would sweep on an invalid folder, stale
  test numbers, the 1.52.x claim, AC7). User decisions: D8 one checksum-verified download of buf 1.75.0, D7 a pinned
  buf in CI, D1 Stop and CI only, the rest as recommended. Built `guardlib/proto_contract.py` and wiring in
  contract_tools, audit, ci, autosetup, report. The real binary refused `--config` with folder inputs (dropped; our
  `buf.yaml` inside each side isolates the run) and printed missing imports as COMPILE lines. Timing 0.089 s (3 files),
  0.259 s (200 files). Build review round 1: PASS; the coordinator made the buf-named folder check case-insensitive
  (one more test). 781 passed, 2 skipped on Python 3.12 (780 on 3.10 before the one-line fix).
