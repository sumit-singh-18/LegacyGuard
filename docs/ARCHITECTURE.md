# LegacyGuard architecture (target: v1.0)

This document fixes the architecture we are building toward. It replaces the missing ARCHITECTURE.md that the
v0.5.1 code refers to. Section 1 describes v0.5.1 as it is; sections 2 to 9 are the target design. The visual
version is `docs/architecture-map.html`.

---

## 1. The one idea

> While the developer and Claude change code, LegacyGuard keeps **two copies of the project**:
> - **OLD**: the project exactly as it was when the session started
> - **NEW**: the project as it is right now, with Claude's edits
>
> After every edit it **runs the changed functions in both copies on the same inputs** and compares what
> comes out. If they disagree, Claude is shown the exact input and the two results, and fixes the code
> (or says the change was intended).

There are three kinds of check, each at a different moment:

| When | What | Runs code? | Speed |
|---|---|---|---|
| **Before** an edit lands (PreToolUse) | Static checks: frozen files, API/DB/event contracts, tests deleted or weakened | No (parsing only) | < 1 s, blocks the edit if needed |
| **After** an edit lands (PostToolUse, async) | Behavioral diff: OLD vs NEW on the same inputs | **Yes, both copies** | seconds, in the background |
| **When Claude stops** (Stop) | Whole-session audit, including edits made with shell commands; report and map | Yes, for anything not yet checked | < 60 s |

### What "OLD" means exactly

OLD is the **session baseline**, not "the previous edit". At SessionStart the guard creates a git worktree at
`HEAD` in `.legacyguard/baseline/`, then copies in any uncommitted changes the developer already had. Every
comparison during the session is baseline vs current working tree. So if Claude makes three edits and then
reverts the first, the guard still compares against how things were before Claude started, which is what the
developer cares about.

## 2. Where it lives (installation and settings layers)

```
~/.claude/plugins/…/legacyguard/          ← installed once per machine (claude plugin install)
    .claude-plugin/plugin.json             plugin identity, version
    hooks/hooks.json                       which Claude Code events call which script
    .mcp.json                              the MCP server Claude can query
    skills/*/SKILL.md                      /legacyguard:status, :map, :waive, :pin, :mode, :impact, :index
    guard code (guardlib/, runner/, indexer/, daemon/)
~/.legacyguard/venv/                       the guard's OWN Python env (tree-sitter, hypothesis, …)

<your project>/
    .venv/                                 the PROJECT's Python env. Code under test runs here
    .legacyguard/                          everything the guard writes; git-ignored
        config.yaml                        project settings (can be committed with git add -f)
        waivers.json                       accepted differences, with reasons (committable)
        baseline/                          git worktree = OLD copy
        index.db                           symbols, calls, routes, contracts, tests, co-change
        ledger.db                          findings for this session (v0.5: session.json)
        cassettes/                         recorded I/O for replay (v1)
        reports/<session>.md / .html       report and map
        guard.sock                         daemon socket (v1)
```

**Two interpreters, on purpose.** The guard's own logic runs in `~/.legacyguard/venv`. The code under test
runs in the **project's** interpreter (`.venv`, `venv`, `env`, or `python:` in the config), because only that
interpreter has the project's dependencies. The pieces that run inside the project's interpreter
(`lg_worker.py`, `lg_codec.py`, the harvest plugin) are standard-library only, so they never conflict with the
project.

### Settings precedence

1. `enabled: false` in `.legacyguard/config.yaml`: the guard is off for this project.
2. `.legacyguard/config.yaml`: scope, mode, frozen files, contract paths, budgets, Python path.
3. Environment: `LEGACYGUARD_MODE`, `LEGACYGUARD_HOME` (useful in CI).
4. Built-in defaults (`guardlib/config.py::DEFAULTS`).

