# legacyguard

A Claude Code plugin that guards one Python backend service. Before a write it checks contracts, test integrity and frozen files. After a write it runs the old and new versions of changed functions on the same inputs and gives Claude any counterexample. At Stop it audits the whole session and writes a report.

It makes no model calls and needs no API key. It runs on your Claude Code plan. Tested on Claude Code v2.1.318, which is the pinned minimum.

## Install

```bash
claude plugin marketplace add turnback-lab/legacy-guard
claude plugin install legacyguard@turnback-lab
```

After that the guard loads in every Claude Code session. Without GitHub: unzip the download package (`dist/legacyguard.zip`, built by `tools/build_package.py`) and run `sh install.sh` once.

```bash
claude --plugin-dir ./legacyguard                    # development, without installing
```

It needs git and a Python 3.10 or newer somewhere on the machine. If the `python3` command is older, the guard finds a newer one by itself.

## Set up a repo

Nothing to do. The first time Claude Code starts in a git repo with Python code, the guard writes `.legacyguard/config.yaml` from what the repo says (a `src/` folder, the Django settings named in the pytest config, an `openapi` file) and reads the codebase. Edit that file to change anything; it is never overwritten. To switch the guard off in one repo, add `enabled: false` to it.

`/legacyguard:index` is optional: it also runs the test suite once, so the map can say which tests reach a function.

A hand-written config looks like this:

```yaml
scope: ./
mode: guarded            # advisory | guarded | strict
app: billing.api.app:app # or django_settings: myproject.settings
contracts:
  http: openapi.yaml
  db: migrations/
  messages: schemas/events/
frozen:
  - legacy/ledger_v1.py
```

The first session start builds the guard's own virtual environment. Optional keys: `python:` (target interpreter, otherwise `.venv`, `venv`, `env`, then PATH) and `source_roots:` (folders to add to the import path, such as `src`).

Under `behavioral_diff:`, `deterministic: false` turns off the fixed random seed, frozen clock and repeatable uuids, and `os_sandbox: off` turns off the operating-system sandbox around the code being run. Both are on by default. `warm: false` turns off the warm worker that keeps the project's libraries loaded between checks.

`mutants: 8` under `behavioral_diff:` sets how many small bugs are planted in the changed lines after a pass, to show how strong the inputs were (0 turns it off).

With `app:` set (FastAPI, Starlette or Flask) and the index built, a change that reaches a route handler also sends the same HTTP requests to the old and the new app in process, and compares status, content type and body.

`affected_tests:` (`enabled`, `max_tests`, `timeout_s`) controls running the project's own tests that reach the changed functions after each edit. It needs the test map from `/legacyguard:index`. A test that passed before the session and fails after an edit is reported to Claude like a behavior difference.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/legacyguard:index` | Developer | Build the index and run the tests once |
| `/legacyguard:status` | Anyone | Findings, mode, prompts used |
| `/legacyguard:impact <symbol>` | Anyone | Callers, routes, tests, co-changed files, and a tree of callers |
| `/legacyguard:map` | Developer | Open the session map: what changed, what depends on it, what was checked |
| `/legacyguard:waive <id> "<reason>"` | Developer | Accept a finding until the code changes |
| `/legacyguard:pin <id>` | Developer | Write a regression test from a counterexample |
| `/legacyguard:mode <mode>` | Developer | advisory, guarded or strict |

## Session report

When Claude stops, the guard writes `.legacyguard/reports/<session>.md`. It opens with "What happened": what you asked, what broke with one example, what fixed it, and what could not be checked. A findings table follows.

## Session map

When Claude stops, the guard writes `.legacyguard/reports/<session>.html` next to the Markdown report. It is one offline file. It shows each function changed this session with its status, the functions that call it up to three steps away, which of those no test reaches, and the API routes that lead to them. Click a function for its counterexample or the reason it is untested.

Edges come from matching names. A solid edge means one test runs both functions; a dashed edge is a name match only.

## Several projects in one folder

If you start Claude in a folder that only holds projects, the guard starts in each git repo directly inside it (up to five) and says which. Each keeps its own settings, findings, report and map. `/legacyguard:status` from that folder shows the project used most recently.

## The guard's files and git

Everything under `.legacyguard/` is ignored by git, so the guard adds nothing to your list of changes. To share the settings with your team: `git add -f .legacyguard/config.yaml`.

## Limits

- Python only. One service per repo.
- The diff compares return values, exceptions, and what a call leaves in its mutable arguments and in `self`. It does not compare writes to a database, files or the network: those are blocked.
- Code under test runs with a fixed random seed and a frozen clock, in an OS sandbox where available (sandbox-exec on macOS, bubblewrap on Linux) plus in-process blocks. Without an OS sandbox, the in-process blocks are best effort only.
- Functions that need a database, the network, or objects it can't build are reported `Untested`.
- A `Verified pass` means no difference on the inputs tried, and that at least one input ran the changed lines. It is not a proof. In a session where Claude may run code, Claude's own comparison of a small function can cover more cases than the guard's.
- Edits made through shell commands are seen only at Stop.
- The map labels API routes as entry points. Scheduled jobs are not indexed, so they appear as ordinary callers.
