# Handoff (2026-11-15)

Start a new session in `/Users/sumit/Documents/LegacyGuard/repo` and say: "Read docs/HANDOFF.md and continue."

## Rules the user set (keep these)
- **Authorship:** every commit and push is by the user only. No Claude co-author trailer and no PR attribution. Commit to `main` directly; no pull requests.
- **Agent workflow:** researcher, implementer and reviewer agents (`.claude/agents/`, skill `/sprint-loop`). Nothing is committed until the reviewer says PASS. Loop on corrections until it does.
- **Plan updates:** revise the plan when a better approach is backed by real sources, and record decisions in `docs/ROADMAP.md`.
- **Overfitting guard:** never tune on the held-out bugs in `docs/validation/holdout-set.md`.

## Where to read
- `CLAUDE.md`: what the plugin does, each sprint's changes, and conventions.
- `docs/ROADMAP.md`: section 3d has the Sprint 6–8 plan.
- `docs/PROGRESS.md`: the running log.
- `docs/validation/`: the validation runs.

## State at handoff
- `main` is at 780f608 and pushed. 278 tests pass on Python 3.12 (`.venv-dev`) and 3.10 (`/private/tmp/claude-501/venv310`; if it is gone, recreate it with `/opt/homebrew/bin/python3.10 -m venv ... && pip install -r requirements-dev.txt`).
- Sprints 1–5 are done. Sprint 6 tasks 6.1–6.6 are merged.
- **Task 6.7 (validation run 3) is in progress.**
  - The harness changes are **uncommitted** in the main checkout: `tools/validate.py`, `tests/test_validate.py`, the new `tools/validation/holdout.yaml`, and 11 new files in `tools/validation/patches/`. They were reported at 281 tests and were in review at handoff.
  - All 20 cases passed `--check-setup`: 10 held-out bugs, the PySnooper control, and 9 tuning bugs.
  - Run 3 was started at handoff; it takes about 10–20 minutes. Log: `/private/tmp/claude-501/lg-validation/run3.log`. Output: `/private/tmp/claude-501/lg-validation/out-run3/`. If the output is missing or incomplete, rerun:
    ```
    .venv-dev/bin/python tools/validate.py tools/validation/manifest.yaml tools/validation/holdout.yaml --fix-tests both --workdir /private/tmp/claude-501/lg-validation --out /private/tmp/claude-501/lg-validation/out-run3
    ```
  - Still to do for 6.7:
    1. Have the reviewer check the harness diff (hashes against BugsInPy, scoring, that controls are excluded, that luigi 22 is scored by outcome type, that `legacyguard/` is unchanged).
    2. Commit the harness.
    3. Have the results reviewed, then write `docs/validation/2026-11-run3.md` like run 2.
    4. Update the README's "How well it works" section with the **held-out** number as the headline.
    5. Log it in `PROGRESS.md`.
- **Next is Sprint 7.** The research is done and saved in `docs/research/2026-11-sprint7.md` but **not reviewed yet**. It proposes tasks 7.1–7.7 and moves a route-correctness fix (7.2) ahead of Django. Send it to the reviewer first, then record the agreed plan in ROADMAP. The items it covers:
  - C5 contract tools: oasdiff, squawk, `makemigrations --check`, buf breaking.
  - D3 `/legacyguard:characterize`.
  - Move the impact skill and MCP tool onto the resolved call graph.
  - C3 Django route diffing.
  - A2 copy-on-write working folder, so the worker gets a throwaway cwd.
  - Route-diff validation on 2–3 real FastAPI or Flask apps.
- **Then Sprint 8:** the A3 daemon, and C1/C2 recorded DB answers inside a rolled-back transaction.

## Known open follow-ups (small)
- Callers do not get guard or date inputs: `diff._merged_edit` merges only literals, fragments, numbers and regexes.
- Crossing an argument with None still applies to untyped required parameters. Narrow it if a validation run shows false alarms from it.
- `gh` is not logged in, so CI on GitHub was not checked after the last pushes.
- Untracked `.legacyguard/` and `.claude/worktrees/` in the checkout are runtime state; do not commit them.

## Results so far (tuning set, 9 BugsInPy bugs)
- Run 1: 2 of 7 detected without the fix's tests.
- Run 2: 4 of 9 without, 6 of 9 with, 0 false alarms.
- Sprint 6 targeted the remaining misses: tqdm 4, thefuck 7 and youtube-dl 5 are now caught in fixtures.
