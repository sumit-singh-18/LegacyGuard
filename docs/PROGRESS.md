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
