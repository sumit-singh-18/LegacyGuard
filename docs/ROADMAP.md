# LegacyGuard roadmap: from hackathon to a guard you can trust

Status: draft plan, 2026-11-13. **Sprint 1 is done on branch `sprint1-foundations`:** A1 (test suite and CI; the benchmark is still open), A2 (layers 1 and 2; the throwaway cwd is still open), B1 and B2. **Sprint 2 is done too:** B3, B4, C4, and A3 as per-tree warm fork-servers (the daemon is still open). **Sprint 3 is done:** B6, B7 and C3 (FastAPI/Starlette and Flask; Django routes are still open). **Sprint 4 is done:** C1 as stub-and-compare (real recorded answers are still open), D1, E1. It is based on a full read of v0.5.1 (about 5.6k lines of Python) and on the
research listed at the end.

## 0. Where it stands

The core idea holds up and has support in the literature. It is **regression verification without a
specification**: the old version is the oracle (Godlin & Strichman [R1]). The noise filter that runs the old
code twice is the same one Twitter's Diffy uses [W1]. The case for the product is strong: in a study of
SWE-bench Verified, **29.6% of patches that pass the tests behave differently from the developer's fix** [R3].
That is the gap LegacyGuard catches while Claude is still editing.

The prototype is also limited in ways that cap both trust and coverage:

| Area | Today | Problem |
|---|---|---|
| Oracle | Return value or exception type and message | Misses changes to arguments, `self`, globals, DB writes and HTTP calls. Backends mostly *do* things |
| Reach | DB, network and file writes come out `untested` | On a real backend most of the interesting functions are untested |
| Inputs | Harvested from tests, repo literals, a type grid, Hypothesis from annotations | Legacy code has no annotations; classes are built with `owner()` and no arguments |
| Non-determinism | Dropped (self-diff) | Functions that use time, random or uuid end up untested instead of controlled |
| Callers | Name match, 1 hop, at most 6 | False edges and missed edges ("edges come from matching names") |
| Sandbox | Monkeypatching | `os.remove`, `shutil.rmtree`, `os.rename`, `os.open`, `posix_spawn`, `ctypes` and raw `_socket` all get through. **A function under test can delete real files** |
| Speed | 3 cold interpreters per edit, full import and Django setup each time | Seconds to tens of seconds per edit; the check lags the agent loop |
| Engineering | No tests in the repo; ARCHITECTURE.md and TESTING.md missing | Nothing to measure regressions against, and no evidence for accuracy claims |
| "Verified" | "Ran the changed lines and saw no difference" | Says nothing about how strong the inputs were |

## 1. Advancements, ranked by value and effort

### Tier A: foundations (do these first; everything else builds on them)

**A1. Test suite, CI and a benchmark for accuracy.** Commit the missing design docs. Add pytest around
`compare_one`, `inputs.build`, `checks.*`, `policy` and `symbol_diff` (all close to pure functions). Turn
`guardlib/backtest.py` into a real benchmark with three parts:
- **Real regressions:** BugsInPy (493 bugs in 17 projects) [R9] and PyBugHive (149 validated bugs) [R10].
  Reverse each fix to get a buggy edit and measure the detection rate.
- **Synthetic regressions:** mutants from `mutmut` or `cosmic-ray` on target repos (boltons, humanize, a
  Django and a FastAPI app).
- **Behavior-preserving edits, to measure false positives:** automated refactorings (rope or LibCST renames,
  extract-method) and refactoring commits from history. Report detection rate, false-positive rate, untested
  rate, and p50/p95 latency for every release. *Without this, no claim about accuracy can be defended.*

**A2. Harden the sandbox (a safety issue, not a feature).**
1. Add a **PEP 578 audit hook** (`sys.addaudithook`) as a second layer. It sees `open`, `os.remove`,
   `os.rename`, `shutil.rmtree`, `socket.connect`, `subprocess.Popen`, `os.system`, `os.posix_spawn` and
   `ctypes.dlopen` no matter which import path the call came through. Audit hooks are a deny-list and
   **not** a security boundary [W3], so:
2. Add an **OS-level layer** where one is available: Linux `bwrap`/`unshare -n` plus a read-only bind of the
   repo (or Landlock); macOS `sandbox-exec` with a deny-network, deny-write-outside-tmp profile. Fall back to
   (1) alone, and say so in the report.
