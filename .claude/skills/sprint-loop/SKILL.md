---
name: sprint-loop
description: Run a LegacyGuard goal as a review loop. This session (Fable) plans from sources, delegates research and implementation to Opus subagents, reviews their reports against tests and sources, and sends corrections until the acceptance criteria hold.
argument-hint: <goal>
arguments: goal
disable-model-invocation: true
---
Goal: $goal

You are the planner and final reviewer. The subagents `researcher` and `implementer` run on Opus. You do not
hand your judgment to them: you check everything they report before it counts.

1. Ground the goal
   - Read CLAUDE.md and docs/ROADMAP.md. Restate the goal and write acceptance criteria that can be checked
     (tests that must pass, behavior on named fixtures, docs to update).
   - For each open design question, start a `researcher` with one precise question. Questions that do not depend
     on each other can run at the same time.
   - Open the two or three sources the plan rests on yourself (WebFetch) and confirm they say what the report
     claims. Drop any claim you cannot confirm.

2. Plan
   - Write the plan into docs/ROADMAP.md (or a section of it): steps, files, acceptance criteria, and the
     sources each decision rests on, as links.
   - Split the work into tasks with clear file boundaries. Tasks that touch the same files run one after another.

3. Implement and review, per task
   - Start an `implementer` with a self-contained brief: the task, the files, the acceptance criteria, the
     relevant facts from research, and what not to touch.
   - When it reports, review it yourself: read the diff (`git diff`), run `.venv-dev/bin/python -m pytest -q`
     (and the Python 3.10 venv if present), and check each acceptance criterion and each claim against the code.
   - If anything falls short, send the same implementer specific corrections with SendMessage: the failing test
     or command output, file and line, what is expected. Do not start a new agent for a correction.
   - Repeat up to 4 rounds per task. If it still falls short, stop and ask the user, with what is blocking.

4. Finish
   - All tests pass on both Pythons, every acceptance criterion holds, and CLAUDE.md, the READMEs and the roadmap
     status reflect the change.
   - Commit as the user, with no Claude attribution lines. Merge into main and push, as the user asked earlier,
     then check the GitHub CI run for the new commit.
   - Report: what changed, the evidence (tests, CI), the sources the decisions rest on, and what is still
     unverified.