**Claude Code settings interplay:**
- The **plugin's hooks** are always on once installed. They do not need permission entries.
- **Skills** call `scripts/lg.py` through their own `allowed-tools`, so they don't prompt the developer.
- A guard **"ask"** decision appears as Claude Code's normal permission prompt, labelled LegacyGuard.
- The **permission mode** (default, acceptEdits, auto, plan) does not change what the guard checks. It only
  changes what happens around it: in `plan` mode no edits happen, so nothing runs. In `bypassPermissions`,
  "ask" turns into allow, so in v1 we treat a session started that way as `advisory` and say so.
- A project's `.claude/settings.json` can disable the plugin (`enabledPlugins`).

### Modes

| Mode | Contract break before edit | Behavior difference after edit |
|---|---|---|
| `advisory` | Context note only | Context note only (no wake) |
| `guarded` (default) | Permission prompt (at most 3 per session, then context) | **Wake Claude** with the counterexample |
| `strict` | Edit denied until waived | Wake Claude, and the Stop hook blocks finishing while differences are open |
| `ci` (v1) | PR comment | PR comment and failing check |

## 3. Target component diagram

```
             ┌───────────────────────── Claude Code session ─────────────────────────┐
 Developer ──► prompt ──► Claude ──► Edit / Write / Bash tool calls                   │
             │                ▲                                                       │
             │   wake (exit 2)│ additionalContext      MCP queries (impact, contract, │
             │                │ permission prompt      declare_change, findings)      │
             └────────────────┼───────────────────────────────┬───────────────────────┘
                              │                               │
                   ┌──────────┴──────────┐          ┌─────────┴─────────┐
                   │ Hook clients (thin) │          │    MCP server     │
                   │ session_start,      │          │  (stdio, thin)    │
                   │ prompt, pre_edit,   │          └─────────┬─────────┘
                   │ post_edit, post_bash│                    │
                   │ stop, session_end   │                    │
                   └──────────┬──────────┘                    │
                              │ unix socket (JSON-RPC)         │
                   ┌──────────▼────────────────────────────────▼─────────────┐
                   │                 GUARD DAEMON (one per project)          │
                   │                                                         │
                   │  Change detector ─► Planner ─► Input factory ─► Scheduler│
                   │  (symbol diff,      (targets,   (harvest, types,   (queue,│
                   │   gates)             call graph, examples, grid,  priority,│
                   │                      risk rank)  Hypothesis,      cancel)│
                   │                                  CrossHair)              │
                   │        ▲                                      │          │
                   │  Static checker                               ▼          │
                   │  (contracts, tests,          ┌──────── Executors ───────┐│
                   │   frozen, oasdiff…)          │ OLD pool     NEW pool    ││
                   │                              │ (baseline)   (worktree)  ││
                   │                              └──────────┬───────────────┘│
                   │  Ledger ◄─ Confidence ◄─ Comparator ◄───┘                │
                   │  (findings)  (mutants)    (return, state, I/O trace)     │
                   │     │                                                    │
                   │     └─► Notifier (wake text, context, reports, map)       │
                   │  Index (SQLite) · Config · Waivers                      │
                   └─────────────────────────────────────────────────────────┘
                                 │ fork per check
       ┌─────────────────────────┴──────────────────────────────┐
       │ Sandboxed run (project's interpreter)                   │
       │  OS sandbox (bwrap / sandbox-exec) + audit hook +       │
       │  throwaway cwd · seeded random, frozen clock ·          │
       │  I/O recorder (OLD) / replayer (NEW) · line tracer      │
       └─────────────────────────────────────────────────────────┘
```

### Why a daemon (the main structural change from v0.5)

In v0.5 every hook is a fresh `python3` process. It reloads the config and index, and the diff starts three
cold interpreters that re-import the whole project. That is simple and robust, but slow, and it can't share
work between edits.

In v1 the hooks become **thin clients** (start, send JSON, print the answer; target < 100 ms of overhead). The
work moves to a **per-project daemon** that SessionStart launches and SessionEnd (or 30 minutes idle) stops.
The daemon keeps:
- the index and config in memory,
- **warm executor pools**: one fork-server per tree, which has already imported the project (and run Django
  setup). Each check is a `fork()` of a warm process, so it starts in milliseconds and dies after the check,
  which means no state leaks between checks,
