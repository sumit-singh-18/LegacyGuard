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
- `CLAUDE.md`: what the plugin does, each sprint's changes (Sprint 6.8 has entries for 6.8a to 6.8f), conventions.
- `docs/ROADMAP.md` section 3d: Sprint 6.8 (added after run 3) and the revised Sprint 7 order.
- `docs/PROGRESS.md`: the running log.
- `docs/validation/2026-11-run3.md`: the write-up that motivated Sprint 6.8. Raw data in `2026-11-run3-raw.md` and `-results.json`.
- `docs/validation/2026-11-run4.md`: run 4, the first measurement of Sprint 6.8, on held-out set 2 (`holdout-set-2.md`). Raw data in `2026-11-run4-raw.md` and `-results.json`.

## State at handoff
- `main` is pushed to `origin/main` and includes Task 6.8e. Pushing works again; check CI with `gh run list --limit 3`.
- 386 tests pass on Python 3.12 (`.venv-dev/bin/python -m pytest -q`, about 160 s) and Python 3.10 (`/private/tmp/claude-501/venv310/bin/python -m pytest -q`; if missing, recreate with `/opt/homebrew/bin/python3.10 -m venv /private/tmp/claude-501/venv310 && /private/tmp/claude-501/venv310/bin/pip install -r requirements-dev.txt`).
- The run 3 write-up and the README headline (held-out number, "1 of 10 either way") are committed and were reviewer-checked (PASS, round 2).
- Run 4 (6.8f) is done and committed (8a7b947): `holdout2.yaml` and patches, the `holdout2` set in `tools/validate.py`, the run 4 raw results and write-up, and the README headline now on held-out set 2 ("8 of 9 not checked; 1 of 9 without the fix's tests, 2 of 9 with").

### Sprint 6.8, general runnability (from run 3). Success metric: the unchecked share on both sets, detections second.
| Task | State | Commit |
|---|---|---|
| 6.8a import- and setup-time cache writes redirected to a per-run scratch folder | done, reviewed | 9337db1 |
| 6.8b `bytes`/`bytearray`/`memoryview` inputs | done, reviewed | 6ee9daf |
| 6.8c stand-in `self` (placeholder args, bypass, carved), labelled weaker; placeholder-only differences warn in CI and are counted apart in `validate.py` | done, reviewed | bf4fdc3 |
| 6.8d name the cause of old-vs-old disagreement; freeze benign clock, pid and thread noise | done, reviewed | ee5dbd7 |
| 6.8e feedback from unreached changed lines into input generation | done, reviewed (PASS round 2) | this commit |
| 6.8f fresh held-out set (9 bugs in ansible, fastapi, scrapy, chosen BEFORE any run) and validation run 4 | done | committed (8a7b947) |

