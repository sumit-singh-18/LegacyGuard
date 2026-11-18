# Handoff (2026-11-18)

Start a new session in `/Users/sumit/Documents/LegacyGuard/repo` and say: "Read docs/HANDOFF.md and continue."

## Rules the user set (keep these)
- **Authorship:** every commit is by the user only. No Claude co-author trailer and no PR attribution. Commit to `main` directly; no pull requests.
- **Agent workflow:** researcher, implementer and reviewer agents (`.claude/agents/`, skill `/sprint-loop`). Nothing is committed until the reviewer says PASS. Loop on corrections until it does (at most 4 rounds, then ask the user).
  - The project agent types may not be registered in a session. Then run them as `general-purpose` agents whose prompt starts with "read .claude/agents/<role>.md and follow it" (implementer: model opus).
  - Send corrections to the **same** agent with SendMessage, and the revised report back to the **same** reviewer.
  - **Run test suites one at a time, never in parallel** (concurrent runs share temp folders and gave a spurious failure once). Tell every agent this.
- **Plan updates:** revise the plan when a better approach is backed by real sources, and record decisions in `docs/ROADMAP.md`.
- **Overfitting guard:** never tune on the held-out bugs in `docs/validation/holdout-set.md` (black, tornado, httpie, cookiecutter, luigi). No holdout project name in code, comments, tests or commit text. Fixtures use generic shapes.
- **Honesty:** quote the held-out number, not the pooled one. "Unchecked" is reported, never hidden.

## Where to read
- `CLAUDE.md`: what the plugin does, each sprint's changes (Sprint 6.8 has entries for 6.8a to 6.8d), conventions.
- `docs/ROADMAP.md` section 3d: Sprint 6.8 (added after run 3) and the revised Sprint 7 order.
- `docs/PROGRESS.md`: the running log.
- `docs/validation/2026-11-run3.md`: the write-up that motivated Sprint 6.8. Raw data in `2026-11-run3-raw.md` and `-results.json`.

## State at handoff
- `main` is at `ee5dbd7`, **9 commits ahead of `origin/main` and NOT pushed**. `git push` failed with HTTP 403. `gh` is logged in as `sumit-singh-18`, but its fine-grained token (`github_pat_...`) lacks write access to repository contents. **The user has to fix the token** (Settings, Developer settings, Fine-grained tokens, give `LegacyGuard` "Contents: Read and write", or `gh auth login` with a classic `repo`-scope token). Then `git push origin main`, and check the GitHub Actions run (`gh run list --limit 3`). The last run seen on GitHub (for 780f608) was green.
- 339 tests pass on Python 3.12 (`.venv-dev/bin/python -m pytest -q`, about 150 s) and Python 3.10 (`/private/tmp/claude-501/venv310/bin/python -m pytest -q`; if missing, recreate with `/opt/homebrew/bin/python3.10 -m venv /private/tmp/claude-501/venv310 && /private/tmp/claude-501/venv310/bin/pip install -r requirements-dev.txt`).
- The run 3 write-up and the README headline (held-out number, "1 of 10 either way") are committed and were reviewer-checked (PASS, round 2).

### Sprint 6.8, general runnability (from run 3). Success metric: the unchecked share on both sets, detections second.
| Task | State | Commit |
|---|---|---|
| 6.8a import- and setup-time cache writes redirected to a per-run scratch folder | done, reviewed | 9337db1 |
| 6.8b `bytes`/`bytearray`/`memoryview` inputs | done, reviewed | 6ee9daf |
| 6.8c stand-in `self` (placeholder args, bypass, carved), labelled weaker; placeholder-only differences warn in CI and are counted apart in `validate.py` | done, reviewed | bf4fdc3 |
| 6.8d name the cause of old-vs-old disagreement; freeze benign clock, pid and thread noise | done, reviewed | ee5dbd7 |
| **6.8e feedback from unreached changed lines into input generation** | **in progress, uncommitted** | none |
| 6.8f fresh held-out set (10 bugs from other projects, chosen BEFORE looking at results) and validation run 4 | not started | none |