3. Run inside a **throwaway copy-on-write cwd**, so even a write that gets through cannot reach the repo or
   the baseline worktree.

**A3. Warm workers.** A fork-server per interpreter that imports the project, Django setup and heavy
dependencies once, then `fork()`s per check and swaps in only the changed module. This also gives per-function
process isolation, so one hang no longer makes every function `untested` (today one 60 s worker timeout fails
the whole batch, and SIGALRM does not stop C-level loops). Expect a 5 to 20x cut in latency on Django-sized
apps. Windows has no `fork`: keep the current spawn path there.

### Tier B: accuracy (fewer misses, fewer false alarms)

**B1. A richer oracle: compare state, not only return values.** After each call, canonicalize and compare
(a) the arguments after the call, since mutated lists and dicts are a classic silent change, (b) `self.__dict__`,
(c) module globals the function writes. CrossHair's `diffbehavior` already reports differences as
"after execution a=[...]" [W2]. The codec (`lg_codec.canon`) already exists; this is mostly plumbing.

**B2. Control non-determinism instead of dropping it.** In the worker, before each call: seed `random`,
`numpy.random` and `secrets` the same way for old and new; freeze the clock (patch `time.time`,
`datetime.now` and `date.today`, the way `time-machine` does); make `uuid.uuid4` and `os.urandom` deterministic;
fix `PYTHONHASHSEED`. Keep the self-diff for whatever is left (threads, set ordering). This turns a whole
class of `untested` into checked.

**B3. Build objects for real.** Replace `owner()` with a ladder:
harvested `self` from tests → `hypothesis.strategies.builds(cls)` driven by `__init__` annotations →
dataclass, attrs and Pydantic field introspection → constructor calls found in the repo → factory_boy or
model_bakery factories if the project has them. Today methods are the largest block of untested code.

**B4. Type information for untyped legacy code.** Extend the harvest plugin to record **runtime types**
(MonkeyType-style tracing [W5]) for arguments and return values, along with values. Feed those types into
`_strategy_for` when annotations are missing. Optionally ask pyright/basedpyright for inferred types. This
matters most in exactly the codebases the product is named for.

**B5. Inputs that aim at the change.** The tool already knows the changed lines (`gates`). Use them:
- **Coverage-guided search:** loop Hypothesis with `hypothesis.target()` on "distance to the changed lines",
  or run a short HypoFuzz/Atheris session per function with the new-code gate as the goal.
- **Symbolic search for small pure functions:** run `crosshair diffbehavior old new` [W2] against the
  baseline worktree, in the background with a time budget. This is the practical Python version of
  *shadow symbolic execution*, which runs old and new together and forks inputs where they diverge [R4, R5].
  CrossHair can say "no difference found" when one exists [W2], so treat a pass as evidence, never proof.

**B6. Measure how strong a "verified" is (new, cheap, and high-value).** After a pass, run a handful of
**mutants of the old function** (operator, boundary and constant swaps on the changed lines only) through the
same inputs. If the inputs kill 9 of 10 mutants, the pass means something. If they kill 1 of 10, report
"verified (weak inputs: 1/10 mutants caught)" and push more generation. Mutation score is a much better
predictor of fault detection than coverage: one study found suites with **100% coverage and only 4% mutation
score** [R8]. Meta's ACH uses mutants the same way, to harden tests against regressions [R7]. This turns a
binary status into a calibrated confidence, which is what a developer needs before trusting it.

**B7. Real call graph.** Replace name matching with resolved edges: PyCG (precision ~99%, recall ~70%) [R11],
or pyright/jedi reference resolution, plus **runtime edges** from the coverage-context run that already
happens in `/legacyguard:index`. Then raise the dependent re-run limit to 2 to 3 hops, ranked by fan-in and
test reach. InferCG shows that name-based candidates plus semantic filtering beat pure static analysis on
recall [R12]. Here Claude is already in the loop to do that filtering if it is ever needed.

### Tier C: reach (check the code that matters on a backend)

