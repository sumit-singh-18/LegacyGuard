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
  Stop report; in round 2 review.
- Sprint 6 phase 2 started: 6.3 + 6.4 (object splices; guard-condition inputs and a date pool) and 6.6
  (guarded slowdown result).