**6.8e state.** The implementer was cut off by a usage limit. Uncommitted in the checkout: `legacyguard/runner/feedback.py` (new) and 44 added lines in `legacyguard/runner/lg_worker.py`. The `diff.py` integration was being written and no tests exist yet. The work is unreviewed and unfinished. Either read both files and finish the task, or discard them (`git checkout legacyguard/runner/lg_worker.py && rm legacyguard/runner/feedback.py`) and restart the task. The brief given to the implementer, reuse it:
1. Diagnose: for each unreached changed line, compute the chain of enclosing conditions (if/elif/while/for/try/match, comprehension filters, early returns or raises before the line) from the old function's AST, and put it in the untested reason in plain words ("line 59 is under `if x is None` after `if not d: return`").
2. Targeted inputs: after the first run, build a bounded second pass (about 40 extra inputs per function, one extra round, inside existing time limits) of values that satisfy the chain: compared constants and neighbours, `is None`/truthiness, isinstance, `in`/`not in`, len comparisons, str methods, regex samples (reuse Sprint 5 machinery), dict key checks, parameter comparisons, defaults. Start from the first-pass input that reached the deepest enclosing statement and change one parameter at a time (branch distance idea). Mark the inputs weak (source "edit" or a new "feedback").
3. Status rules unchanged: "verified" only when every changed statement is reached; unreachable lines stay untested with their chain named.
4. Fixtures to include: early-return guard, `if key in d and d[key]:`, isinstance plus len, string-method guard, regex match, two parameters set consistently, a dead changed line that stays untested, a no-false-alarm refactor. Report seconds added per check and what it still cannot satisfy (object state from constructors, external data, results of other calls).

**Then 6.8f.** Have the researcher choose 10 fresh BugsInPy bugs in projects not yet used (not youtube-dl, tqdm, thefuck, tornado, black, httpie, cookiecutter, luigi, pysnooper), selection criteria as in `holdout-set.md`, record them in a new `docs/validation/holdout-set-2.md` BEFORE running, reviewer-checked, add `tools/validation/holdout2.yaml` (+ patches), run `tools/validate.py ... --fix-tests both`, write `docs/validation/2026-11-run4.md`. Report the **unchecked share first**, then the detection rate, per set. Compare with run 3 (8 of 10 unchecked). Update the README headline, `PROGRESS.md`, CLAUDE.md. Do not tune on set 2 afterwards without a set 3.

**After Sprint 6.8: Sprint 7**, order agreed with the reviewer (see ROADMAP 3d): 7.4 (A2 throwaway cwd; acceptance includes "black-style import with absolute-path write is handled", note 6.8a already redirects cache-like writes) and 7.2 (route correctness: a route is not "verified" when every request returned the same early 4xx; "reached N of M" rule) first; 7.1 (impact skill and MCP tool on the resolved call graph) last; 7.5 (Django routes) and 7.7 (`/legacyguard:characterize`) wait for the runnability and determinism fixes; 7.6 (contract tools) independent, split into oasdiff and `makemigrations --check` first. Research is in `docs/research/2026-11-sprint7.md`; its web-sourced claims are unverified (bwrap `--tmp-overlay` flag and version, oasdiff JSON fields, `cp -c`): confirm them before building. Then Sprint 8 (A3 daemon, C1/C2 recorded DB answers).

## Known open follow-ups (small)
- `inputs._grid_values` and `_fits_hint` still strip `|None` by substring, so `list[int|None]` loses element Nones there (6.8b note).
- `id()` values and the order of id-hashed sets are classified, not normalised, in the old-vs-old check; one false "different" was seen on a set of id-hashed objects (pre-existing).
- 6.8d: `Lock`/`Semaphore.acquire(timeout)`, selectors and socket timeouts do not advance the frozen clocks; a timed-out call after 200+ frozen readings is reported as a deadline loop.
- 6.8a: `os.path.exists`/`stat`/`listdir` do not see redirected (shadow) files; `_cache_like` is a name heuristic; a SIGKILLed warm server leaks its scratch folder in temp.
- 6.8c: bypass instance copies only top-level `__init__` assignments; the harvested `__init__` types path is untested end to end; the warm-server path has no dedicated test.
- Callers do not get guard or date inputs: `diff._merged_edit` merges only literals, fragments, numbers and regexes.
- Not verified on Linux bubblewrap, on a Django project, or on real libraries with the new 6.8 code (fixtures only until run 4).
- Untracked `.legacyguard/` and `.claude/worktrees/` are runtime state; do not commit them.
- Several untested-reason texts were never re-run by hand; run 4 is the first real measurement of Sprint 6.8.

## Results so far
- Tuning set (9 BugsInPy bugs, run 3): 8 of 9 detected without the fix's tests, 9 of 9 with. This reflects tuning.
- **Held-out set (10 bugs, run 3, the number to publish): 1 of 2 checked detected without the fix's tests, 8 of 10 unchecked; with them 1 of 3 checked, 7 unchecked. 1 of 10 either way.** False alarms not measured (no change cases).
- Run 4 on a fresh held-out set is the next measurement.