**C1. Record and replay at the I/O boundary.** This is the biggest win for backends. Run the **old** version
with I/O allowed only against a recorder: DB cursor calls, `requests`/`httpx` and Redis are captured as
(request → response). Run the **new** version against a **replayer** that serves the same responses. Then
compare (a) the return values and (b) **the sequence of I/O requests issued**: SQL text after normalization,
HTTP method, URL and body. A different query or a dropped write *is* the finding. Diffy does the same thing at
the service level [W1]; VCR.py-style cassettes show the mechanics. Where replay has no recorded answer
(because new code issued a new query), report "new I/O: SELECT ..." instead of failing.
*Feasibility:* medium. Patch at the DB-API (`cursor.execute`), `httpx`/`requests` transport and Django ORM
`connection.execute_wrapper` levels. Do not patch sockets.

**C2. Real test DB when the project has one.** If pytest-django or a `conftest` DB fixture exists, run the
diff inside a transaction on the project's test DB and roll back, so ORM code gets checked against real
semantics. Opt-in, since it needs the DB service (testcontainers can supply it).

**C3. Differential testing at the HTTP layer.** For FastAPI, Flask or Django apps (`app:` is already in the
config), send the same requests through `TestClient` to old and new and compare status, headers and body.
Requests come from harvested test traffic, plus **Schemathesis**, which generates requests from the OpenAPI
spec the guard already parses. This checks whole routes, and is the level where API breakage actually hurts
consumers.

**C4. Run the affected tests after each edit.** The coverage map already says which tests reach which
function. After a diff, run just those tests on the new code (regression test selection, Ekstazi-style [R13];
pytest-testmon is the Python tool). This is cheap and catches what developers already wrote down as intended.

**C5. Use mature contract tools instead of regexes.** `oasdiff` for OpenAPI breaking changes,
`buf breaking` for protobuf, `squawk` for unsafe Postgres migrations, `manage.py makemigrations --check`
for Django model drift, and Pydantic/SQLAlchemy model diffing for code-first schemas. Keep the current parsers
as a fallback when no binary is present.

### Tier D: intent (tell the change that was asked for from the one that wasn't)

**D1. Intent declaration through MCP.** Today, when a difference shows up, Claude replies in prose. Instead,
add an MCP tool `declare_change(function, description, examples=[(input, expected_output)])`. The guard then
(a) checks the examples on the new code, and (b) **closes differences that fall inside the declared scope**,
while still flagging differences in *other* functions or on inputs that contradict the declaration. This keeps
the no-API-key design (Claude in the session does the reasoning) and turns waivers into checked statements.
nl2postcond shows LLMs can turn intent into checkable postconditions that catch real bugs (64 Defects4J bugs)
[R6]. Expect some wrong specs [R6b], which is why the declared examples are executed, not trusted.

**D2. A budget for alert fatigue.** Google's experience is that developers ignore analyzers above roughly 10%
effective false positives. Keep the prompt budget, but rank findings by confidence (B6 score, source = tests
over generated, NaN/float-noise demoted, as now) and report precision per finding type from the benchmark (A1).

**D3. Characterization tests on demand.** Generalize `/legacyguard:pin` into `/legacyguard:characterize <module>`,
which writes approval/snapshot tests (Feathers' "characterization tests" from *Working Effectively with
Legacy Code*) from the generated inputs. The guard's knowledge then outlives the session and the plugin.
LLM test generators such as CoverUp reach 80% median line+branch coverage on Python modules [R2], and Claude
can do that job inside the session when asked.

### Tier E: distribution

**E1. CI/PR mode.** The same engine as a GitHub Action: baseline = merge base, targets = PR diff, output =
one PR comment with counterexamples and the session map. This guards PRs from humans, Copilot, Codex and
others, not only Claude Code sessions. It is the biggest lever for adoption.

**E2. More languages, later.** Tree-sitter, the symbol diff, the gates and the ledger are language-agnostic.
The worker and codec are not. TypeScript/Node is the natural second target. Estimate ~40% of the effort of the
Python runner. Do not start until Tier A-C are solid; depth in Python is the selling point.

## 2. Technical bottlenecks and how to handle them