**6.8e done.** `legacyguard/runner/feedback.py` diagnoses the chain of conditions around each unreached changed statement and builds a bounded second pass (at most 40 weak `feedback` inputs per function, one round, inside the check's remaining time). Only lines of second-pass inputs that `compare_one` actually compared count as reached. Built values are capped (MAX_SIZE, MAX_REGEX_TEXT). Adds about 0.11 to 0.28 s per check on fixtures. Not covered: comprehension filters (the gate is per statement), `self` state, results of other calls, globals and external data, and conditions over more than 3 parameters. Optional follow-ups from the review: fold the repeated "only runs when …" wording and cap the reason length; add tests for a warm-server second pass and a placeholder-`self` second pass; non-regex evaluation off the main thread has no timer (it relies on size and operator checks). 381 tests pass on 3.12 and 3.10.

**6.8f done (committed in 8a7b947).** Held-out set 2 (`docs/validation/holdout-set-2.md`) was chosen and reviewer-checked before any run. At setup, ansible-4 was replaced by ansible-5 under the replacement rule (`~/.ansible/tmp` is created at import and refused by the sandbox), and the lxml and zope.interface pins were raised to the lowest arm64 cp310 wheels. `tools/validation/holdout2.yaml` and patches were added, and `tools/validate.py` gained the `holdout2` set (its summary names both held-out sets). Run 4 covered all three sets in both modes in 203 s. Write-up: `docs/validation/2026-11-run4.md`. Do not tune on set 2 without a set 3.

**Frozen-pid regression from run 4: fixed and reviewer-passed.** The 6.8d fake pid (4242) made `psutil.Process(os.getpid())` raise NoSuchProcess while inputs were set up, so thefuck-1 went from quiet/detected to unchecked. Project code now gets its real pid and ppid. `lg_codec.canon` writes them as `<pid>`/`<parent pid>` (only ids >= 1000), so old-vs-old still agrees, and warm-server children also mask the server's pid. thefuck-1 is back to run 3's outcome. No other validation case was re-run.

**After Sprint 6.8: Sprint 7.** Run 4's unchecked reason groups should inform the order: on held-out set 2, conditions on `self`, object attributes or argument contents never met (3 cases); signatures nothing could be built for (3); and placeholder-only `self` (2). See the run 4 write-up. The order agreed earlier with the reviewer (see ROADMAP 3d): 7.4 (A2 throwaway cwd; acceptance includes "black-style import with absolute-path write is handled", note 6.8a already redirects cache-like writes) and 7.2 (route correctness: a route is not "verified" when every request returned the same early 4xx; "reached N of M" rule) first; 7.1 (impact skill and MCP tool on the resolved call graph) last; 7.5 (Django routes) and 7.7 (`/legacyguard:characterize`) wait for the runnability and determinism fixes; 7.6 (contract tools) independent, split into oasdiff and `makemigrations --check` first. Research is in `docs/research/2026-11-sprint7.md`; its web-sourced claims are unverified (bwrap `--tmp-overlay` flag and version, oasdiff JSON fields, `cp -c`): confirm them before building. Then Sprint 8 (A3 daemon, C1/C2 recorded DB answers).

### Sprint 7.4 Part A (A2 throwaway working folder), built 2026-11-20, reviewer PASS, committed
Plan: `docs/research/2026-11-sprint7-4-plan.md` (updated with the reviewer's round-2 corrections). New
`legacyguard/runner/scratch.py`; wiring in `runner/diff.py`, `lg_worker.py` (`use_cwd`), `lg_server.py`,
`affected_tests.py` (launcher, absolute paths, `--rootdir`), `os_sandbox.active`, `scripts/post_edit.py`,
`guardlib/audit.py`, `guardlib/ci.py`, `guardlib/engine.py` (`record_isolation`), `guardlib/report.py` (Isolation
line), config defaults `cwd_mode: auto`, `cwd: null`. Tests: `tests/test_throwaway_cwd.py`, additions to
`tests/test_affected_tests.py` and `tests/test_warm.py`. Review round 1 REVISE (sub-folder `cwd:` protected only
the sub-folder); fixed with `cwd_root`, plus the optional items (fail-open `sandboxed`, base-36 pid in folder names,
top-level-only build/dist/_build pruning, `.git` pointer files left out). 423 tests pass on Python 3.12 and 3.10.
Tuning-set run after the fix: 8 of 9 / 9 of 9, thefuck-1 quiet without fix tests and detected with, no verdict or
function status/reason changed against run 4 plus the thefuck-1 fix; every case used `clone` under sandbox-exec.
The temp cleanup at midnight also gutted the cached thefuck venv in the validation workdir (moved aside as
`envs/thefuck-1848729c.broken-20261120`, rebuilt). Known limits added: one folder per tree shared across all files
of an audit or `ci.run`; the Isolation line is in the Markdown report only; `_sweep` can misjudge pids across
containers that share /tmp with separate pid namespaces. The 3.10 venv had lost
its `pyvenv.cfg` (temp cleanup) and was recreated; the broken one is at `/private/tmp/claude-501/venv310.broken-20261120`.

7.4 follow-ups, in order:
1. **Part B: baseline folder once per session** (first follow-up). Reuse the baseline's clone across checks; only the
   working tree is cloned per check. Tests to write: staleness keys (baseline commit, `warm._generation`, realpath,
   devices, mode, subdir, `LIST_VERSION`, folder gone); fingerprint re-clone (entry count, size, max mtime); `flock`
   with overlapping hooks (replace into a new folder, retire the old path); retired-folder cleanup at SessionStart.
2. **7.4b bwrap overlay** (`--overlay-src` + `--tmp-overlay`): needs bwrap 0.11+ (CI's Ubuntu 24.04 has 0.9.0) and
   Linux 5.11+; changes the readonly rule (writes into the tree succeed into tmpfs).
3. **Phase 2: imports from the folder** (moves `__file__`-relative absolute writes into the folder).
4. **Empty-mode relative-read rule** (Q4): mark a relative read in an empty cwd as untested.
5. **Masking cwd/tree paths in `lg_codec.canon`** (the folder name also carries the hook pid, which canon may show as
   `<parent pid>` in the cold worker).

## Known open follow-ups (small)
- Pid fix, known limit (documented, pinned by `test_warm.py::test_known_limit_warm_child_ppid_reads_as_pid`): a warm child masks the server's pid as `<pid>`, which it needs for module-level pids captured before the fork. The server is also the child's real parent, so `os.getppid()` reads `<pid>` too. An edit from `return os.getpid()` to `return os.getppid()` is "verified" under warm workers and "different" under cold. The swap is hidden whenever the new side runs warm (both warm, or only the old side fell back to cold); an unchanged `getppid()` with one side warm and the other cold gives a false "different".
- Thread ids are still faked (6.8d: `threading.get_ident`, `get_native_id`, `Thread.ident`/`native_id` return small stable numbers to project code), so they carry the same OS-handoff risk as the old fake pid. Failure modes:
  - `sys._current_frames()[get_ident()]` and `threading._active[get_ident()]` give KeyError or the wrong entry;
  - `signal.pthread_kill(get_ident(), …)` gets a bogus pthread_t;
  - `ctypes.pythonapi.PyThreadState_SetAsyncExc(ident, …)` is a no-op;
  - on Linux, a fake `native_id` breaks `/proc/<pid>/task/<tid>` and psutil thread lookups.
  Proposed fix: keep the idents real and map them in `lg_codec.canon` to stable labels in first-seen order, as was done for pids. Not started.
- LegacyGuard stop-hook audit on this session flagged `install_determinism` as "before None, after AttributeError: module 'lg_codec' has no attribute 'set_pids'". That is a false positive: the new `lg_worker` was run against the old `lg_codec`, so an edit in one worker file that depends on an edit in a sibling worker file was not checked together. Hypothesis only, until someone confirms how the baseline/working-tree runs load sibling modules (the guard's own runner files vs the tree's). To investigate.
- Run 4: home-directory writes at import that are not cache-like are still refused (`~/.ansible/tmp`, which forced the ansible-4 swap; tornado-14's `IOLoop` write).
- Run 4: ansible-3's `parse_distribution_file_Debian` was "verified" on a placeholder `self` (54 inputs, every changed statement reached), possibly without the input that shows the bug. Not credited; not checked by hand.
- `inputs._grid_values` and `_fits_hint` still strip `|None` by substring, so `list[int|None]` loses element Nones there (6.8b note).
- `id()` values and the order of id-hashed sets are classified, not normalised, in the old-vs-old check; one false "different" was seen on a set of id-hashed objects (pre-existing).
- 6.8d: `Lock`/`Semaphore.acquire(timeout)`, selectors and socket timeouts do not advance the frozen clocks; a timed-out call after 200+ frozen readings is reported as a deadline loop.
- 6.8a: `os.path.exists`/`stat`/`listdir` do not see redirected (shadow) files; `_cache_like` is a name heuristic; a SIGKILLed warm server leaks its scratch folder in temp.
- 6.8c: bypass instance copies only top-level `__init__` assignments; the harvested `__init__` types path is untested end to end; the warm-server path has no dedicated test.
- Callers do not get guard or date inputs: `diff._merged_edit` merges only literals, fragments, numbers and regexes.
- Not verified on Linux bubblewrap or on a Django project. The 7.4 FICLONE clone path is untested on a real reflink file system (CI's ext4 picks copy).
- Untracked `.legacyguard/` and `.claude/worktrees/` are runtime state; do not commit them.
- Several untested-reason texts were never re-run by hand (run 4's groups were read from the raw reasons).

## Results so far
- **Held-out set 2 (9 bugs, run 4, the clean number to publish): 8 of 9 unchecked without the fix's tests, 1 of 9 found; with them 7 of 9 unchecked, 2 of 9 found (one only through the fix's own test literals).**
- Held-out set 1 (10 bugs, run 4): 7 of 10 unchecked, 2 of 10 found without the fix's tests (run 3: 8 of 10, 1 of 10); with them 6 of 10 unchecked, 2 of 10 found. Set 1 influenced Sprint 6.8 indirectly, through run 3's failure classes, so this is weaker evidence.
- Tuning set (9 bugs, run 4): 8 of 9 found in both modes; thefuck-1 regressed to unchecked (6.8d frozen pid). The fix brings it back to quiet without fix tests and detected with them, re-run alone. This reflects tuning.
- False alarms not measured (no change cases).