- a **job queue**: a newer edit to the same function cancels the older check (v0.5 does this by re-reading
  the file at the end).

**Fail-open rule stays:** if the daemon is not running or doesn't answer in time, each hook falls back to the
v0.5 in-process path, or allows and logs. The guard must never break the developer's session.

## 4. Lifecycle, step by step (target)

### 4.1 SessionStart
1. Hook client reads the event (cwd, session_id, permission mode).
2. Find the projects (the cwd, or up to 5 git repos inside it). Load or auto-create `config.yaml`.
3. Make sure the guard venv exists (first run takes about a minute).
4. Create or refresh the **baseline worktree** (OLD) = HEAD + uncommitted files.
5. Start the daemon. It refreshes the index incrementally (by git blob SHA) and warms both executor pools
   in the background.
6. Return a one-line context: "LegacyGuard is watching ./ (mode guarded)".

### 4.2 UserPromptSubmit
Look up functions and files the prompt names (`late fee` → `compute_late_fee`). Add their blast radius as
context: callers, routes, tests, files that usually change with them. Store the prompt; it is used later to
judge intent. **Never blocks.**

### 4.3 PreToolUse (Edit / Write / MultiEdit / NotebookEdit)
1. Apply the edit **in memory** to get the would-be file.
2. Frozen file → **deny**.
3. Static checks against OLD: route removed, parameter renamed, response field dropped or retyped, DB column
   dropped, unsafe migration, event field removed, test deleted, skipped or weakened.
4. Policy (mode + prompt budget + waivers) → allow / ask / deny, plus risk context ("12 callers, 3 routes").
5. Must answer in < 1 s. **No code is executed here.**

### 4.4 PostToolUse (Edit / Write …), asynchronous with asyncRewake
1. Claude continues working immediately; the check runs in the background.
2. **Change detector:** tree-sitter symbol diff OLD vs NEW for that file → changed functions, changed
   statements (the "gate" lines), changed module constants.
3. **Planner:** add callers (resolved call graph, 2 hops, ranked by fan-in, routes and test reach), add
   functions with open findings (to confirm a fix), cap and prioritise.
4. **Input factory**, in order: real arguments harvested from the test suite → literal calls written in the
   repo → edge-value grid (0, ties like 2.675, NaN, inf) → Hypothesis from types (annotations, else types
   seen at runtime) → coverage-guided search toward the gate lines → CrossHair for small pure functions
   (background only).