| Bottleneck | Why it is hard | Mitigation |
|---|---|---|
| **Undecidability**: "no difference" is never proof | Equivalence is undecidable in general [R1] | Calibrated confidence (B6), symbolic search where it terminates (B5), and wording that never says "proved" (already a rule) |
| **Oracle noise**: timestamps, ordering, float rounding, memory addresses | Spurious differences cause alert fatigue | Control first (B2), then self-diff, then canonicalize (sort sets and dicts, scrub addresses, float tolerance, already partly done) |
| **Side effects and I/O** | Running them for real is unsafe; blocking them hides the logic | Record and replay (C1), transactional test DB (C2) |
| **Building valid inputs** for deep object graphs and untyped code | The input space is huge and most guesses are invalid | Harvest values *and* types (B4), build objects (B3), coverage-guided search toward the change (B5) |
| **Latency** inside the agent loop | Cold imports of Django and similar apps take seconds | Warm fork-server (A3), async rewake (already), run inputs incrementally and wake on the first difference |
| **Safety** of running code the agent just wrote | Python cannot sandbox itself | OS sandbox plus audit hooks plus copy-on-write cwd (A2) |
| **Import side effects** (settings, DB connections at import time) | Many legacy modules connect when imported | Lazy-connection stubs during import; report `import_blocked` precisely (already partial) |
| **Telling intended from unintended** | Only the developer or Claude knows the intent | Declared intent plus executable examples (D1) |
| **Environment drift**: the project venv is missing or the wrong Python | Untested everywhere | Clear setup report; optional `uv sync` into `.legacyguard/venv` from pyproject or lockfile |
| **Concurrency and async code** | Event loops, threads, Celery tasks | Run coroutines under a fresh loop (already); call Celery task bodies directly; flag threads as non-deterministic |
| **Scale**: monorepos and many-file edits | MAX_FUNCTIONS=12, one service per repo | Rank targets by risk (fan-in, routes, coverage), defer the rest to the Stop audit, support several services in config |

## 3. How feasible this is for developers in practice

| Item | Effort (1 dev) | Risk | Value for users | Notes |
|---|---|---|---|---|
| A1 tests + benchmark | 1–2 wk | Low | Indirect, but needed | Turns claims into numbers |
| A2 sandbox hardening | 3–5 d | Low | **High (safety)** | Audit hook is ~100 lines; the OS layer differs per platform |
| A3 warm workers | 1–2 wk | Medium | High | `fork` plus Django needs care (DB connections, threads) |
| B1 state oracle | 2–3 d | Low | High | Reuses the codec |
| B2 determinism | 3–5 d | Low | Medium-high | Patching inside the worker only |
| B3 object building | 1 wk | Medium | High | Methods are most of real code |
| B4 runtime types | 3–5 d | Low | High for legacy | Extends the existing harvest plugin |
| B5 targeted inputs / CrossHair | 1–2 wk | Medium | Medium | CrossHair is CPython-only, best on pure code |
| B6 mutation confidence | 1 wk | Low | **High (trust)** | Mutants only on changed lines, so it stays cheap |
| B7 call graph | 1 wk | Medium | Medium | PyCG is unmaintained; pyright/jedi is more durable |
| C1 record/replay | 3–4 wk | High | **Very high for backends** | Design the boundary API carefully; start with DB-API and httpx |
| C2 test DB | 1 wk | Medium | High where available | Opt-in |
| C3 HTTP differential | 1–2 wk | Medium | High | Schemathesis does the request generation |
| C4 affected tests | 3–5 d | Low | High | Coverage map already exists |
| C5 contract tools | 1 wk | Low | Medium | Optional binaries |
| D1 intent MCP | 1 wk | Medium | High | Turns waivers into checked statements |
| E1 CI mode | 2 wk | Low | **Very high (adoption)** | Same engine, new entry point |
| E2 TypeScript | 6–8 wk | High | Market-dependent | Later |

**Suggested order.** Sprint 1: A1, A2, B1, B2. Sprint 2: A3, B3, B4, C4. Sprint 3: B6, B7, C3.
Sprint 4: C1 and D1. Then E1. Re-run the benchmark after each sprint and publish the numbers in the README.

## 3b. Validation on real repositories (after the sprints)

During the sprints, every feature ships with tests on small fixture projects (`tests/`). After that comes a
separate validation phase on real open-source Python code, and the guard gets fixed based on what it finds:

1. **Pick the repos.** Pure-Python libraries (boltons, humanize, more-itertools), a Django app, a FastAPI app,
   and BugsInPy subjects (real bugs with known fixes).
2. **Replay history.** For each repo, replay recent commits through `guardlib/backtest.py`, and replay reversed
   bug fixes (the bug should be caught) and pure refactors (nothing should be flagged).
