---
name: sprint-loop
description: Run a LegacyGuard goal as a review loop with subagents. The researcher finds sources, the implementer builds with tests, the reviewer checks both against the brief, tests and sources; corrections go back to the same agent until the reviewer passes the work.
argument-hint: <goal>
arguments: goal
disable-model-invocation: true
---
Goal: $goal

This session coordinates. Three subagents do the work: `researcher` (sources), `implementer` (code and tests),
`reviewer` (the quality gate). Nothing counts as done until the reviewer returns PASS.

1. Ground the goal
   - Read CLAUDE.md and docs/ROADMAP.md. Restate the goal and write acceptance criteria that can be checked.
   - For each open design question, start a `researcher` with one precise question (independent questions can
     run at the same time).
   - Send each research report to the `reviewer` with the question. On REVISE, forward the corrections to the
     same researcher with SendMessage, then review again.

2. Plan
   - Write the plan into docs/ROADMAP.md: steps, files, acceptance criteria, and the reviewed sources as links.
   - Split the work into tasks with clear file boundaries. Tasks on the same files run one after another.

3. Build, per task
   - Start an `implementer` with a self-contained brief: task, files, acceptance criteria, relevant research
     facts, and what not to touch.
   - Send the brief and the implementer's report to the `reviewer`.
   - On REVISE, forward the reviewer's corrections, unchanged and in full, to the same implementer with
     SendMessage. When it reports back, send the new report to the same reviewer with SendMessage.
   - Repeat up to 4 rounds per task. If the reviewer still says REVISE, stop and ask the user, with the open
     corrections.

4. Finish
   - Every task has a reviewer PASS. Docs (CLAUDE.md, READMEs, roadmap status) reflect the change.
   - Commit as the user, with no Claude attribution lines. Merge into main and push, as the user asked, then
     check the GitHub CI run for the new commit.
   - Report: what changed, the evidence (reviewer verdicts, tests, CI), the sources, and what is unverified.