5. **Executors run each input three times** in sandboxed forks:
   - **OLD-A** in the baseline tree (I/O recorder on),
   - **OLD-B** in the baseline tree again (the noise check: if OLD disagrees with itself on an input, that
     input is dropped),
   - **NEW** in the working tree (I/O replayer serves OLD-A's recorded responses).
   All three have the same seed, frozen clock and fixed uuid.
6. **Comparator** checks, per input: return value or exception, arguments and `self` after the call, globals
   written, and the **I/O trace** (SQL and HTTP requests issued, normalised).
7. **Gate check:** a pass counts only if at least one input executed the changed lines.
8. **Confidence:** on a pass, run up to 10 mutants of the OLD function's changed lines through the same inputs.
   Confidence = mutants caught / mutants run.
9. **Ledger + notifier:**
   - Difference, not waived, not covered by `declare_change` → **exit 2 = wake Claude** with up to 3
     counterexamples (`parse_int_list('5-8') before [5,6,7,8] after [5,6,7]`).
   - Pass → a short factual context note ("ran 140 inputs, no difference, 9/10 mutants caught").
   - Could not run → note with the reason (`needs a database: no recording available`).

### 4.5 PostToolUse (Bash), new in v1
Shell commands can change files (`sed -i`, codemods, `git checkout`). After a Bash call, the client asks git
which tracked `.py` files changed since the last check and queues them as in 4.4. In v0.5 these are only
seen at Stop.

### 4.6 Claude's side: MCP tools
- `impact(symbol)`: callers, routes, tests, co-changed files.
- `contract(path)`: what a contract file defines.
- `diff_report()`: current findings.
- **`declare_change(function, description, examples)`** (v1): Claude states an intended behavior change
  with concrete input→output examples. The guard runs the examples on NEW. If they hold, differences
  **inside that function** are marked "intended". Differences elsewhere still wake Claude.

### 4.7 Stop
1. Diff the whole working tree against the baseline (catches anything missed).
2. Run checks for anything changed and not yet checked; re-run callers.
3. Write `reports/<session>.md` (what you asked, what broke, what fixed it, what was not checked) and
   `reports/<session>.html` (the map).
4. `strict` mode: if differences are still open, **block the stop** with a reason, so Claude must fix or
   declare them. Other modes: report only.

### 4.8 SessionEnd
Stop the daemon. Keep the reports. Remove the baseline worktree, or keep it if another session for the same
project is still running.

## 5. Safety model (running code the agent just wrote)

The code under test is untrusted, because it was just written by an agent. Layers, outermost first:

1. **OS sandbox per fork:** Linux `bwrap --unshare-net`, read-only bind of the repo, writable tmpfs;
   macOS `sandbox-exec` profile (deny network, deny writes outside a temp dir). If neither is available,
   say so in the report.
2. **Throwaway working directory** (copy-on-write), so a write that gets through hits a copy.
3. **PEP 578 audit hook:** deny `open` for writing, `os.remove`, `os.rename`, `shutil.rmtree`,
   `socket.connect`, `subprocess.Popen`, `os.system`, `os.exec*`, `os.fork`, `ctypes.dlopen`.
4. **Monkeypatched boundaries** (as in v0.5) for clearer "blocked: needs a database" reasons.
5. **Limits:** per-call timeout (5 s), per-check timeout, memory rlimit, output size cap. Each fork dies after
   its check.

Layers 3 and 4 are not security boundaries on their own: Python cannot sandbox itself. Layers 1 and 2 are.

## 6. Data contracts

- **Hook to daemon:** JSON-RPC over `.legacyguard/guard.sock`: `{method: "post_edit", event, deadline_ms}` →
  `{exit_code, stdout_json}`. The client turns this into Claude Code's hook output (exit 2 + stderr for a wake,
  `hookSpecificOutput.additionalContext` for context, `permissionDecision` for ask/deny).
- **Executor request:** `{tree: old|new, module, qualname, inputs[], seed, clock, io_mode: record|replay|block,
  cassette_id}` → per input `{outcome, value, state_after, io_trace, lines_hit, blocked_kinds}`.
- **Finding:** `{id: LG-n, kind, status, file, qualname, code_hash, counterexamples[], confidence, state:
  open|resolved|waived|intended|superseded}`. A waiver is tied to `code_hash`, so it expires when the
  code changes again.

## 7. Performance budgets

| Step | v0.5 (typical) | v1 target |
|---|---|---|
| Hook overhead (spawn + load) | 300–800 ms | < 100 ms |
| PreToolUse total | ~1 s | < 1 s (hard 10 s timeout, fail-open) |
| PostToolUse, simple function | 3–10 s | < 3 s |
| PostToolUse, Django app | 15–60 s | < 10 s p95 |
| Stop audit | up to 240 s | < 60 s |

## 8. Same engine, other entry points

| Entry | OLD | NEW | Output |
|---|---|---|---|
| Claude Code session (main) | session baseline | working tree | wake, context, prompt, reports |
| CLI `legacyguard diff` | any git ref (default HEAD) | working tree | terminal and report |
| **CI / GitHub Action** | merge base of the PR | PR head | PR comment, check status |
| Pre-commit hook | HEAD | staged files | block or warn |

Every entry point uses the same daemon library. Only the "where are OLD and NEW" and "where do results go"
adapters differ.

## 9. What stays out of scope for v1

- Languages other than Python (the design keeps tree-sitter and the ledger language-agnostic for later).
- Proofs of equivalence: "no difference found" is always reported as evidence with a confidence, never as proof.
- Model calls from the guard itself: Claude in the session does all the reasoning, so no API key is needed.