3. **Live sessions.** Run real Claude Code sessions with the plugin installed on those repos, using the README's
   kind of requests. Record wakes, false alarms, untested reasons and latency.
4. **Triage and fix.** Each false alarm or miss becomes a fixture test in `tests/` first, then a fix.
5. **Publish the numbers** in section 4 in the README.

**How to run.** `tools/validate.py` replays cases from a manifest (YAML or JSON; see
`tools/validation/manifest.example.yaml`, whose entries are placeholders to fill from research). A `bug` case checks
out the `fixed` commit and puts the `buggy` source files into the working tree (the guard should report a
difference; only differences in the bug's `files`, taken from BugsInPy's `bug_patch.txt`, or in their callers count);
a `change` case checks out `head` against `base` with `expect: quiet|detected`. Each case runs through
`guardlib.ci.run` (its `overrides` add `app`, `source_roots` and mutants on). Run `.venv-dev/bin/python tools/validate.py MANIFEST --workdir DIR --out DIR`:
repositories are cloned into `--workdir` and reused, per-repo `setup` commands run once (`$LG_VENV` is a cached venv
folder), and `results.json` plus `results.md` go to `--out` with the detection rate, false-alarm rate, untested
share, mutation score, median/p95 time per case and the target Python version. A case that fails (including an
interpreter that cannot run or is older than 3.7) is recorded as `error` and the run goes on; a case where no
function was verified, different or broken is `unchecked`. Rates count neither; both are listed separately. The harness itself is tested on local fixture repos (`tests/test_validate.py`).

## 3c. Sprint 5: from validation run 1 (docs/validation/2026-11-bugsinpy.md)

Baseline to beat: 2 of 7 checkable BugsInPy bugs detected without the fix's own tests, 2 unchecked.

| Task | What | Acceptance |
|---|---|---|
| A | Gate callers on the callee's changed lines; gate every changed statement, not any; skip comment/whitespace-only changes; skip callers that cannot be called | No result is "verified" unless an input reached every changed statement it claims; tqdm 4 and youtube-dl 5 (no fix tests) become unchecked/untested instead of quiet; existing tests still pass |
| B | Inputs from tests and from the edit: arguments passed through local variables, objects for parameters built from constructor calls in tests, edit literals and regex character classes spliced into examples | youtube-dl 10 and thefuck 7 get inputs that distinguish the versions; youtube-dl 1 and 3 detected without the fix tests |
| C | Harness: "without fix tests" mode on a synthetic base commit (headline number), and the source of each deciding input | Both numbers reported per run |
| D | Planted bugs for strings and regexes | js_to_json-style changes get a confidence score |
| E | Record the arguments callers pass to the edited function and reuse them as its inputs | tqdm 4's format_meter reached with total=None and a numeric unit_scale, or a stated reason why not |
| F | Re-run the nine bugs in both modes | A reviewer-checked comparison with run 1 |

## 4. Success metrics to publish

- Detection rate on BugsInPy reversals and on mutants (target: >70% for functions that can be checked).
- False-positive rate on behavior-preserving refactors (target: <5%, following the Google guidance).
- Share of changed functions that come out `untested` (target: from today's majority on backends to <30%).
- p95 latency from edit to verdict (target: <10 s on a mid-size Django app).
- PatchDiff-style replication: run the guard on SWE-bench Verified agent patches against gold patches and
  measure how many of the ~30% divergent patches [R3] it flags.

## References

Papers
- [R1] [Regression verification](https://consensus.app/papers/details/w:78722482-7b29-450d-ac17-d91f00843e15/?utm_source=claude_code). Godlin & Strichman, DAC 2009.
- [R1b] [Partial Contracts Suffice: Sound, LLM-Inferred Regression Verification](https://consensus.app/papers/details/w:ca15361f-5f93-4192-b672-5ffc6a571af6/?utm_source=claude_code). Charalambous et al., 2026 (preprint).
- [R2] [CoverUp: Effective High Coverage Test Generation for Python](https://consensus.app/papers/details/w:42572836-a856-4581-a9f3-2d85fd3d8e24/?utm_source=claude_code). Pizzorno & Berger, FSE 2025.
- [R3] [Are "Solved Issues" in SWE-bench Really Solved Correctly?](https://consensus.app/papers/details/w:5bcaa0cc-bfec-423b-a41e-dd92ae95fd32/?utm_source=claude_code). Wang et al., ICSE 2026 (PatchDiff).
- [R4] [Shadow of a doubt](https://consensus.app/papers/details/w:a4fda703-a868-4246-bf3e-f3165d06a5e9/?utm_source=claude_code). Palikareva, Kuchta & Cadar, ICSE 2016.
- [R5] [Shadow Symbolic Execution for Testing Software Patches](https://consensus.app/papers/details/w:bb539aa4-21b9-40f8-b3a7-2b09850a4446/?utm_source=claude_code). Kuchta et al., TOSEM 2018.
- [R6] [Can LLMs Transform Natural Language Intent into Formal Method Postconditions?](https://consensus.app/papers/details/w:22affa87-df13-47f9-99a2-1079c6bdeb54/?utm_source=claude_code). Endres et al., FSE 2024.
- [R6b] [Talk is Cheap, Logic is Hard: Benchmarking LLMs on Post-Condition Formalization](https://consensus.app/papers/details/w:7131d8ab-9d1d-42e1-88ac-45f427ab6382/?utm_source=claude_code). Prasetya et al., 2026 (preprint).
- [R7] [Mutation-Guided LLM-based Test Generation at Meta](https://consensus.app/papers/details/w:686bec44-33f1-4dca-a38e-971f76a3e651/?utm_source=claude_code). Harman et al., FSE 2025.
- [R8] [Mutation-Guided Unit Test Generation With a Large Language Model](https://consensus.app/papers/details/w:3b815094-d632-40be-973b-8c3833c0b672/?utm_source=claude_code). Wang et al., TSE 2026.
- [R9] [BugsInPy](https://consensus.app/papers/details/w:60eb91aa-b82d-47ce-a35b-65b53927376e/?utm_source=claude_code). Widyasari et al., FSE 2020.
- [R10] [PyBugHive](https://consensus.app/papers/details/w:be0037b3-1dad-4948-9a35-49b7021e1c15/?utm_source=claude_code). Antal et al., IEEE Access 2024.
- [R11] [PyCG: Practical Call Graph Generation in Python](https://consensus.app/papers/details/w:e8bdd6a7-3a57-4370-b2b0-616eb765e982/?utm_source=claude_code). Salis et al., ICSE 2021.
- [R12] [InferCG: Python Call Graphs via Static Analysis and LLMs](https://consensus.app/papers/details/w:942a7906-e384-4235-921f-68aa884d6e43/?utm_source=claude_code). Xiang et al., TOSEM 2026.
- [R13] [Practical regression test selection with dynamic file dependencies (Ekstazi)](https://consensus.app/papers/details/w:16a8b303-b613-43bb-9691-096592adec2a/?utm_source=claude_code). Gligoric et al., ISSTA 2015.
- [R14] [The Patchwork Problem in LLM-Generated Code](https://consensus.app/papers/details/w:4f93c48b-72c3-40c9-8fba-18366362fc34/?utm_source=claude_code). Mothukuri et al., 2026 (preprint). Structural incoherence that evades tests; supports C5 and B7.
- Sadowski et al., "Lessons from Building Static Analysis Tools at Google", CACM 2018 (the ~10% false-positive threshold).
- M. Feathers, *Working Effectively with Legacy Code* (characterization tests).

Tools and posts
- [W1] Diffy: [Twitter engineering blog](https://blog.x.com/engineering/en_us/a/2015/diffy-testing-services-without-writing-tests), [repo](https://github.com/twitter-archive/diffy)
- [W2] CrossHair `diffbehavior`: [docs](https://crosshair.readthedocs.io/en/stable/diff_behavior.html), [false "same" report](https://www.github.com/pschanely/CrossHair/discussions/193)
- [W3] [PEP 578: Runtime Audit Hooks](https://peps.python.org/pep-0578/) (explicitly not a sandbox)
- [W4] [Hypothesis Ghostwriter `--equivalent`](https://hypothesis.readthedocs.io/en/latest/ghostwriter.html)
- [W5] [MonkeyType (Instagram)](https://instagram-engineering.com/let-your-code-type-hint-itself-introducing-open-source-monkeytype-a855c7284881)
- [W6] [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) (`asyncRewake`)
- Schemathesis, oasdiff, buf, squawk, pytest-testmon, mutmut, time-machine, VCR.py: see each project's docs.
