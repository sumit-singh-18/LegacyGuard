# Sprint 7.6 build spec, Part D: `buf breaking` for Protocol Buffers contracts

Researcher agent, 2026-11-26, main at 6060062 (Part C on top of the process-group fix). **Reviewer PASS in round 3 (2026-11-26).**

**User decisions (2026-11-26, final):** D8 yes: the implementer may download
`https://github.com/bufbuild/buf/releases/download/v1.75.0/buf-Darwin-arm64` once into the session scratchpad, verify
its sha256 against the release's `sha256.txt` before `chmod +x`, and never put it on `PATH` globally. D7 (b): a pinned,
checksum-verified release binary in `.github/workflows/tests.yml` (Linux x86_64 and macOS arm64). D1 (b): Stop and CI
only; PreToolUse only in a later change if the D8 timing passes the gate (report the measurement, do not enable it).
D2, D3, D4, D5, D6, D9, D10 and D11 as recommended.

**Build note (2026-11-26, real buf 1.75.0; build reviewer PASS round 1):** the real binary refuses
`--config`/`--against-config` with a folder input ("contained by module at path \".\""), so 5.2 step 3, test 6 and AC3
are superseded: the command is `buf breaking rev#format=dir --against base#format=dir --error-format json
--disable-symlinks`, with our v2 `buf.yaml` written inside `rev/` and `base/` (rules follow `rev/buf.yaml`). Also from
the binary: a missing import is a `COMPILE` line on stdout ("imported file does not exist"), not stderr text;
FILE_NO_DELETE is path-less with 1/1/1/1; MESSAGE_NO_DELETE and SERVICE_NO_DELETE carry the rev path with 1/1/1/1; a
rename also reports FIELD_SAME_JSON_NAME; an empty `rev/` and usage errors exit 1; a lone CR puts every annotation on
line 1. D1 gate measured and passed (0.089 s and 0.259 s medians); PreToolUse stays off per D1 (b).

**Round-2 corrections (reviewer, 2026-11-26) are folded in and override earlier text, round 1 included.**
R2-1 `_proto` has a fixed order (5.4): unset → `buf-unset` check, `None`; invalid or out of scope → `not_checked`, kind
`config`, on **every** Stop whatever `changed` holds (note once); `applies` false → `None`; else compute. So an invalid
folder keeps its findings (test 23 f) instead of sweeping them. 5.1 is reworded to match.
R2-2 the round-1 paragraph now cites tests 8b, 29b, 29c (it said 36, 37, 38).
R2-3 D11: in buf 1.52.0 and 1.52.1 `MESSAGE_SAME_JSON_FORMAT` prints a correct path but a garbled message (the file path
plus Go's `%!(EXTRA ...)`), so that rule's subject and key are garbled there; it is an ordinary break, not a failed run.
The 1.32.0 minimum is unaffected (D11, 2.3).
R2-4 acceptance criterion 7: `off` and a removed `contracts.proto` sweep; every other unchecked kind, `config` included,
keeps findings.
Optional, all done: (a) the reviewer's round-2 source reads move to confirmed (2.3, 2.5, 9), only
`getDescriptorAndLocationForDeletedMessage` stays unverified; (b) D11 cites v1.32.0 source; (c) 6.7 records the empty-input
exit code and text; (d) test 23 (h) uses the one concrete out-of-scope case.

**Round-1 corrections (reviewer, 2026-11-26) are folded in and override earlier text.** The reviewer confirmed the
architecture (temp `base/`/`rev/`, our own v2 `buf.yaml`, `#format=dir`, `BUF_*` stripped, sandbox, decisions from the
JSON lines, and D6's sweep when no `.proto` changed). Changes, each marked "R1-n" where it lands:
R1-1 D11 minimum is now 1.32.0 with a sourced reason; the v1.53.0 "JSON" fix was about the `MESSAGE_SAME_JSON_FORMAT`
rule, not `--error-format json`; the v1.68 compiler split is listed as unverified (D11, 5.2, 9).
R1-2 `contracts.proto: .` is the empty prefix; `..` is refused (5.1, 5.3, test 8b).
R1-3 scope: a folder outside scope is never checked and gets one note; auto-setup writes the folder relative to scope and
only inside it (D5, 5.1, test 28).
R1-4 waivers keep the existing lookup (file, item); test 24 and section 8 now say what that does (5.4, 8).
R1-5 the "unset key" note is specified: kind `buf-unset` (5.3, 5.4, test 29b).
R1-6 invalid `contracts.proto` is not checked (kept); a key removed mid-session sweeps with a report line (D6, 5.4, test 23).
R1-7 `line` is `None` when all four positions are 1; annotations with a path can lack a position (2.3, 5.3, fake, test 9).
R1-8 a path component named `buf.yaml`, `buf.work.yaml` or `buf.lock` refuses the run (5.2, test 3).
R1-9 D2 (ii) counts 16 rules and names the two exclusions.
R1-10 version parsing rule (5.2, test 29c).
Optional items: (i) adopted, missing-import text now sourced (2.5, 5.2); (ii) adopted, an empty side is
`ErrNoTargetProtoFiles` (2.5, 5.3); (iii) adopted, decoys at two levels (tests 7, 32); (iv) adopted, COMPILE columns are
recorded as UTF-8 bytes per the reviewer (2.3; still unused); (v) adopted, separate note kinds `buf-category` and
`buf-folder` (5.1); (vi) adopted, the SessionStart probe is stated in section 3.

Self-contained: the implementer should not need the Part A, B or C plans, but this part reuses their code
(`guardlib/contract_tools.py`: `find`, `refresh`, `_run`, `Refusal`, `take_notes`, `report_line`; the Stop-side outcome
pattern of `audit._drift`/`_drift_findings`; `audit._squawk_kept`'s lifecycle rule; `os_sandbox.run` and `os_sandbox.wrap`).
Spec origin: Part A plan section 10 (unreviewed sketch), `docs/HANDOFF.md` "Next (start here)", `docs/ROADMAP.md` C5.

Fixtures in this plan are generic shapes (a `proto/` folder with `acme/user/v1/user.proto`, messages `User`,
`Address`, a service `UserService`). None of the held-out cases in `docs/validation/holdout-set.md` or
`holdout-set-2.md` is used or named.

Nothing was downloaded, installed or run. Every buf claim below was read in buf's source at the tag `v1.75.0` (raw files
at `https://raw.githubusercontent.com/bufbuild/buf/v1.75.0/<path>`), the `buf.build/go/app` module buf uses (v0.3.0, source
`github.com/bufbuild/app-go` at `v0.3.0`, located through `https://proxy.golang.org/buf.build/go/app/@v/v0.3.0.info`), the
buf.build docs, the GitHub releases API, the release's `sha256.txt`, the npm registry, PyPI and the Homebrew formula API.

What the sketch got right, and what it missed (each checked below):

| Sketch (Part A section 10, HANDOFF) | What the sources say |
|---|---|
| latest v1.75.0, 2026-10-22 | **Confirmed**: v1.75.0, `published_at` 2026-10-22T01:02:29Z, not prerelease (CHANGELOG dates it 2026-10-21). Nothing newer on 2026-11-26. v1.74.0 2026-10-14, v1.73.0 2026-09-11 (2.1). |
| `buf breaking <tree> --against <baseline dir> --error-format json` | **The flags exist** (`breaking.go` lines 45-56, 103-166), but the form is unsafe as written: (1) a path with no format option is a module reference when it parses as one and is not a directory (`assumeModuleOrDir`), so pass `#format=dir` explicitly; (2) for a directory input buf **walks up from the input to `/`** looking for a `buf.yaml` v2 or `buf.work.yaml` that controls it, and that file then sets the bucket root and the rules; `--config` does not stop the walk (2.4); (3) the session baseline lives *inside* the project (`.legacyguard/baseline`), so the project tree as input would also contain a second copy of every `.proto` (2.9). Recommended form: copies of the `.proto` files in a fresh temp folder, each side with our own `buf.yaml` (5.2). |
| "flags and JSON unverified" | JSON read in source: **one JSON object per line (not an array)**, keys `path`, `start_line`, `start_column`, `end_line`, `end_column`, `type`, `message`, `plugin`, `policy`, all `omitempty`; positions are 1-based and never 0 (`atLeast1`); `path` is **absent** for annotations without a new-side file (a deleted file, a deleted package) (2.3). |
| (not mentioned) exit codes | **0** = no breaking change; **100** = annotations printed (breaking changes **or compile errors**, which are also printed as JSON lines with `type: "COMPILE"`) **or** a missing import (exit 100, text on stderr, nothing on stdout); **1** for other errors (2.5). Decide from the parsed lines, never from the exit code alone. |
| "needs a `buf.yaml` in the project" | **Not needed**, and better not used: LegacyGuard writes its own `buf.yaml` (v2, no deps, `breaking.use` = one category) into each side; the project's `buf.yaml`, `buf.work.yaml`, `buf.lock` are never copied (D3, D5). Without any `buf.yaml` buf uses the `FILE` category (docs; source default). |
| "Stop only" | Re-examined in D1. No published timing exists and the binary was not run; buf builds **both** trees' images in full on every run (`breaking.go` lines 198-246). Stop and CI recommended; PreToolUse only behind a measured gate (D1, D8). |
| (not mentioned) contract config | LegacyGuard has **no proto support today**: `contracts` knows `http`, `db`, `messages` only (`indexer/contracts.py`, `checks.run_checks`); a `.proto` edit gets no static check at all (2.9). Minimal support: a new folder key `contracts.proto` (D5). |
| (not mentioned) in-file ignores | `// buf:lint:ignore` comments **do not apply to `buf breaking`** (`bufcheck/client.go` line 815: "will never be triggered by breaking"). Nothing to neutralise in the `.proto` text (2.7). |
| (not mentioned) caches, env | buf **writes** a cache on every run (`$BUF_CACHE_DIR`, else `$XDG_CACHE_HOME/buf`, else `$HOME/.cache/buf`: Wasm runtime, well-known types) and reads `BUF_*` variables (`BUF_TOKEN`, `BUF_INPUT_HTTPS_*`, and since v1.75.0 `BUF_DEBUG`, `BUF_LOG_FORMAT`, `BUF_TIMEOUT`) (2.6). |
| (implied) Python install route | **There is no official PyPI package.** The PyPI name `buf` belongs to an unrelated chemistry library (`buf` 1.0.0, "For easily making chemical buffers and solutions"). Official routes: release binaries (with `sha256.txt`), Homebrew `bufbuild/buf/buf` (core formula `buf` is 1.75.0 too), npm `@bufbuild/buf` 1.75.0, `go install`, Docker `bufbuild/buf`, GitHub Action `bufbuild/buf-action` (2.1). |

## 0. Summary

When buf is installed (config `contract_tools.buf: auto|false|<path>`, else `PATH`) and `contracts.proto` names the
folder the project's `.proto` imports are relative to, LegacyGuard checks the whole proto module of the session baseline
against the working tree once per Stop (and once per `ci.run`) whenever some `.proto` file under that folder differs from
the baseline:

1. Copy every tracked-or-untracked, not-ignored `*.proto` regular file under the folder (the same `git ls-files -co
   --exclude-standard` listing `baseline.changed_files` uses, `.legacyguard/` excluded) from the baseline tree into
   `<tmp>/base/` and from the working tree into `<tmp>/rev/`, paths relative to the folder. Nothing else is copied
   (no `buf.yaml`, `buf.work.yaml`, `buf.lock`, `buf.gen.yaml`, no other file).
2. Write our own `buf.yaml` into `<tmp>/base/` and `<tmp>/rev/` and as `<tmp>/lg-buf.yaml` (identical: `version: v2`,
   `breaking: {use: [<category>]}`, no `deps`, no `modules`, no `lint`).
3. Run, with cwd `<tmp>`, through `os_sandbox.run` (process-group kill) and, when available, inside `os_sandbox.wrap`
   (no network, writes only to `<tmp>`):
   `buf breaking rev#format=dir --against base#format=dir --error-format json --config lg-buf.yaml
   --against-config lg-buf.yaml --disable-symlinks`, env without any `BUF_*` variable and with `BUF_CACHE_DIR`,
   `BUF_CONFIG_DIR`, `BUF_DATA_DIR`, `HOME` pointing into `<tmp>`, stdin empty, `timeout_s` (20 s).
4. Parse stdout as JSON lines. Exit 0 with no lines: no break. Exit 100 with lines: each non-`COMPILE` line is a
   `Break("proto", item, subject, "buf", rel, line=...)`, i.e. a `contract_break` finding (FAILURE, blocks the Stop once),
   a CI `::warning`. Any `COMPILE` line, a missing import, a timeout, a missing or too-old binary, bad output: no break,
   one note per kind per session, and the run is "not checked" with a kind.
5. Item key `buf:<rule id>:<repo rel or ->:<message[:80]>:<sha12(rule, rel, message)>`; buf's messages carry
   fully qualified names and no line numbers, so keys survive unrelated edits.
6. Lifecycle (D6): switched off sweeps open `buf` findings to `not_applied`; every other unchecked kind keeps them open.

Hooks (D1): Stop and `ci.run`; not PostToolUse; PreToolUse only if the D8 measurement passes the gate in D1. There is no
built-in fallback parser (D9): without buf a `.proto` edit stays unchecked, and the report says so.

## 1. Open decisions for the user (each with a recommendation)

| # | Decision | Recommendation |
|---|---|---|
| D1 | Which hooks run buf. (a) PreToolUse (0.8 s cap, skip rule) plus Stop and CI; (b) Stop and CI only; (c) also PostToolUse. | **(b) now; (a) only if the D8 measurement passes a gate.** Sourced cost: one `buf breaking` builds both images from source (`breaking.go` 198-246: `GetTargetImageWithConfigsAndCheckClient` for the input, then for `--against`), creates a Wasm runtime on every run (`bufcli.NewWasmRuntime`, `breaking.go` 189), and the binary is 45.4 MB (`buf-Darwin-arm64`, releases API). No published timing exists and nothing was run here, so the PreToolUse cost is **unknown**, and unlike squawk (two small texts) it grows with the whole module, not the edited file. A PreToolUse run would also need both sides copied per edit. Gate for (a): with the real binary (D8), median of 10 runs ≤ 0.3 s on the 3-file fixture **and** ≤ 0.8 s on a generated 200-file module (test 34); then PreToolUse can be switched on with the same `pre_edit_allowed(cfg, "buf")` skip rule, as a small follow-up. What (b) loses: a breaking `.proto` edit is not asked about before it lands; the Stop audit catches it (blocks once) and CI annotates it. (c) adds nothing: the files are static and Stop sees shell edits too. |
| D2 | Which breaking category, and how rules map. Categories (v2): `FILE` (default), `PACKAGE`, `WIRE_JSON`, `WIRE`, plus `CSR` (Confluent Schema Registry, added v1.54.0). | **`FILE` by default, every reported rule a break**, configurable `contract_tools.buf_category: FILE|PACKAGE|WIRE_JSON|WIRE` (anything else, `CSR` included: refused with a note, `FILE` used). Reasons: (1) it is buf's own default and the docs' advice ("start with `FILE` (the default)"); (2) LegacyGuard guards Python backends, and Python's generated code is **one module per `.proto` file** (`user_pb2.py`), so moving a message to another file breaks `from ..user_pb2 import User` in callers: a per-file source break, which only `FILE` checks (`MESSAGE_NO_DELETE`, `ENUM_NO_DELETE`, `FILE_NO_DELETE` are FILE-only, `bufcheckserver.go` V2Spec); (3) buf runs only the rules of the chosen category, so every non-`COMPILE` annotation is a break by construction and no rule table can go stale. Alternatives: (i) `WIRE_JSON` (fewer findings: only changes that break binary or JSON clients; renames of fields still count, moves between files and packages do not); (ii) `FILE` but demote the 16 language and file-option rules (`FILE_SAME_GO_PACKAGE`, `FILE_SAME_JAVA_PACKAGE`, `FILE_SAME_CSHARP_NAMESPACE`, `FILE_SAME_OPTIMIZE_FOR`, ...; R1-9: V2Spec has 18 `FILE_SAME_*` rules, and the two not demoted are `FILE_SAME_PACKAGE`, which is in every category including `WIRE`, and `FILE_SAME_SYNTAX`, in CSR, FILE and PACKAGE) to one context note per run, on the grounds that a Python backend rarely ships those clients. Not recommended as the default: they are real source breaks for clients in those languages, and a project that does not care can pick `WIRE_JSON` or waive. |
| D3 | Honour the project's `buf.yaml` / `buf.work.yaml` / `buf.lock`. (a) No: copy only `.proto` files, our own `buf.yaml` on both sides and as `--config`/`--against-config`; (b) read the project's `buf.yaml` for the module layout (module paths, `excludes`) but not its `breaking`/`deps`; (c) honour it fully. | **(a).** A `buf.yaml` with `breaking: {except: [FIELD_NO_DELETE]}` or `ignore: [acme/]`, or a `buf.work.yaml` placed in any parent of the input, is one edit away for Claude and would silence the check (`client.go` `ignoreFileLocation`: `IgnoreRootPaths`, `IgnoreRuleIDToRootPaths`; `terminate.go`: a parent `buf.work.yaml` or v2 `buf.yaml` becomes the controlling workspace). The temp-folder layout makes the result independent of every file except the `.proto` texts: the walk-up stops at the input itself because the input holds a v2 `buf.yaml` ("A input directory with a v2 buf.yaml is the controlling workspace for itself", `terminate.go`), and `--config` fixes the rules even if that changed. The project's intent goes through `/legacyguard:waive`. (b) is a reasonable follow-up for multi-module repos (D5 covers the single-root case); (c) re-opens the bypass. |
| D4 | Modules with dependencies (`deps` on BSR modules, or imports from folders outside `contracts.proto`). (a) Refuse: no network; an import buf cannot find ends the run as "not checked" (kind `imports`) with a note naming the import; (b) fall back to a check without the missing files (`--exclude-path`, or dropping importers); (c) allow BSR fetches (network, cache). | **(a).** buf fetches BSR deps over the network into its cache; LegacyGuard never lets a tool reach the network (Part A D5, D8). (b) would build a different module than the project's and report false "deleted" breaks for the dropped files. Well-known types (`google/protobuf/*.proto`) are built into buf (`bufimage/well_known_type_image_file_info.go`; v1.75.0 "Update built-in Well-Known Types to Protobuf v36.2"), so they need nothing. Vendored third-party protos **under** `contracts.proto` (a copied `google/api/annotations.proto`) work and are checked too. A follow-up could add `contract_tools.buf_include: [third_party/]` (copied as read-only extra roots). |
| D5 | Is a `buf.yaml` required, and where does the module root come from? (a) A new config key `contracts.proto: <folder>` (the import root), set by hand; (b) (a) plus auto-setup: on first use, if the repo has one `buf.yaml` (v1, or v2 with at most one module) the generated config gets `contracts.proto: <that module's folder>`; (c) require a project `buf.yaml` and use its location. | **(b).** No project `buf.yaml` is needed for the check itself (we write our own). The folder must be the import root (imports are relative to it), which a `buf.yaml` states when there is one. Auto-setup reads it once, when `.legacyguard/config.yaml` is first written (`autosetup.setup`); later edits by Claude do not change the config, and Claude cannot edit `.legacyguard/` through Edit/Write (`pre_edit.py` lines 88-89). Auto-setup reads only `version` and `modules[].path` (v2) and never `deps`/`breaking`; anything else (two modules, a `buf.work.yaml`, parse errors) writes nothing. R1-3: contract paths are relative to `scope` (`Config.contract_path`, config.py 53-59), and auto-setup uses scope `src/` for src layouts (autosetup.py 72), so auto-setup writes the module folder **relative to scope** (`proto` at repo root with scope `./` → `proto`; `src/proto` with scope `src/` → `proto`) and writes nothing when the folder is outside scope (`proto/` with scope `src/`). A hand-set folder outside scope is never checked: `changed_files` filters by scope (baseline.py 108-116), so no change there would ever be seen; instead of a separate unscoped listing, the first Stop gives one note (kind `buf-folder`, 5.1) and nothing runs. Reason: keeping one listing keeps "scope" meaning the same for every check. R1-5: without the key, `.proto` files are not checked, with one note (kind `buf-unset`, condition and text in 5.3) at the first Stop that sees a changed in-scope `.proto` (D9). (c) loses repos that use `protoc` without buf. `ci.load_cfg` must map the new detect key (it copies only `contracts` today, which `detect` never returns: an existing gap noted in 2.9). |
| D6 | Lifecycle of open buf findings when buf does not check this Stop. (a) Switched off (`contract_tools.buf: false`) sweeps them to `not_applied`; missing, too old, timeout, failed start, bad output, compile error, missing import, too large, no sandbox where required: kept open; (b) keep in every case; (c) sweep in every case. | **(a)**, as Part C D11 and Part A drift. Turning the check off is the developer's choice and the report says "buf switched off"; any other failure says nothing about the `.proto` files, so sweeping would read as "fixed". A Stop where no `.proto` under the folder differs from the baseline runs nothing and lets the sweep close buf findings (the module equals the baseline, so no break can stand), as drift does with `drift=None`. R1-6: (a) an invalid `contracts.proto` (ends `.proto`/`.yaml`/`.yml`, normalises to `..` or outside the repo, or lies outside scope) is `not_checked`, kind `config`: findings kept, one note. (b) The key removed mid-session: `_proto` returns `None` and the sweep closes open buf findings, like "switched off"; the report's buf part then reads "buf not run: contracts.proto not set" whenever the ledger holds any `buf` finding (so the closure is never silent). Reason: removing the key is the developer's choice, the same as `buf: false`. |
| D7 | Install buf in CI. (a) No (fake binary only; real-binary tests skip); (b) a pinned, checksum-verified release binary in `.github/workflows/tests.yml` (`buf-Linux-x86_64` on ubuntu-latest, `buf-Darwin-arm64` on macos-latest), not in `requirements-dev.txt`; (c) `npm install @bufbuild/buf@1.75.0` (needs Node on the runners). | **(b) if D8 is yes, else (a).** There is no pip package (2.1), so `requirements-dev.txt` cannot carry it; a `curl -fsSL` of `https://github.com/bufbuild/buf/releases/download/v1.75.0/buf-$(uname -s)-$(uname -m)` checked against the published `sha256.txt` value (Linux x86_64 `fad1e38d...764b`, Darwin arm64 `f71b009e...330b`, full values in 2.1) adds ~45 MB per job and closes the "real binary never run" gap Part A still has for oasdiff. It is a third-party binary in CI, so it is the user's call. Never `pip install buf` (an unrelated package). |
| D8 | May the implementer download the real buf once to check flags, the JSON lines, exit codes, the walk-up refusal and timing? Exact source: `https://github.com/bufbuild/buf/releases/download/v1.75.0/buf-Darwin-arm64` (raw binary, **45,408,338 bytes**, sha256 `f71b009ebd8530b12fd85b060c6c245e15d6e5110ccc8da48d8b6fa71e59330b` per the release's `sha256.txt`), into the session scratchpad, verified with `shasum -a 256` before `chmod +x`, never on `PATH` globally. | **Yes, recommended.** Every claim in section 2 comes from reading source; none was run. One run settles the JSON bytes and key order, the `path` form for a directory input (`rev/acme/...` or not, UNVERIFIED), exit codes for compile errors and missing imports, whether a decoy `buf.work.yaml` above the temp folder is ignored, what buf writes to the cache folder, the sandbox behaviour, and the D1 timing gate. 6.6 lists the runs. |
| D9 | No buf: built-in fallback? (a) None: `.proto` changes are "not checked" with one note per session (kind `buf`), only when a `.proto` under `contracts.proto` changed; (b) a small built-in parser (messages, field numbers/names/types, services, RPCs) with its own items, like the OpenAPI parser. | **(a).** ROADMAP C5 says "keep the current parsers as a fallback", but there is no current proto parser, and a correct one (nested types, `oneof`, `map<>`, `extend`, options, editions, comments in odd places) is a sprint of its own, with item keys that would differ from buf's (Part A's "parser and tool waiver items differ" limit). Honest "unchecked" is the project rule (HANDOFF "Honesty"). (b) is a follow-up if users run without buf. |
| D10 | Run buf inside the OS sandbox? (a) Wrap with `os_sandbox.wrap(writable=[tmp], readonly=[])` when available, run unwrapped when not; (b) require the sandbox (no sandbox: not checked); (c) never wrap. | **(a).** The inputs are fully ours (explicit `#format=dir`, no `deps`, no `buf.lock`), so the source gives no path to the network (2.6); the sandbox is defence in depth against a bug or a newer buf, and it makes "writes only to the temp folder" true even if buf ignored `BUF_CACHE_DIR`. Requiring it (b) would leave every Linux machine without bubblewrap unchecked for no source-backed reason. Cost: one `sandbox-exec`/`bwrap` start per Stop (not per file). The cost is UNVERIFIED (D8 measures it). |
| D11 | Oldest buf accepted. | **1.32.0** (R1-1), read from `tools.json` `version` (the `--version` probe prints just `1.75.0`, 2.1; parsing rule in 5.2). Older versions are refused with a note (kind `buf-old`; findings kept, D6). Sourced reason: our `buf.yaml` is `version: v2`, which buf supports only from v1.32.0 (CHANGELOG), so an older buf cannot read our config at all. Correction: the v1.53.0 entry "Fix buf breaking annotations for JSON format" is commit e0ff84446e "Fix AddProtosourceAnnotation file name for breaking (#3771)", which swaps two arguments in `handleBreakingMessageSameJSONFormat`: it concerns the proto JSON-format rule `MESSAGE_SAME_JSON_FORMAT`, not `--error-format json` (reviewer, from the GitHub compare v1.52.1...v1.53.0; not re-read here). So it is no reason for a minimum. R2-3: at v1.52.0 `handleBreakingMessageSameJSONFormat` passes the format string where the file name belongs and `message.File().Path()` as the format (bufcheckserverhandle/breaking.go 1636-1644 at v1.52.0); the location is non-nil (`withBackupLocation(..., message.Location())`), and `AddProtosourceAnnotation` ignores the file-name argument when a location is given, so the **path is correct** but the **message** is the file path followed by Go's `%!(EXTRA ...)`. Result: in 1.52.0 and 1.52.1 only, that one rule gives an ordinary break with a garbled subject and item key (not a failed run); v1.51.0 has no file-name argument there (lines 1665-1672), and the enum twin was already right in 1.52.0 (reviewer, round 2; not re-read here). R2 optional (b), source for 1.32.0 (reviewer read v1.32.0): `breaking.go` 37-45 has `--error-format`, `--config`, `--against-config`, `--disable-symlinks`, line 157 `WithFileAnnotationsToStdout`; `buftarget/terminate.go` has the same `prefix == originalInputPath` rule; `buf_yaml_file.go` 362-365 defaults `modules` to `"."`; `print.go` 253-259 has the same JSON keys minus `plugin` and `policy` (which `parse_buf` already treats as optional). Also: v1.68.0 "Use new compiler for build process" and v1.68.1 "Revert the use of the new compiler report format" (CHANGELOG); the `COMPILE` and missing-import behaviour in 2.5 was read at v1.75.0 only, so for 1.32 to 1.67 it is UNVERIFIED (section 9); anything that does not match the 5.2 shapes ends as `failed`, never as a break. Alternative: 1.53.0, because in 1.52.x the `MESSAGE_SAME_JSON_FORMAT` message text, and so its item key, is garbled; not chosen (one rule, two patch releases), because keys across buf versions are not promised anyway (section 8). |

## 2. Sources and confirmed claims

"Inference" marks my own reasoning. Line numbers are at `v1.75.0` unless said otherwise.

### 2.1 Releases and distribution

- **Latest v1.75.0**, `published_at` 2026-10-22T01:02:29Z, not prerelease; v1.74.0 2026-10-14T17:36:02Z, v1.73.0
  2026-09-11T13:27:59Z, v1.72.0 2026-07-17T20:15:07Z —
  [GitHub releases API](https://api.github.com/repos/bufbuild/buf/releases?per_page=4). CHANGELOG: "## [v1.75.0] -
  2026-10-21" — [CHANGELOG.md](https://github.com/bufbuild/buf/blob/v1.75.0/CHANGELOG.md).
- Assets per release: raw binaries `buf-<OS>-<arch>` for Darwin arm64/x86_64, Linux aarch64/armv7/ppc64le/riscv64/s390x/
  x86_64, FreeBSD, OpenBSD, Windows (`.exe`), each also as `.tar.gz`/`.zip`, plus `protoc-gen-buf-breaking-*`,
  `protoc-gen-buf-lint-*`, `sha256.txt`, `sha256.txt.minisig`. Sizes: `buf-Darwin-arm64` 45,408,338 bytes,
  `buf-Darwin-arm64.tar.gz` 30,970,383, `buf-Linux-x86_64` 46,764,192, `buf-Linux-x86_64.tar.gz` 32,801,981 (releases API).
- Checksums ([sha256.txt](https://github.com/bufbuild/buf/releases/download/v1.75.0/sha256.txt)):
  `f71b009ebd8530b12fd85b060c6c245e15d6e5110ccc8da48d8b6fa71e59330b  buf-Darwin-arm64`,
  `fad1e38d6909bbb583a4da81d8c02a01046e2cd663bde7e337dd24277393764b  buf-Linux-x86_64`,
  `5478531c4645c5b80866e87d76d73f0e8bf5a35644b12c92cbdcc408e7c8b9d3  buf-Darwin-arm64.tar.gz`,
  `7168a06e29bd5eb8d1530848f242264f7a86b9b8af3b2e811570fbea0c929a30  buf-Linux-x86_64.tar.gz`.
- Install routes in the docs: `brew install bufbuild/buf/buf`, Scoop, WinGet, `npm install @bufbuild/buf`, release
  binaries at `https://github.com/bufbuild/buf/releases/download/v${VERSION}/buf-$(uname -s)-$(uname -m)`, `go install
  github.com/bufbuild/buf/cmd/buf@v1.75.0`, Docker `bufbuild/buf`; no PyPI package, no GitHub Action named on that page —
  [Buf CLI installation](https://buf.build/docs/cli/installation/).
- npm `@bufbuild/buf` 1.75.0, `bin` `buf`, `protoc-gen-buf-lint`, `protoc-gen-buf-breaking`, optional per-platform packages
  `@bufbuild/buf-darwin-arm64`, `-linux-x64`, ... — [npm registry](https://registry.npmjs.org/@bufbuild/buf/latest).
- Homebrew core formula `buf`: stable 1.75.0, bottle true — [formulae.brew.sh](https://formulae.brew.sh/api/formula/buf.json).
- PyPI: `bufbuild`, `buf-cli`, `bufbuild-buf` do not exist (404); **`buf` exists but is unrelated** (1.0.0, "For easily
  making chemical buffers and solutions", buf.readthedocs.io) — [PyPI JSON API](https://pypi.org/pypi/buf/json).
- GitHub Action `bufbuild/buf-action`, latest v1.7.0 (2026-11-03) —
  [releases API](https://api.github.com/repos/bufbuild/buf-action/releases?per_page=2). Not used by this plan.
- **Version text:** `const Version = "1.75.0"` —
  [private/buf/bufcli/bufcli.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufcli/bufcli.go) line 18;
  the root command sets `Version: bufcli.Version` ([cmd/buf/buf.go](https://github.com/bufbuild/buf/blob/v1.75.0/cmd/buf/buf.go)
  line 146) and app-go's `--version` writes `command.Version + "\n"` to stdout
  ([appcmd/appcmd.go](https://github.com/bufbuild/app-go/blob/v0.3.0/appcmd/appcmd.go) lines 347-362). So `buf --version`
  prints `1.75.0`, and `contract_tools.refresh`'s "last word of the first line" rule reads `1.75.0` (inference from
  source; not run).

### 2.2 The `buf breaking` command

From [cmd/buf/internal/command/breaking/breaking.go](https://github.com/bufbuild/buf/blob/v1.75.0/cmd/buf/internal/command/breaking/breaking.go):

- `Use: name + " <input> --against <against-input>"`, `Args: appcmd.MaximumNArgs(1)`; the input defaults to `"."`
  (`bufcli.GetInputValue(container, flags.InputHashtag, ".")`, line 176) (lines 65-71, 176).
- Flags (lines 45-56, 103-166): `--error-format` (default `text`; one of `text, json, msvs, junit, github-actions,
  gitlab-code-quality`, plus alias `gcc` — [bufanalysis.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufanalysis/bufanalysis.go)),
  `--exclude-imports`, `--path`, `--limit-to-input-files` ("Only run breaking checks against the files in the input ...
  Overrides --path"), `--config` ("The buf.yaml file or data to use for configuration"), `--against` ("Required, except if
  --against-registry is set. The source, module, or image to check against"), `--against-config`, `--against-registry`
  (network: BSR latest commit), `--exclude-path`, `--disable-symlinks`.
- **`--limit-to-input-files`** carries a source TODO: "this doesn't actually work because we're using the same file paths
  for both sides of the roots change" (lines 208-216). It also restricts the against side to the input's files, so a
  deleted file would not be compared (inference). Not used.
- `--against` and `--against-registry` together, or neither: error (`validateFlags`, lines 391-399).
- Both sides go through `controller.GetTargetImageWithConfigsAndCheckClient` with `bufctl.WithConfigOverride(flags.Config)`
  and `(flags.AgainstConfig)` respectively (lines 198-246). Image counts must match, else "input contained %d images,
  whereas against contained %d images" (lines 311-334, 484-490). One module per side (our layout) gives one image each.
- Annotations of all images are printed once with `bufanalysis.PrintFileAnnotationSet(container.Stdout(), ...)` and the
  command returns `bufctl.ErrFileAnnotation` (lines 367-377).
- **Input forms** (`buffetch/ref_parser.go` `processRawRef`, lines 550-620; `assumeModuleOrDir`, lines 836-853): format
  from the extension (`.bin`/`.binpb`/`.json`/`.txtpb`/`.yaml` images, `.tar`/`.zip`/`.gz`/`.zst`/`.tgz` archives,
  `.git` git, `.proto` a single file unless it is a directory); otherwise **a path that parses as a module reference and
  is not a directory is a BSR module** (network), else a directory. Docs: inputs take the form
  `path#option_key1=option_value1,...` and `format` sets the format explicitly; git examples `.git#branch=main`,
  `.git#ref=<sha>` — [Inputs reference](https://buf.build/docs/reference/inputs/). Hence `rev#format=dir` and
  `base#format=dir`.
- **`--config` / `--against-config` accept a file or inline data**: `GetBufYAMLFileForOverride` reads the file when
  `filepath.Ext(override)` is `.json`, `.yaml` or `.yml`, else parses the string itself as YAML/JSON —
  [bufconfig/buf_yaml_file.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufconfig/buf_yaml_file.go)
  lines 187-202. Inference: inline data containing a `.` in its last path element would be taken for a file name; the plan
  passes the relative file `lg-buf.yaml` instead, read relative to the cwd (`os.ReadFile`).
- v2 parsing is strict (`getUnmarshalStrict`), and **omitted `modules` means one module at `"."`** (same file, lines
  476-490). The docs call `modules` "Required" — [buf.yaml v2](https://buf.build/docs/configuration/v2/buf-yaml/); the
  source and docs disagree, and our file omits `modules` per the source (D8 confirms).
- `breaking` keys: `use`, `except`, `ignore`, `ignore_only`, `ignore_unstable_packages`, `disable_builtin`; "The `FILE`
  category is used if `breaking` is unset" — [buf.yaml v2](https://buf.build/docs/configuration/v2/buf-yaml/); the same set
  is compared in `equalBreakingConfig` (`breaking.go` lines 421-437).

### 2.3 JSON output

From [bufanalysis/print.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufanalysis/print.go),
[file_annotation.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufanalysis/file_annotation.go),
[file_annotation_set.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufanalysis/file_annotation_set.go),
[util.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufanalysis/util.go):

- `printAsJSON` uses `printEachAnnotationOnNewLine`: **one `json.Marshal` object per annotation, each followed by
  `\n`**; no surrounding array (print.go lines 46-52, 231-238, 409-426). No annotations: nothing is printed (inference:
  `breaking.go` prints only when `len(allFileAnnotations) > 0`).
- Object (`externalFileAnnotation`, print.go lines 296-324), keys in this order, **all `omitempty`**: `path` (string,
  `FileInfo().ExternalPath()`, empty → key absent), `start_line`, `start_column`, `end_line`, `end_column` (ints, each
  `atLeast1(...)`, so always present and ≥ 1; a missing position prints as 1), `type` (the rule ID, or `COMPILE`),
  `message`, `plugin`, `policy` (strings; empty unless a plugin or policy reported it). Go's `encoding/json` escapes
  `<`, `>`, `&` as `<` etc. (inference from Go's default `json.Marshal`; Python's `json.loads` reads them).
- Annotations are **deduplicated by their string form and sorted** by external path, start line, start column, type,
  message, end line, end column (file_annotation_set.go lines 52-90).
- Positions for check annotations are **1-based**: `startLine := fileLocation.StartLine() + 1`, columns likewise
  ([bufcheck/annotation.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufcheck/annotation.go)).
  Whether the column counts bytes, runes or tab stops is UNVERIFIED (the plan never uses columns).
- **No new-side file → no `path`:** when an annotation has no `FileLocation`, `annotationToFileAnnotation` builds it with
  `fileInfo nil` and positions 0 (printed as 1) (annotation.go). `FILE_NO_DELETE` is built with only
  `check.WithAgainstFileName(previousFilePath)` and the message `Previously present file %q was deleted.`
  ([bufcheckserverhandle/breaking.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufcheck/bufcheckserver/internal/bufcheckserverhandle/breaking.go)
  lines 110-135); the `PACKAGE_*_NO_DELETE` rules note "If the file does not exist, we don't know where the enum was
  deleted from" (lines 2065, 2126, 2184, 2292). Inference: these print without `path`.
- **R1-7: a `path` without a position also occurs.** `handleBreakingServiceNoDelete` passes `nil, // Deleted service does
  not have an input location` together with `file.Path()` (bufcheckserverhandle/breaking.go ~180-190), and
  `AddProtosourceAnnotationf` then sets only the file name (bufcheckserverutil/response_writer.go 63-73), so the line has
  `path` and all four positions print as 1 (`atLeast1`, print.go 315-318). `MESSAGE_NO_DELETE` can have a nil location
  too (`getDescriptorAndLocationForDeletedMessage`). (Confirmed by the reviewer's round-2 read of `handleBreakingServiceNoDelete` at v1.75.0 and `response_writer.go`
  53-81; the nil location for `MESSAGE_NO_DELETE` via `getDescriptorAndLocationForDeletedMessage` is inferred, UNVERIFIED.) So "no `path` ⇔ no
  new-side file" is incomplete: a line with `path` and positions 1/1/1/1 has no real position, and 5.3 gives it
  `line = None`. Cost: a real error at line 1 column 1 to line 1 column 1 also loses its line (acceptable; a
  one-character span at the file's first byte is not a declaration).
- **Columns (optional iv):** `COMPILE` columns at v1.75.0 count UTF-8 bytes (`length.Bytes` in build_image.go, read by the
  reviewer in round 2); check
  annotation columns remain UNVERIFIED. The plan uses no column.
- Message templates (same file): `Previously present message %q was deleted from file.` (161), `Previously present enum
  %q was deleted from file.` (64), `Previously present service %q was deleted from file.` (192), `Previously present %s
  was deleted%s.` with `field "<n>" with name "<name>" on message "<Name>"` (`fieldDescription`, breaking_util.go lines
  276-302; breaking.go 440-476), `%s changed type from %q to %q.` (873), `Previously present RPC %q on service %q was
  deleted.` (1449), `File %s changed from %q to %q.` (1351), `Previously present package %q was deleted.` (2248). Names
  are quoted; message names in field texts are the short `ParentMessage().Name()`. **No line numbers in messages.**
- `type` for build errors is `"COMPILE"` (2.5).
- For a directory input, the printed `path` is the file's *external path*. Whether it is `rev/acme/user/v1/user.proto`
  (input as given joined with the module path) or something else is **UNVERIFIED** (D8); the parser accepts the forms
  in 5.3 and rejects anything else.
- CHANGELOG: v1.53.0 "Fix buf breaking annotations for JSON format" (R1-1, R2-3: commit e0ff84446e swaps two arguments in
  the `MESSAGE_SAME_JSON_FORMAT` handler, whose message was garbled in 1.52.x; not the `--error-format json` printer); v1.52.0 "Add file path to `buf lint` and `buf breaking`
  output even when source code info is not available".

### 2.4 Config discovery (walk-up) and controlling workspaces

- For a directory input, `getReadWriteBucketForOS` opens the OS root as a bucket and calls `buftarget.NewBucketTargeting`
  with a terminate function ([buffetch/internal/reader.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/buffetch/internal/reader.go)
  lines 722-770). `mapControllingWorkspaceAndPath` runs the terminate function **from the input directory up through each
  parent to the root** ("we effectively do a do-while loop", `curDirPath = normalpath.Dir(curDirPath)`) —
  [buftarget/bucket_targeting.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/buftarget/bucket_targeting.go)
  lines 162-217.
- `terminateAtControllingWorkspace`: at each prefix it reads `buf.work.yaml` and `buf.yaml`; both in one directory is an
  error; **"A input directory with a v2 buf.yaml is the controlling workspace for itself"** (`prefix ==
  originalInputPath`); a v2 `buf.yaml` higher up controls the input when one of its `modules[].path` equals, contains or
  is contained in the input; a `buf.work.yaml` higher up controls it when one of its `directories` contains the input. A
  v1 `buf.yaml` is **not** a controlling workspace here, so the walk continues past it —
  [buftarget/terminate.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/buftarget/terminate.go).
- The docs say only that for `dir` inputs buf "looks at your current directory for a `buf.yaml` file" — [Inputs
  reference](https://buf.build/docs/reference/inputs/). The source is broader (walk to `/`).
- A config override does **not** skip the walk: `newWorkspaceTargeting` applies `overrideBufYAMLFile` to the bucket the
  targeting already chose ([bufworkspace/workspace_targeting.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufworkspace/workspace_targeting.go)
  lines 75-116; [workspace_provider.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufworkspace/workspace_provider.go)
  lines 263-285). Inference: a decoy `buf.work.yaml` with `directories: [lg-buf-x/rev]` in `$TMPDIR` would move the bucket
  root to `$TMPDIR` even with `--config`; our own v2 `buf.yaml` inside `rev/` and `base/` ends the walk at the first step.
- In v2, `buf.lock` belongs to the workspace root next to `buf.yaml` (inference from the docs' "resolution ... is stored in
  the buf.lock file"; not confirmed in source). We copy none.

### 2.5 Exit codes and error reporting

- `bufctl.ExitCodeFileAnnotation = 100` ("used when we print file annotations ... to distinguish user-parsable errors
  from system errors"), `ErrFileAnnotation = app.NewError(100, "")` —
  [bufctl/bufctl.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufctl/bufctl.go). CHANGELOG (old entry):
  "Exit with code 100 for `FileAnnotation` errors."
- **Compile errors** are a `FileAnnotationSet` of `type "COMPILE"` lines (only `report.Error` level; warnings suppressed)
  ([bufimage/build_image.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufimage/build_image.go) lines
  100-192). The controller prints any `FileAnnotationSet` error with the chosen `--error-format`, **to stdout** because
  breaking sets `WithFileAnnotationsToStdout()`, and turns it into `ErrFileAnnotation` (exit 100)
  ([bufctl/controller.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufctl/controller.go) lines 1366-1390;
  `breaking.go` lines 180-185). So a compile error on either side looks like a breaking result by exit code; only `type`
  tells them apart. The input side is built first, so a broken working tree ends the run before the baseline is built
  (`breaking.go` 198-229; inference).
- **A missing import** (`bufmodule.ImportNotExistError`) is wrapped with exit code 100 by the root error interceptor
  (`cmd/buf/buf.go` lines 591-594) but is a plain error, printed as text to stderr by the app (inference from app-go), with
  nothing on stdout. R1 optional (i), now sourced: `ImportNotExistError.Error()` writes `<external path>: import
  "<x>": <inner>`, the inner error being `fs.ErrNotExist` ("file does not exist") —
  [bufmodule/errors.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufmodule/errors.go) lines 43-70
  (read); the interceptor's fallback is `fmt.Errorf("Failure: %w", err)` (`buf.go` line 622, read). It is deliberately
  not a FileAnnotation (`bufmodule/module_dep.go` 193-199, whose comment says it is not a FileAnnotation and would print as 1:1; read by the
  reviewer in round 2). Expected stderr with our relative inputs:
  `Failure: rev/acme/user/v1/user.proto: import "third/x.proto": file does not exist` (the `rev/` form of the external
  path is inference, as in 2.3).
- **An empty module** (R1 optional ii) is an error, not a clean result: `buildImage` returns
  `bufmodule.ErrNoTargetProtoFiles` when no target files remain
  ([bufimage/build_image.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufimage/build_image.go)
  lines 79-84, read), text "no .proto files were targeted. This can occur if no .proto files are found in your input,
  ..." (errors.go line 40); `bufworkspace/workspace_targeting.go` returns it too (lines 271, 380). Exit code: 1
  (inference: a plain error). The plan never runs buf on an empty side (5.3).
- Other errors: app-go's `GetExitCode` returns the `appError` code, else **1**
  ([app-go app.go](https://github.com/bufbuild/app-go/blob/v0.3.0/app.go) lines 356-370). Usage errors: 1 (inference: a
  cobra argument error is not an `appError`); UNVERIFIED.
- Interceptor text for some errors: "it looks like you have found a bug in buf..." for syserrors (`buf.go` lines 581-589).

### 2.6 Environment, cache, network

- Cache, config and data folders: `<APPNAME>_CACHE_DIR` (`BUF_CACHE_DIR`), `BUF_CONFIG_DIR`, `BUF_DATA_DIR`, else
  `$XDG_CACHE_HOME/buf` (`$HOME/.cache/buf`), `$XDG_CONFIG_HOME/buf`, `$XDG_DATA_HOME/buf` —
  [app-go appext/name_container.go](https://github.com/bufbuild/app-go/blob/v0.3.0/appext/name_container.go) lines 78-122,
  [app_unix.go](https://github.com/bufbuild/app-go/blob/v0.3.0/app_unix.go) lines 58-119.
- **buf writes to the cache on every breaking run:** `NewWasmRuntime` creates `<cache>/v3/wasmruntime` (called
  unconditionally in `breaking.go` line 189), and the controller's `NewWKTStore` creates the WKT cache folder —
  [bufcli/cache.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufcli/cache.go) lines 205-245;
  [bufcli/controller.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufcli/controller.go) line 64.
- Variables read ([bufcli/env.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/buf/bufcli/env.go)):
  `BUF_INPUT_HTTPS_USERNAME`, `BUF_INPUT_HTTPS_PASSWORD`, `BUF_INPUT_SSH_KEY_FILE`, `BUF_INPUT_SSH_KNOWN_HOSTS_FILES`
  (remote inputs), `BUF_BETA_COPY_FILES_TO_MEMORY`, testing-only registry variables; HTTP auth also reads `~/.netrc`
  (`httpauth.NewNetrcAuthenticator`). v1.75.0 adds `BUF_DEBUG`, `BUF_LOG_FORMAT`, `BUF_TIMEOUT` as defaults for the
  global flags (CHANGELOG). `BUF_TOKEN` (BSR auth) is from the docs, not read here; inference: only used for BSR calls.
- **Network:** BSR calls come from `--against-registry` (breaking.go 252-310), module-reference inputs (2.2), and `deps`
  resolution (module data provider). `NewConnectClientConfig` is constructed on every run (line 247) but only used by the
  registry path (inference). No telemetry: no file in the v1.75.0 tree has `telemetry`, `analytics`, `otel` or `trace` in
  its path (tree listing via the GitHub trees API; 903 non-test `.go` files) — inference, not proof.
- Inference: with explicit `#format=dir` inputs, no `deps`, no `buf.lock`, and no `--against-registry`, the source shows
  no network path. UNVERIFIED by a run (D8 runs it inside the sandbox, which would show a blocked connection as an error).

### 2.7 In-file ignores and config-level ignores

- `ignoreFileLocation` applies `IgnoreRootPaths`, `IgnoreRuleIDToRootPaths`, `IgnoreUnstablePackages`, and comment
  ignores only when `config.AllowCommentIgnores && config.CommentIgnorePrefix != ""`, with the source comment **"Not a
  great design, but will never be triggered by breaking since this is never set"** —
  [bufcheck/client.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufcheck/client.go) lines 785-860.
  So `// buf:lint:ignore` and similar comments do not change `buf breaking` results; there is nothing to neutralise in
  the `.proto` text (unlike squawk). Config-level ignores live in `buf.yaml` only, which we write (D3).
- Annotations whose new **or** against location is ignored are dropped (`ignoreAnnotation`, same file 766-783).

### 2.8 Rules and categories (V2 spec)

From [bufcheckserver/bufcheckserver.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufcheck/bufcheckserver/bufcheckserver.go)
`V2Spec` (lines 284-414; `Build(<default>, <categories>)`) and IDs from
[bufcheckserverbuild.go](https://github.com/bufbuild/buf/blob/v1.75.0/private/bufpkg/bufcheck/bufcheckserver/internal/bufcheckserverbuild/bufcheckserverbuild.go).
Category purposes (lines 895-919): CSR "no Confluent Schema Registry (CSR) compatibility breaking changes"; FILE
"source-code breaking changes at the per-file level"; PACKAGE "... per-package level"; WIRE "wire breaking changes for the
binary encoding"; WIRE_JSON "... binary or JSON encodings". Docs: "the FILE and PACKAGE categories are supersets of the
WIRE_JSON category"; FILE is "**Default.**" — [Breaking rules and categories](https://buf.build/docs/breaking/rules/)
(the docs page lists four categories; `CSR` appears in source and in CHANGELOG v1.54.0 "Add `CSR` category").

| Category | Rules (rule IDs as printed in `type`) |
|---|---|
| FILE only (not PACKAGE) | `ENUM_NO_DELETE`, `EXTENSION_NO_DELETE`, `FILE_NO_DELETE`, `MESSAGE_NO_DELETE`, `SERVICE_NO_DELETE` |
| FILE and PACKAGE | `ENUM_SAME_TYPE`, `ENUM_VALUE_NO_DELETE`, `EXTENSION_MESSAGE_NO_DELETE`, `FIELD_NO_DELETE`, `FIELD_SAME_CARDINALITY`, `FIELD_SAME_CPP_STRING_TYPE`, `FIELD_SAME_JAVA_UTF8_VALIDATION`, `FIELD_SAME_JSTYPE`, `FIELD_SAME_TYPE`, `FIELD_SAME_UTF8_VALIDATION`, `FILE_SAME_CC_ENABLE_ARENAS`, `FILE_SAME_CC_GENERIC_SERVICES`, `FILE_SAME_CSHARP_NAMESPACE`, `FILE_SAME_GO_PACKAGE`, `FILE_SAME_JAVA_GENERIC_SERVICES`, `FILE_SAME_JAVA_MULTIPLE_FILES`, `FILE_SAME_JAVA_OUTER_CLASSNAME`, `FILE_SAME_JAVA_PACKAGE`, `FILE_SAME_OBJC_CLASS_PREFIX`, `FILE_SAME_OPTIMIZE_FOR`, `FILE_SAME_PHP_CLASS_PREFIX`, `FILE_SAME_PHP_METADATA_NAMESPACE`, `FILE_SAME_PHP_NAMESPACE`, `FILE_SAME_PY_GENERIC_SERVICES`, `FILE_SAME_RUBY_PACKAGE`, `FILE_SAME_SWIFT_PREFIX`, `FILE_SAME_SYNTAX`, `MESSAGE_NO_REMOVE_STANDARD_DESCRIPTOR_ACCESSOR`, `ONEOF_NO_DELETE`, `RPC_NO_DELETE` |
| FILE, PACKAGE, WIRE_JSON | `ENUM_SAME_JSON_FORMAT`, `ENUM_VALUE_SAME_NAME`, `FIELD_SAME_JSON_NAME`, `FIELD_SAME_NAME`, `MESSAGE_SAME_JSON_FORMAT` |
| FILE, PACKAGE, WIRE_JSON, WIRE | `FIELD_SAME_DEFAULT`, `FIELD_SAME_ONEOF`, `FILE_SAME_PACKAGE`, `MESSAGE_SAME_REQUIRED_FIELDS`, `RESERVED_ENUM_NO_DELETE`, `RESERVED_MESSAGE_NO_DELETE`, `RPC_SAME_CLIENT_STREAMING`, `RPC_SAME_IDEMPOTENCY_LEVEL`, `RPC_SAME_REQUEST_TYPE`, `RPC_SAME_RESPONSE_TYPE`, `RPC_SAME_SERVER_STREAMING` |
| PACKAGE only (not default) | `PACKAGE_ENUM_NO_DELETE`, `PACKAGE_EXTENSION_NO_DELETE`, `PACKAGE_MESSAGE_NO_DELETE`, `PACKAGE_NO_DELETE`, `PACKAGE_SERVICE_NO_DELETE` |
| WIRE_JSON (and CSR) only | `ENUM_VALUE_NO_DELETE_UNLESS_NAME_RESERVED`, `FIELD_NO_DELETE_UNLESS_NAME_RESERVED`, `FIELD_WIRE_JSON_COMPATIBLE_CARDINALITY`, `FIELD_WIRE_JSON_COMPATIBLE_TYPE` |
| WIRE_JSON, WIRE | `ENUM_VALUE_NO_DELETE_UNLESS_NUMBER_RESERVED`, `FIELD_NO_DELETE_UNLESS_NUMBER_RESERVED` |
| WIRE only | `FIELD_WIRE_COMPATIBLE_CARDINALITY`, `FIELD_WIRE_COMPATIBLE_TYPE` |
| no category | `MESSAGE_SAME_MESSAGE_SET_WIRE_FORMAT` (never run by a category `use`) |

The "default" flag (`Build(true, ...)`) marks the rules of the default `FILE` set. CSR membership is listed in the source
for many rules and omitted above. LegacyGuard keeps no rule table: buf runs only the chosen category's rules (D2).

### 2.9 This repository (file:line at 6060062)

- **No proto support.** `config.DEFAULTS["contracts"] = {}` (`legacyguard/guardlib/config.py` line 15); `contract_path`
  is generic (53-59). Kinds in use: `http`, `db`, `messages` (`indexer/contracts.py` 14-31; `checks.run_checks` 372-398).
  A `.proto` reaches `run_checks`' last branch and gets `[]` (398); README's config sample lists the same three keys
  (`legacyguard/README.md` lines 33-38). No code mentions `.proto`, protobuf, gRPC or buf (grep over `legacyguard/`).
- **The baseline is inside the project:** `baseline_dir(cfg) = cfg.state_dir / "baseline"`, i.e. `<root>/.legacyguard/
  baseline` (`guardlib/baseline.py` 42-43). A tool pointed at the project root would see every `.proto` twice.
  `changed_files` lists `git ls-files -co --exclude-standard` in both trees, drops `.legacyguard/`, filters by scope and
  compares bytes (108-124); deleted files are included. `read_baseline` reads text (127-132).
- `guardlib/contract_tools.py`: `TOOLS = ("oasdiff", "squawk")` (50); `settings` defaults (107-120); `_resolve`/`_entry`/
  `find`/`find_reason` (168-220); `refresh` probes every tool's `--version` through `os_sandbox.run`, 2 s, SessionStart
  with `reset_counters=True` (223-252; call sites `scripts/session_start.py` 120, `audit.run` 301, `ci.run` 137);
  `pre_edit_allowed`/`_bump_timeouts` take a tool name (255-268); `_run(cmd, timeout, cwd, env, input=None, text=True)` via
  `os_sandbox.run` (273-289); `Refusal` (81-87); `_sha12` (417-418); `take_notes` (757-770); `_squawk_part` (791-809);
  `report_line` (812-843).
- `guardlib/checks.py`: `Break(kind, item, subject, source, contract, line=None)` (15-25); `run_checks` per file
  (372-398).
- `guardlib/audit.py`: `_contract_findings(cfg, data, changed, drift, notes, oas, tool_state)` adds per-file breaks, then
  `_squawk_kept`, `_drift_findings`, `_oas_findings`, then the `not_applied` sweep over `contract_break` findings in
  `open`/`applied` not in `seen_items` (36-73); `_squawk_kept` (76-84); `_drift_findings` keeps a not-checked run's
  findings (87-110); `_drift` (205-221); `_report_notes` (287-291); `run` opens `scratch.for_trees`, runs drift, app
  OpenAPI, contracts, behaviour (294-312). `audit.run` runs **under the ledger lock** (`scripts/stop_audit.py` 15-16;
  `ledger.locked` takes a blocking `flock`, `guardlib/ledger.py` 44-56). Stop hook timeout 240 s, PreToolUse 10 s
  (`hooks/hooks.json`).
- `guardlib/ci.py`: `load_cfg` copies `scope, source_roots, django_settings, app, contracts` from `autosetup.detect`, but
  `detect` returns `openapi`, not `contracts` (`guardlib/autosetup.py` 60-77), so CI without a committed config has no
  contracts today (63-78; an existing gap, not fixed here beyond D5's key); `run`: `contract_files` = every changed
  non-`.py` file, so `.proto` files are in it (154); `tool_state` per run (160); `_drift`/`_app_openapi` after the loop
  (186-187); `annotations` (`django-app:`/`app:` pseudo-files without `file=`, `line=` when `b.line`) (249-271);
  `exit_code` counts breaks only under `fail_on: any` (350-357).
- `guardlib/ledger.py` 22 `SOURCES`; `add_finding` identity `kind|file|item|code_hash` (59-75).
- `guardlib/waivers.py` 21-23: a waiver holds while `code_hash` matches.
- `guardlib/report.py` 160-170 `tools_line` (shown when `contracts.http`/`db`, drift, notes or app OpenAPI is present).
- `runner/os_sandbox.py`: `wrap(cmd, python, writable, readonly, mode)` (85-97; macOS profile: allow default, deny
  outbound network and every write except `writable`, 33-44; Linux bwrap `--unshare-net`, read-only root, 47-54);
  `active` (80-82); `run(cmd, timeout, *, input, capture_output, text, cwd, env)`: own session, `killpg` SIGKILL and reap
  on timeout or any exception (129-152).
- `scripts/pre_edit.py` 88-89 denies Edit/Write under `.legacyguard/`.
- Fakes: `tests/oasdiff_fake.py` and `tests/squawk_fake.py` handle `--version` first and log it only to `versions.log`.

## 3. Security

- **What buf reads:** the two input folders we build, our `lg-buf.yaml`, and, by its walk-up, any `buf.yaml`/
  `buf.work.yaml` between the input and `/`. Our own v2 `buf.yaml` in `rev/` and `base/` makes each input its own
  controlling workspace at the first step of the walk (2.4), so no parent file is consulted; `--config lg-buf.yaml` and
  `--against-config lg-buf.yaml` fix the rule set besides. Nothing from the project except `.proto` bytes is copied: no
  `buf.yaml`, `buf.work.yaml`, `buf.lock`, `buf.gen.yaml`, `buf.policy.yaml`, no README/LICENSE.
- **What it is given:** a fresh `tempfile.mkdtemp(prefix="lg-buf-")` with `base/` and `rev/`; relative inputs
  `rev#format=dir`, `base#format=dir`; cwd = the temp folder, so `$TMPDIR` never appears in argv and printed paths start
  with `rev/` or `base/` (UNVERIFIED form, D8). Text returned to the user never contains the temp path: stderr lines are
  stripped of it before use.
- **File set:** from `git ls-files -co --exclude-standard` in each tree (the listing `changed_files` already trusts), names
  ending `.proto` under `contracts.proto`, `.legacyguard/` excluded, `..` or absolute names refused. A symlink whose
  `realpath` stays inside that tree is copied as its target's bytes; one that leaves the tree refuses the whole run (kind
  `refused`), so a symlink can never make buf read outside the tree and a skipped file can never look like a deleted one.
  `--disable-symlinks` is passed too (our copies have none). Caps: `MAX_PROTO_FILES = 2000`, `MAX_PROTO_BYTES = 32 MB` per
  side; over either: kind `too_large`.
- **Bytes, not text:** files are copied with `read_bytes`/`write_bytes` (no newline translation, BOM kept), so buf's line
  numbers refer to the file as it is on disk. No offsets are computed on our side; item keys never contain lines.
- **Environment:** `os.environ` minus every `BUF_*` variable (tokens, `BUF_INPUT_*`, `BUF_DEBUG`, `BUF_LOG_FORMAT`,
  `BUF_TIMEOUT`, `BUF_BETA_*`), plus `BUF_CACHE_DIR=<tmp>/cache`, `BUF_CONFIG_DIR=<tmp>/config`, `BUF_DATA_DIR=<tmp>/data`,
  `HOME=<tmp>/home` (so `~/.netrc` and `~/.cache` are not the user's), all created empty. A fresh cache per run: no state
  carries over between runs, and nothing the project can write is ever read as cache. Cost: re-creating the WKT and Wasm
  cache folders each run (UNVERIFIED, measured in D8).
- **stdin:** `input=b""`.
- **OS sandbox (D10):** `os_sandbox.wrap(cmd, sys.executable, writable=[tmp], readonly=[], mode=behavioral_diff.os_sandbox)`
  when `os_sandbox.active(...)`; unwrapped otherwise, with the label kept for the report. The sandbox makes "no network,
  writes only to the temp folder" hold even if a future buf ignored `BUF_CACHE_DIR`.
- **Binary location:** as for oasdiff and squawk, `auto` takes `buf` from `PATH`; a project-local `PATH` folder (e.g.
  `./node_modules/.bin`, which `npm install @bufbuild/buf` creates) lets the project supply the binary. With no
  fallback (D9), such a binary could hide buf breaks. Same follow-up as Part C (refuse a resolved path inside `cfg.root`),
  listed in section 8, not built here.
- **Process control:** `_run` → `os_sandbox.run` (process-group SIGKILL and reap on timeout or any exception).
- **The `--version` probe (R1 optional vi):** `contract_tools.refresh` runs `buf --version` through `os_sandbox.run` with
  a 2 s cap, **unsandboxed, with the full environment and the hook's cwd**, exactly like the oasdiff and squawk probes.
  It runs only when `contracts.proto` is set, once per SessionStart and again only when the binary's stat changes. From
  source, `--version` is handled by app-go before any command runs (appcmd.go 347-362), so it reads no input; that is
  inference, not a run. Wrapping the probe is a follow-up for all three tools, not done here.

## 4. Current code paths that change

| File | Change |
|---|---|
| `guardlib/contract_tools.py` | `TOOLS += ("buf",)`; `settings` defaults `buf: "auto"`, `buf_category: "FILE"`; `refresh` skips `buf` (no `--version` probe) unless `cfg.contract_path("proto")` is set; new `buf_breaking`, `parse_buf`, `BufResult`, `buf_category`, `BUF_MIN_VERSION = (1, 53, 0)`, `_buf_part` in `report_line`. |
| `guardlib/proto_contract.py` (new) | `applies(cfg, changed)`, `collect(tree, folder, listing)`, `compute(cfg, base_dir, root, timeout) -> ProtoOutcome`, `breaks_from(result, ...)`, `item_key`, `code_hash`, notes and texts. Guard side only; no project code runs. |
| `guardlib/checks.py` | none (`Break` already has `line`; `kind="proto"`). |
| `guardlib/audit.py` | `_proto(cfg, data, changed, log)` (after `_drift`), `_contract_findings(..., proto=None)` → new `_buf_findings` before the sweep; `_report_notes` takes the proto notes. |
| `guardlib/ci.py` | `_buf(cfg, out, base_dir, root, files)` after `_drift`; `annotations`: a break whose `rel` equals `contracts.proto` (path-less) is annotated without `file=`; `load_cfg` maps `detect()["proto"]` into `contracts.proto` (D5). |
| `guardlib/autosetup.py` | `detect` gains `proto` (D5: one `buf.yaml`, v1 or single-module v2); `render` writes it under `contracts:`. |
| `guardlib/ledger.py` | `SOURCES += ("buf",)`. |
| `guardlib/config.py` | `contract_tools` defaults gain `buf: auto` and `buf_category: FILE` (comment). |
| `guardlib/report.py` | `tools_line` also when `contracts.proto` is set. |
| `legacyguard/README.md` | `contracts.proto` in the config sample; one line on buf (the coordinator may move this to docs). |
| `tests/buf_fake.py`, `tests/test_buf.py` | New (section 6). |

Not changed: `scripts/pre_edit.py`, `scripts/post_edit.py` (D1 b), the runner, `indexer/contracts.py` and the MCP
`contract` tool (no proto items are indexed; a follow-up).

## 5. Build plan

### 5.1 Config

- `contracts.proto: <folder>`, relative to `scope` like the other keys (`Config.contract_path("proto")`, which
  normalises, so `proto/` → `proto` and `.` → `.`). Must name a folder. Refused (state `not_checked`, kind `config`,
  findings kept, R1-6; one note kind `buf-folder`, R1 optional v):
  - a value ending `.proto`, `.yaml` or `.yml`: "contracts.proto must name the folder your .proto imports are relative
    to, e.g. proto/; .proto changes were not checked.";
  - a value that normalises to `..` or starts with `../`, or is absolute (R1-2): "contracts.proto must be a folder inside
    the repository; .proto changes were not checked.";
  - a folder outside `scope` (R1-3; `paths.in_scope(folder + "/x.proto", cfg.scope)` is False): "contracts.proto
    (<folder>) is outside scope (<scope>), so LegacyGuard never sees changes there; .proto changes were not checked."
    Because `changed_files` never lists such files, this case (like the two above) is decided in `_proto` from the config
    alone, on **every** Stop whatever `changed` holds, before `applies` (R2-1, order in 5.4); the note is shown once per
    session (`take_notes`).
  A folder that exists in neither tree: nothing runs, no note.
- **Folder `.` (R1-2):** `Config.contract_path` returns `"."`, `git ls-files` never prints `./`, and `checks.is_under(rel,
  ".")` is False (checks.py 34-35). So `proto_contract` turns the folder into a **prefix**: `prefix = ""` when the folder
  is `"."`, else `folder + "/"`. Every rule uses the prefix and nothing else: `applies` (`rel.startswith(prefix)`),
  `collect` (module path `m = name[len(prefix):]`), the repo path `rel = prefix + m` (so `acme/x.proto`, never
  `./acme/x.proto`), the item key, and the ledger `file` for path-less rules (the folder itself, written `.` for the root).
- `contract_tools.buf: auto | false | <path>` (resolution, cache in `tools.json`, stat check exactly as for oasdiff and
  squawk).
- `contract_tools.buf_category`: a string in `{"FILE", "PACKAGE", "WIRE_JSON", "WIRE"}` after `.strip().upper()`; anything
  else (including `CSR`, lists, ints) → `FILE` with one note (kind `buf-category`, its own kind so it never hides a
  `buf-folder` note, R1 optional v: "contract_tools.buf_category must be one of FILE, PACKAGE, WIRE_JSON, WIRE; buf used
  FILE.").
- `timeout_s` (20) is shared with the other tools.

### 5.2 `contract_tools.buf_breaking(cfg, base_files, rev_files, category, timeout) -> BufResult | Refusal`

`base_files`/`rev_files`: `{module path: bytes}` from `proto_contract.collect`. Never raises. Steps:

1. `find("buf", cfg)`; none → `Refusal("", "off")` (switched off) or `missing` ("buf was not found, so .proto changes
   were not checked. Install buf (https://buf.build/docs/cli/installation/) or set contract_tools.buf in
   .legacyguard/config.yaml."), or for a bad configured path "buf configured path <value> not found or not executable, so
   .proto changes were not checked." Version known and `< BUF_MIN_VERSION` (= `(1, 32, 0)`, R1-1) → `Refusal("buf <v> is
   older than 1.32.0, the first version that reads a version v2 buf.yaml, which LegacyGuard writes for the check; .proto
   changes were not checked.", "old")`.
   **Version parsing (R1-10):** `refresh` already stores the last word of the first `--version` line. `buf_version(word)`
   applies `re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)", word)` (ASCII digits only, anchored at the start by `re.match`, a
   suffix such as `-dev` allowed) and returns the tuple of ints, else `None`. `None` (garbage, empty, no probe) is
   **tried**: buf runs and any shape mismatch ends as `failed`. So `1.75.0` → `(1, 75, 0)`, `1.75.1-dev` → `(1, 75, 1)`,
   `1.40.0` → `(1, 40, 0)` (accepted; refused only below 1.32.0), `1.31.9` → refused, `buf` / `v1.75.0` / `１.７５.０`
   (full-width) → `None`.
2. `tmp = tempfile.mkdtemp(prefix="lg-buf-")`; create `base/`, `rev/`, `cache/`, `config/`, `data/`, `home/`. Write each
   file under its side (`os.makedirs(parent)`, `write_bytes`). Write the same text to `base/buf.yaml`, `rev/buf.yaml`,
   `lg-buf.yaml`:
   ```yaml
   version: v2
   breaking:
     use:
       - FILE
   ```
   (the chosen category). **R1-8:** a folder component can carry one of buf's names (`proto/buf.yaml/x.proto`), which
   would collide with our `rev/buf.yaml` or be read at the walk's first step (`terminateAtControllingWorkspace`,
   terminate.go 66-79). `collect` therefore refuses the run (kind `refused`, findings kept, note kind `buf-failed`:
   "buf was not run: <repo path> has a folder named <name>, which buf would read as its own config; .proto changes were
   not checked.") when **any** component of a module path, other than the final `*.proto` name, is `buf.yaml`,
   `buf.work.yaml` or `buf.lock` (case-sensitive, as buf reads them). Any component, not only the first: a deeper one does
   no harm by the source read, but the check is cheaper than proving that, and such names are never legitimate folders.
3. Command: `[path, "breaking", "rev#format=dir", "--against", "base#format=dir", "--error-format", "json", "--config",
   "lg-buf.yaml", "--against-config", "lg-buf.yaml", "--disable-symlinks"]`. Never `--against-registry`, `--path`,
   `--limit-to-input-files`, `--exclude-path`, `--exclude-imports`, `--debug`, `--timeout`.
4. Env per section 3. Sandbox per D10: `cmd = os_sandbox.wrap(cmd, sys.executable, writable=[tmp], readonly=[], mode)`
   when `os_sandbox.active(sys.executable, mode)`; record `sandboxed: bool` in `tools.json` `buf`.
5. `done, reason, timed_out = _run(cmd, timeout, tmp, env, input=b"", text=False)`; update `tools.json` `buf.last_seconds`,
   `last_error`. Timeout → `Refusal(f"buf took longer than {timeout:g} s; .proto changes were not checked.", "timeout")`;
   start failure → kind `unstartable`.
6. `lines = parse_buf(done.stdout)`; `None` → `failed` ("buf printed output LegacyGuard could not read"), or with a
   non-zero exit and an empty stdout, the first stderr line with `tmp + os.sep` removed: `Refusal(f"buf could not check
   the .proto files: {line[:200]}; nothing was reported.", "failed")`; when that stderr line (after an optional
   `Failure: ` prefix) matches `^(?:Failure: )?(rev|base)/(\S+?\.proto): import "([^"]+)": ` (the sourced
   `ImportNotExistError` text, 2.5) the kind is `imports`, and the note maps the path to the repo path: "buf could not
   find import \"<x>\" in <repo path>; LegacyGuard gives buf only the files under contracts.proto and fetches no
   dependencies, so .proto changes were not checked."
7. Consistency: exit 0 with lines, exit 100 with no lines and no stderr, or any other exit code with lines → `failed`
   ("buf exited with code N"). Exit 100 with no lines and stderr → step 6.
8. Any line with `type == "COMPILE"` → `Refusal(f"buf could not compile {side} ({rel}:{start_line}: {message[:120]});
   nothing was reported.", "compile")`, side "the new .proto files" / "the baseline .proto files" by the path prefix
   (5.3), `rel` the repo path.
9. Return `BufResult(lines, seconds, sandboxed)`. `finally`: `shutil.rmtree(tmp, ignore_errors=True)`.

`parse_buf(stdout) -> list[dict] | None`: split on `\n`, skip empty lines; each must `json.loads` to a dict with
`type` (non-empty str) and `message` (str); `path` absent or str; `start_line`, `start_column`, `end_line`, `end_column`
absent or int (not bool) ≥ 1; `plugin`, `policy` absent or str; other keys ignored (forward compatible). Any line that
fails → `None` (the whole run is rejected).

### 5.3 `proto_contract`: files, paths, items

- `collect(tree, folder, files)`: `files` from `git -C <tree> ls-files -co --exclude-standard -z` (one call per tree,
  timeout 30 s), keep names starting with the prefix (5.1: `""` for `.`, else `folder + "/"`) and ending `.proto`, not
  under `.legacyguard/`; module path = name minus the prefix. Read bytes (symlink rule, caps: section 3; the R1-8
  component rule, 5.2). Returns `({module path: bytes}, refusal or None)`.
- `applies(cfg, changed)`: some `rel` in `changed` starts with the prefix and ends `.proto` (a deletion counts).
- **Unset key (R1-5):** when `contracts.proto` is unset, `_proto` checks whether some `rel` in `changed` (already
  scope-filtered) ends `.proto`; if so, one note, kind `buf-unset`, at most once per session (`take_notes`), and no run:
  "A .proto file changed (<first such rel>), but contracts.proto is not set in .legacyguard/config.yaml, so .proto
  changes were not checked. Set it to the folder your .proto imports are relative to." The return value stays `None`
  (no findings can exist without the key, except after a mid-session removal, which D6 (b) sweeps). The note is given
  whether or not buf is installed.
- **Printed path → side and repo path:** accept `rev/<m>` (new side) and `base/<m>` (baseline side, only for `COMPILE`),
  also an absolute path under `os.path.realpath(tmp)` + `/rev/` or `/base/` (stripped), and a bare `<m>` that is a key of
  `rev_files` (new side). Any other `path` → the run is `failed` ("buf reported a file LegacyGuard did not give it"). Repo
  path `rel = prefix + m` (5.1; `m` itself for the root folder).
- **No `path`:** for `FILE_NO_DELETE`, take the quoted name from `Previously present file "<m>" was deleted.` (a JSON-style
  quoted string; `json.loads` on the quoted part) and use `rel = prefix + <m>` when `<m>` is a key of `base_files`; other
  path-less lines (`PACKAGE_*`) use `rel = "-"` and the ledger `file` is the folder itself (`.` for the root).
- **Item key:** `T = type`, `M = " ".join(message.split())`; `item = f"buf:{T}:{rel}:{M[:80]}:{_sha12(T + '\0' + rel +
  '\0' + M)}"`. Identical `(T, rel, M)` twice in one run → one item (first line). Keys contain no line numbers or columns.
- **Break:** `Break("proto", item, subject, "buf", file, line=L)` where (R1-7) `L = start_line` only when the line had a
  `path` **and** not all four of `start_line`, `start_column`, `end_line`, `end_column` are 1; else `L = None` (no
  invented `line=1` in CI annotations; `SERVICE_NO_DELETE` and some `MESSAGE_NO_DELETE` lines print 1/1/1/1). With
  `subject = f"{M} (buf {T})"` and `file = rel` (or the folder for `rel == "-"`).
- **Code hash (waivers):** `symbols.sha(<working-tree text of rel>)` when that file exists, else `symbols.sha(item)` (a
  deleted file or a package rule). R1-4: the Stop lookup of a known finding stays `(file, item)`, as for every other
  source (audit.py 55-59, 98-103), and `/legacyguard:waive` sets the finding's state to `waived` at once
  (`scripts/lg_more.py` 43-44). So within a session a waived buf finding stays `waived` after later edits of the file
  (it is the same finding); the code hash matters only for a **new** finding (a new session, a finding swept and
  re-created is not one: a `not_applied` finding found again goes back to `open`, never re-checked against waivers) and
  for CI (`waivers.is_waived` per break). Chosen over matching `code_hash` in the lookup because that would make every
  edit of a file with a standing break a new open finding that blocks the Stop again, and would make buf behave unlike
  squawk, drift and the static checks.
- **Empty sides:** no `.proto` in the baseline → `ProtoOutcome("skip")` (nothing can break; no note). Files in the
  baseline and none in the working tree: with category `FILE`, one synthesised `FILE_NO_DELETE` break per baseline file
  with buf's own message text (`Previously present file "<m>" was deleted.`), so the key equals what buf prints for a
  single deleted file (test 16); other categories → not checked, kind `empty`, note. buf is not run in either case:
  an empty side is a sourced error, `ErrNoTargetProtoFiles` (2.5, R1 optional ii), not a clean result.
- `ProtoOutcome(state="checked"|"not_checked"|"skip", kind=None, breaks=[(file, Break)], notes=[(kind, text)])`.
- `compute(cfg, base_dir, root, timeout)`: settings, `collect` both trees, the empty-side rules, `buf_breaking`, mapping.
  Wrapped in `try/except Exception` → `not_checked`, kind `failed`, note "the .proto check failed inside LegacyGuard".

Note kinds (each once per session through `take_notes`): `buf` (missing), `buf-old`, `buf-failed` (failed, unstartable,
bad output, refused, too_large), `buf-timeout`, `buf-compile`, `buf-imports`, `buf-folder` (invalid or out-of-scope
`contracts.proto`, outcome kind `config`), `buf-category` (bad `buf_category`; the run goes on with FILE), `buf-unset`
(a changed `.proto` with the key unset), `buf-empty`. Kind `off` adds none. (R1 optional v: `buf-config` is split into
`buf-folder` and `buf-category`.)

### 5.4 Hooks

- **PreToolUse, PostToolUse:** no change (D1 b).
- **Stop** (`audit.run`): `proto = _proto(cfg, data, changed, log)` after `_drift`. R2-1, in this order:
  1. `contracts.proto` unset: the `buf-unset` check (5.3), then return `None`.
  2. Invalid or outside scope (5.1): return `ProtoOutcome("not_checked", kind="config")` with the `buf-folder` note
     (shown once per session through `take_notes`). This runs on **every** Stop, whatever `changed` holds, so open buf
     findings are kept even when no `.proto` changed (an invalid prefix such as `proto/user.proto/` would make `applies`
     False and sweep them otherwise).
  3. `applies(cfg, changed)` False: return `None` (the sweep closes buf findings: the module equals the baseline).
  4. Otherwise `proto_contract.compute(cfg, str(baseline.baseline_dir(cfg)), cfg.root, settings["timeout_s"])`, and `data["contract_tools"]["buf"] = {"runs": n + 1, "last": state, "kind": kind}`.
  `_contract_findings(..., proto=proto)` calls `_buf_findings(cfg, data, proto, seen_items)` after `_squawk_kept` and before
  the sweep:
  - `proto is None` → nothing (the sweep closes stale buf findings: no `.proto` differs from the baseline, or, R1-6 (b),
    `contracts.proto` was removed mid-session; the report then says "buf not run: contracts.proto not set" when the
    ledger holds any `buf` finding).
  - `state == "not_checked"` and `kind == "config"` (R1-6 (a): invalid or out-of-scope `contracts.proto`) → kept, like
    every other unchecked kind below.
  - `state == "not_checked"` and `kind != "off"` → add `(f["file"], f["item"])` of every `contract_break` finding with
    `source == "buf"` in `open`/`applied` to `seen_items` (kept, D6). `kind == "off"` → nothing (swept). `skip` → nothing.
  - `checked` → for each `(file, b)`: `seen_items.add((file, b.item))`; a known finding in `not_applied` goes back to
    `open`; otherwise `ledger.add_finding(kind="contract_break", status=FAILURE, source="buf", subject=b.subject,
    file=file, evidence=f"{b.text()}; found by buf breaking ({category}) against the session baseline", item=b.item,
    code_hash=proto_contract.code_hash(...), state="waived" if waived else "open", extra={"contract_kind": "proto"})`.
  - **Dedupe:** there are no other proto items in LegacyGuard (2.9), so none to drop. A `.proto` file under `contracts.db`
    or `contracts.messages` would also reach `run_checks`' other branches; `check_migration`'s regexes cannot match proto
    syntax and the messages branch takes `.json` only, so no double report. Inside one run, buf deduplicates identical
    annotations (2.3) and `_buf_findings` deduplicates equal items.
  - Notes go through `_report_notes` (proto notes added like `oas_notes`).
- **Time under the ledger lock:** one `git ls-files` per tree (30 s cap each, in practice milliseconds), copying at most
  2 × 32 MB, and **one** buf process per Stop (≤ `timeout_s` 20 s plus up to 5 s of reap). There is no per-file loop, so no
  separate time rule is needed. `refresh(reset_counters=False)` at Stop does not re-probe an unchanged binary (existing
  rule).
- **CI** (`ci.run`): `_buf(cfg, out, str(base_dir), root, changed + contract_files)` after `_drift`, same `compute`; breaks
  after `waivers.is_waived(cfg, "contract_break", file, b.item, code_hash)`; notes in `out.notes`; annotations
  `::warning file=<rel>,line=<n>,title=LegacyGuard contract::...` (path-less: no `file=`). Counts toward failure only under
  `fail_on: any`, as every contract break.
- **SessionStart:** `refresh(reset_counters=True)` probes `buf --version` only when `contracts.proto` is set (2 s cap).
- **Ledger and report:** `SOURCES += ("buf",)`. `report_line` part (only when `contracts.proto` is set): `buf <version>
  (<path>), last run <s> s[, no OS sandbox]` / `last run failed (...)` / `buf switched off` / `buf not found` / `buf <v> too
  old`; plus `, category <C>` when not `FILE`. R1-6: the part is also shown when `contracts.proto` is unset but the
  ledger holds a `buf` finding, as "buf not run: contracts.proto not set"; and for kind `config` as "buf not run:
  contracts.proto is invalid (<reason>)". `report.tools_line`'s condition gains both cases.

### 5.5 Fail-open

Every failure path leaves no break and at most one note per kind per session; `compute` never raises into the hook. The
Stop audit's other checks run unchanged. No partial results: a run with a compile error on either side reports nothing,
because a breaking check on a module that does not build is not defined.

## 6. Tests (run one file at a time, never in parallel)

### 6.1 `tests/buf_fake.py`

A Python script at `<base>/bin/buf`, like `tests/squawk_fake.py`:

- `--version` is handled **first**: append a line to `<base>/versions.log`, print `1.75.0`, exit 0; it never writes
  `calls.jsonl` (so `refresh()` at SessionStart, Stop and `ci.run` never counts as a run).
- Every other call appends to `<base>/calls.jsonl`: `argv`, `cwd`, `env` (sorted names starting with `BUF_`, plus the
  values of `BUF_CACHE_DIR`, `BUF_CONFIG_DIR`, `BUF_DATA_DIR`, `HOME`), `listing` (every file under the cwd, relative),
  `configs` (text of `lg-buf.yaml`, `rev/buf.yaml`, `base/buf.yaml`), `stdin` (bytes read, expected empty).
- Mode `breaking` (default): requires argv to start with `breaking rev#format=dir --against base#format=dir` (else stderr
  "unexpected arguments", exit 1). Compares `base/` and `rev/` with stand-in patterns: a file in base missing in rev →
  `FILE_NO_DELETE` without `path`; `message <Name> {` missing → `MESSAGE_NO_DELETE` (path of the rev file); a field line
  `<type> <name> = <n>;` whose number is gone → `FIELD_NO_DELETE` ("Previously present field \"<n>\" with name \"<name>\"
  on message \"<Name>\" was deleted."); same number, other type → `FIELD_SAME_TYPE` ("Field \"<n>\" with name \"<name>\" on
  message \"<Name>\" changed type from \"<a>\" to \"<b>\"."); same number, other name → `FIELD_SAME_NAME`; `rpc <Name>(`
  gone → `RPC_NO_DELETE`; `service <Name> {` gone → `SERVICE_NO_DELETE` **with** `path` (the rev file) and all four
  positions 1 (R1-7, buf's shape for a deleted service). Each line is the v1.75.0 shape (keys in order `path`, `start_line`, `start_column`, `end_line`,
  `end_column`, `type`, `message`, omitted when empty, positions ≥ 1), sorted as buf sorts; exit 100 if any, else no output
  and exit 0. The category in `lg-buf.yaml` filters: under `WIRE_JSON` the fake drops `MESSAGE_NO_DELETE` and
  `FILE_NO_DELETE` (stand-in for buf's category sets).
- Tokens: `COMPILEERR` in a rev (or base) file → only a `COMPILE` line with `path` `rev/<m>` (or `base/<m>`), exit 100;
  `IMPORTMISSING` → stderr `Failure: rev/<m>: import "third/x.proto": file does not exist` (the sourced shape, 2.5),
  empty stdout, exit 100.
- Failure modes: `garbage` (prints `not json`, exit 100), `array` (prints one JSON array of the objects: the wrong shape),
  `rc1` (stderr "Failure: something", exit 1), `rc0-nonempty`, `rc100-empty` (no stdout, no stderr), `bad-field`
  (`start_line: "1"`), `zero-line` (`start_line: 0`), `abs-path` (absolute `path` under the cwd), `foreign-path`
  (`path: "other/x.proto"`), `plugin` (adds `"plugin": "p"`; accepted), `slow` (sleep 3 s, then `breaking`), `hang-child`
  (forks a sleeping child, then sleeps), `probe` (tries to write `<project root>/probe.txt` and to connect to a listening
  `127.0.0.1` port given in `<base>/port`, records `write: ok|denied` and `connect: ok|denied`, then behaves as
  `breaking`), `old-version` (`--version` prints `1.31.0`), `dev-version` (`1.75.1-dev`), `garbage-version` (`buf`).
- Docstring: shape pinned from `bufanalysis/print.go` `externalFileAnnotation` and the message templates in
  `bufcheckserverhandle/breaking.go` at v1.75.0; the patterns are a stand-in, not buf's rules; replace the "UNVERIFIED"
  notes with what the real binary printed after D8.

### 6.2 `tests/test_buf.py`, unit (fake or no binary)

1. `parse_buf` accepts hand-written lines in the 2.3 shape (with and without `path`, with `plugin`), returns dicts; rejects
   a JSON array, a non-dict line, missing `type`, `start_line: "1"`, `start_line: 0`, `start_line: true`, `message: null`;
   ignores an unknown extra key.
2. Item keys: `FIELD_NO_DELETE` on `proto/acme/user/v1/user.proto` gives `buf:FIELD_NO_DELETE:proto/acme/user/v1/user.proto:
   Previously present field "2" with name "email" on message "User" was deleted.:<sha12>` (first 80 characters of the
   message; sha over `T\0rel\0M`); the same message on another file is another key; whitespace differences in the message
   give the same key; a message longer than 80 characters with a shared prefix gives two keys.
3. `collect`: only `*.proto` under the folder; `buf.yaml`, `buf.lock`, `buf.work.yaml`, `.txt` and files under
   `.legacyguard/` never collected; a git-ignored `.proto` not collected; an untracked one collected; a symlink to a file
   inside the tree collected as bytes; a symlink leaving the tree → refusal; 2001 files or > 32 MB → `too_large`.
   R1-8: `proto/buf.yaml/x.proto`, `proto/acme/buf.lock/y.proto` and `proto/buf.work.yaml/z.proto` each → kind `refused`,
   note names the repo path and the folder name, fake not called, an open buf finding stays open.
4. Bytes: a file with CRLF line ends and a leading BOM is copied byte for byte (the fake's listing shows the same bytes).
5. `buf_category`: `"file"` → `FILE`; `"WIRE_JSON"` kept; `"CSR"`, `["FILE"]`, `3`, `None`-but-set → `FILE` with note kind
   `buf-category`; a bad category and a bad folder in one session give both notes (separate kinds, R1 optional v).

### 6.3 Isolation and command (fake binary)

6. argv exactly `["breaking", "rev#format=dir", "--against", "base#format=dir", "--error-format", "json", "--config",
   "lg-buf.yaml", "--against-config", "lg-buf.yaml", "--disable-symlinks"]` (after any sandbox prefix, which the test
   strips by finding the fake's path in the recorded argv); cwd a folder named `lg-buf-*`; `lg-buf.yaml`, `rev/buf.yaml`,
   `base/buf.yaml` identical, `version: v2`, `use: [FILE]`, no `deps`, no `modules`; stdin empty; no `BUF_*` variable from
   the test's environment (`BUF_TOKEN=x`, `BUF_DEBUG=1`, `BUF_TIMEOUT=1s`, `BUF_CACHE_DIR=/somewhere`) reaches the fake
   except our `BUF_CACHE_DIR`, `BUF_CONFIG_DIR`, `BUF_DATA_DIR`, all under the cwd, and `HOME` under the cwd; the temp
   folder is gone afterwards.
7. Decoys at two levels (R1 optional iii: buf errors when both share a folder, terminate.go 76-79): with
   `tempfile.tempdir` monkeypatched to `tmp_path / "a" / "tmpdir"` (the real `$TMPDIR` is never written), a v2
   `buf.yaml` in `tmp_path/a/tmpdir` whose `modules[].path` contains the run's folder (written after the folder name is
   known, through a `tempfile.mkdtemp` wrapper) with `breaking: {except: [FIELD_NO_DELETE]}`, and a `buf.work.yaml` with `directories` covering the inputs in `tmp_path/a`, each of which would
   be honoured if the walk reached it; plus a project
   `proto/buf.yaml` with the same `except`: the fake's listing shows neither project file, the configs it received are ours,
   and `FIELD_NO_DELETE` is reported.
8. No `.legacyguard/baseline` copy: a fixture where `contracts.proto` is `.` (the repo root) collects each `.proto` once,
   never a path under `.legacyguard/`.
8b. Folder `.` with a nested tree (R1-2): `acme/user/v1/user.proto` at the repo root, field deleted → `applies` is True,
   module path `acme/user/v1/user.proto`, finding `file` `acme/user/v1/user.proto` (no `./`), item
   `buf:FIELD_NO_DELETE:acme/user/v1/user.proto:...`; a path-less `PACKAGE_NO_DELETE` line gives ledger `file` `.`.

### 6.4 Results and failures (fake binary, through `proto_contract.compute`)

9. Field deleted in `User` → one break, source `buf`, kind `proto`, `file` `proto/acme/user/v1/user.proto`, `line` from
   `start_line`, subject ends `(buf FIELD_NO_DELETE)`. R1-7: a deleted service → `SERVICE_NO_DELETE` with `file` the rev
   file and `line is None` (positions 1/1/1/1); in CI its annotation has `file=` and no `line=`.
10. Type change and rename → `FIELD_SAME_TYPE` and `FIELD_SAME_NAME` items; no change → no break, exit 0 path.
11. Deleted file → `FILE_NO_DELETE` without `path`, attributed to `proto/acme/user/v1/old.proto` via the message;
    `line is None`.
12. A path-less non-file rule (fake prints a `PACKAGE_NO_DELETE` line without `path`) → `file` is the folder `proto`,
    item contains `:-:`.
13. Category: `buf_category: WIRE_JSON` → the configs say `WIRE_JSON`; the fake drops `MESSAGE_NO_DELETE`.
14. `COMPILEERR` in rev → not checked, kind `compile`, note names "the new .proto files" and the repo path (no temp path);
    in base → "the baseline .proto files".
15. `IMPORTMISSING` → kind `imports`; the note has no temp path and says dependencies are not fetched.
16. Empty sides: baseline without `.proto` → `skip`, fake not called; working tree with every file deleted, category
    `FILE` → one synthesised `FILE_NO_DELETE` break per file with the same key the fake's single-file deletion gives;
    category `WIRE` → not checked, kind `empty`.
17. Failure modes `garbage`, `array`, `rc1`, `rc0-nonempty`, `rc100-empty`, `bad-field`, `zero-line`, `abs-path` (accepted:
    stripped), `foreign-path` (failed), `plugin` (accepted) → the expected kinds; each note once per session through
    `take_notes`.
18. Missing binary → kind `missing`, note kind `buf` once; `contract_tools.buf: false` → kind `off`, no note, no call;
    configured path missing or not executable → the exact configured-path note; `old-version` → kind `old`, fake not
    called for `breaking`.
19. Timeout and process group: `hang-child` with `timeout_s: 0.3` → kind `timeout` in under 1.5 s, both pids gone (as
    `tests/test_process_group.py`).
20. `refresh`: with `contracts.proto` unset, SessionStart does not run `buf --version` (`versions.log` absent); with it set,
    one line; Stop and `ci.run` add none for an unchanged binary.
21. Sandbox (`probe` mode; skips when `os_sandbox.active` is False): `write: denied`, `connect: denied`; the report line
    has no "no OS sandbox". With `behavioral_diff.os_sandbox: off`: the fake runs unwrapped and the report part says
    "no OS sandbox".

### 6.5 Hooks (fake binary)

22. Stop: a shell edit deletes field 2 of `User` → a `contract_break` finding, source `buf`, state `open`, `contract_kind`
    `proto`; the Stop blocks once; a second Stop adds no duplicate. Restore the field and Stop → `not_applied`. Revert every
    `.proto` change (nothing differs from the baseline) → buf not called and the finding is swept.
23. Lifecycle (D6), each from a fresh open finding: (a) mode `slow` with `timeout_s: 1` → stays `open`, note kind
    `buf-timeout`; (b) binary removed (missing) → stays `open`; (c) `COMPILEERR` added → stays `open`, note `buf-compile`;
    (d) `IMPORTMISSING` → stays `open`; (e) `contract_tools.buf: false` → `not_applied`, report says "buf switched off";
    (f) R1-6 (a): `contracts.proto: proto/user.proto` (invalid) → stays `open`, note kind `buf-folder`, report "buf not
    run: contracts.proto is invalid"; (g) R1-6 (b): `contracts.proto` removed from the config → `not_applied`, report
    "buf not run: contracts.proto not set"; (h) folder outside scope: scope `src/`, `contracts.proto: ../proto` (normalises to `proto`, outside
    `src/`; the only way to reach a folder outside scope) → stays `open` on a Stop with no `.proto` change too, note kind
    `buf-folder` once across two Stops. (f) is also run on a Stop with no changed `.proto` (R2-1).
24. Waiver (R1-4, existing lookup): `/legacyguard:waive` on the finding → `waived` at once; the next Stop with the same
    text keeps `waived` and adds nothing; after an unrelated edit of that file the next Stop still keeps the same finding
    `waived` (lookup by file and item) and adds no new one. In a fresh session (new ledger) with the same file text the
    break is recorded `waived` (code hash matches); with a different text it is recorded `open`. In CI the committed
    waiver drops the break only while the hash matches (test 26).
25. Time under the lock: three changed `.proto` files and mode `slow` (3 s) with `timeout_s: 1`: one buf call
    (`calls.jsonl` has one entry), `audit.run` returns in under 7 s.
26. CI: `ci.run` on a git fixture whose PR deletes a field → `out.breaks` has the buf break; `annotations` has
    `::warning file=proto/acme/user/v1/user.proto,line=<n>,title=LegacyGuard contract::...`; a path-less break is
    annotated without `file=`; a committed waiver drops it; notes in `out.notes` and the markdown "Contract tool notes".
27. Report: `report_line` shows `buf 1.75.0 (<path>), last run <s> s` when `contracts.proto` is set, "buf not found"
    without a binary, nothing without `contracts.proto`; `ledger.add_finding(source="buf")` accepted.
28. Auto-setup (D5): a repo with `proto/buf.yaml` v1 → `contracts.proto: proto`; v2 at the root with `modules: [{path:
    proto}]` → `proto`; v2 with two modules, a `buf.work.yaml`, or an unparsable file → no key; `ci.load_cfg` without a
    committed config picks the key up. R1-3: a src layout (scope `src/`) with `src/proto/buf.yaml` → `contracts.proto:
    proto` (relative to scope, so `contract_path` gives `src/proto`); with `proto/buf.yaml` at the root and scope `src/` →
    no key.
29. A `.proto` edit at PreToolUse and PostToolUse: no buf call (`calls.jsonl` empty) and no new note (D1 b).

29b. (R1-5) `contracts.proto` unset, a shell edit changes `proto/a.proto` (in scope) → Stop gives one `buf-unset` note
    naming `proto/a.proto`, no buf call; a second Stop adds no second note; a changed `.proto` outside scope gives no note.
29c. (R1-10) `buf_version`: `1.75.0` → (1, 75, 0); `1.75.1-dev` → (1, 75, 1); `1.40.0` → accepted; `1.31.0` → refused
    (`old-version` mode, kind `old`); `buf`, `v1.75.0`, full-width digits, empty → `None` and buf is still run.

### 6.6 Real binary (each test skips when `shutil.which("buf")` is None or its version is below 1.32.0)

30. Fixture pair (base: `User {string id = 1; string email = 2;}`, `UserService.Get`; rev: field 2 deleted, field 1 typed
    `int64`, `Get` deleted, a second file deleted): `parse_buf` reads the output; `FIELD_NO_DELETE`, `FIELD_SAME_TYPE`,
    `RPC_NO_DELETE`, `FILE_NO_DELETE` present; every key of every line is in the nine of 2.3; the deleted-file line has no
    `path`; the other lines' `path` form is recorded and matches 5.3; exit 100.
31. Syntax error in rev → `COMPILE` line(s), kind `compile`; missing import → kind `imports` (stderr matches the 5.2
    pattern); an empty `rev/` given directly → non-zero exit with `ErrNoTargetProtoFiles` text; record stdout, stderr,
    exit. A deleted service → `SERVICE_NO_DELETE` with `path` and 1/1/1/1 (R1-7).
32. Decoys with the real binary: test 7's two-level decoys (`buf.work.yaml` one level up, v2 `buf.yaml` with
    `except: [FIELD_NO_DELETE]` in the temp parent; first confirm, without our `rev/buf.yaml`, that each decoy alone does
    change the result, so the test proves something), and `breaking: {ignore: [acme]}` in the project's `proto/buf.yaml`: `FIELD_NO_DELETE` still
    reported.
33. Cache and home: with the test's `HOME` and `XDG_CACHE_HOME` pointed at empty folders under `tmp_path`, a run leaves them
    empty (buf wrote only under our temp folder, which is removed).
34. Timing (the D1 gate): 10 runs on the test-30 pair and 10 on a generated 200-file module (one message with 5 fields per
    file, one field deleted in the rev side), printed median and max, sandboxed and not; assert max < `timeout_s`.
35. CRLF and BOM: the test-30 rev side written with CRLF line ends gives the same items (keys) as LF; record whether lines
    match the editor's line numbers; a BOM-prefixed file compiles or gives a `COMPILE` refusal (record which).

### 6.7 If D8 is yes: one manual verification run (implementer, scratchpad, nothing committed)

`curl -fsSL -o <scratch>/buf/buf https://github.com/bufbuild/buf/releases/download/v1.75.0/buf-Darwin-arm64`, `shasum -a 256`
must print `f71b009ebd8530b12fd85b060c6c245e15d6e5110ccc8da48d8b6fa71e59330b`, then `chmod +x`; run tests 30-35 with
`PATH=<scratch>/buf:$PATH`, plus by hand: `buf --version`; `buf breaking rev#format=dir --against base#format=dir
--error-format json` (exact bytes, key order); the same without our `buf.yaml` but with a decoy `buf.work.yaml` in the
parent (does the decoy move the root? confirms 2.4); an empty `rev/` (record the exit code and text; 2.5 sources `ErrNoTargetProtoFiles`); `--config` with the
`modules`-less v2 file (accepted?); a usage error's exit code; a `ls -R` of `BUF_CACHE_DIR` after a run (what is written).
Record the outputs in the build report and fix the fake where any shape differs.

## 7. Acceptance criteria

1. With buf absent, `contract_tools.buf: false`, or `contracts.proto` unset, every existing test passes unchanged and no
   `buf` process starts (`versions.log`/`calls.jsonl` absent); the skip count rises only by the real-binary tests 30-35.
2. With the fake, tests 1-29 pass; a field deleted from a message under `contracts.proto` is a `contract_break` finding at
   Stop that blocks once, and a `::warning` with `line=` in CI.
3. buf is never started without: cwd a fresh `lg-buf-*` folder, inputs `rev#format=dir` and `base#format=dir`, our
   `buf.yaml` in both and `--config`/`--against-config lg-buf.yaml`, `BUF_CACHE_DIR`/`BUF_CONFIG_DIR`/`BUF_DATA_DIR`/`HOME`
   inside that folder, no other `BUF_*` variable, `input=b""`, and `os_sandbox.run` (tests 6, 19, 21; a grep in review).
   Never `--against-registry`, a git or module input, `--path` or `--limit-to-input-files`.
4. No project file other than `.proto` bytes reaches buf; a project `buf.yaml`/`buf.work.yaml`/`buf.lock`, or a decoy
   above the temp folder, does not change the result (tests 3, 7; 32 with the real binary).
5. Every failure (missing, off, old, timeout, unstartable, bad output, compile, imports, too large, refused, internal
   exception) gives no break and at most one note per kind per session, and never raises out of the Stop hook or `ci.run`
   (tests 14-19).
6. A Stop starts at most one buf process, and its time under the ledger lock is bounded by `timeout_s` plus the 5 s reap
   (test 25).
7. Lifecycle: only `off` and a removed `contracts.proto` sweep open buf findings; every other unchecked kind, `config`
   included, keeps them; a Stop with no `.proto` change and a valid folder sweeps them (tests 22, 23).
8. Item keys: `buf:<type>:<rel or ->:<message[:80]>:<sha12>`, no lines or columns; equal for CRLF and LF texts (tests 2, 35).
9. `CLAUDE.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md` and `docs/HANDOFF.md` describe Part D, its limits and what is
   unverified (written by the coordinator; part of the review).
10. (R1) `contracts.proto: .` gives repo paths without `./` (test 8b); an invalid or out-of-scope folder is not checked
    and keeps findings (test 23 f-h); a removed key sweeps with a report line (23 g); a changed `.proto` with the key
    unset gives one `buf-unset` note (29b); a folder named `buf.yaml`/`buf.work.yaml`/`buf.lock` refuses the run (3); no
    CI annotation carries `line=` for a 1/1/1/1 position (9); versions parse per 5.2 and only below 1.32.0 are refused (29c).

## 8. Known limits to document

- Stop and CI only (D1): a breaking `.proto` edit is not asked about before it lands; the Stop blocks once afterwards.
- No fallback (D9): without buf (or with a too-old buf), `.proto` changes are unchecked, with one note per session.
- One proto root (`contracts.proto`, one folder); imports from outside it, BSR `deps` and `buf.work.yaml` workspaces are
  not supported (D4): the run is "not checked" with kind `imports`. Multi-module repos need a follow-up (D3 b).
- The project's `buf.yaml` breaking settings (`except`, `ignore`, `ignore_only`, `ignore_unstable_packages`) are
  deliberately not honoured; projects that rely on them see more findings than their own `buf breaking` (waive them).
  Unstable packages (`v1alpha1`, `v1beta1`) are checked too.
- A module that does not compile on either side reports nothing (kind `compile`); open findings stay open.
- Path-less annotations (`PACKAGE_*_NO_DELETE`, and any other rule without a new-side location) are attributed to the
  folder; CI annotates them without `file=`.
- Two nested messages of the same short name in one file losing the same field number and name give one item (buf's
  message uses the short name).
- Waivers (R1-4): within a session a waived buf finding stays waived whatever later edits do to the file (the Stop
  looks findings up by file and item, as for every source). The file's code hash decides only for a new finding (a new
  session) and in CI, where a committed waiver holds only while the file's text is unchanged.
- `auto` takes buf from `PATH`; a project-local `node_modules/.bin/buf` (what `npm install @bufbuild/buf` creates) is
  trusted; with no fallback it could hide breaks. Follow-up (with squawk and oasdiff): refuse a resolved path inside the
  project.
- A fresh cache per run (no reuse between Stops); cost UNVERIFIED.
- The FILE category reports language-option changes (`go_package`, `java_package`, ...) as breaks for every project (D2 ii
  is the alternative).
- Old-vs-old noise does not apply (static input), but buf's own rule set changes between versions: a new rule in a later
  buf can add findings at the next Stop without any edit to the files.

## 9. What is unverified

- **The real binary was not run.** Unverified: the exact JSON bytes (read from `externalFileAnnotation` only), the printed
  `path` form for a relative directory input (`rev/<m>`?), whether the missing-import text really carries `rev/<m>` as its
  path (the message shape and exit 100 are now sourced, 2.5), the exit code for usage errors and for an empty input
  (`ErrNoTargetProtoFiles` is sourced; its exit code 1 is inference), `buf --version` output, timing (start-up, image builds, Wasm runtime, sandbox start) and the
  D1 gate, what is written to `BUF_CACHE_DIR`, and that a `modules`-less v2 `buf.yaml` passed with `--config` is accepted.
- That a decoy `buf.work.yaml` or v2 `buf.yaml` above the input is ignored once our v2 `buf.yaml` sits in the input (read
  in `terminate.go` and `bucket_targeting.go`, not run).
- That no network call happens on this path (inference from source; the sandbox would turn one into a failure).
- `getDescriptorAndLocationForDeletedMessage` returning a nil location for `MESSAGE_NO_DELETE` (inferred). The other
  round-1 citations (commit e0ff84446e, `handleBreakingServiceNoDelete`, `response_writer.go` 53-81, `module_dep.go`
  193-199, `length.Bytes`) were read by the reviewer in round 2 and are now confirmed from source; that
  `SERVICE_NO_DELETE` prints 1/1/1/1 is still from source only, not a run.
- The 1.52.x garbled `MESSAGE_SAME_JSON_FORMAT` message (reviewer's v1.52.0 read; not run, exact text unknown).
- Check annotation column units (bytes, runes, tab stops; `COMPILE` columns are UTF-8 bytes per the reviewer) and how a lone `\r` or a BOM affects buf's line numbers; the plan uses lines only
  for annotations.
- That the `PACKAGE_*` deletion rules print no `path` (read as "we don't know where the enum was deleted from").
- Behaviour of buf versions between 1.32.0 and 1.74.x: the JSON line shape is read at v1.75.0 only (R1-1: the v1.53.0
  "JSON" fix was about `MESSAGE_SAME_JSON_FORMAT`, not `--error-format json`, so no stability claim rests on it); compile
  errors and missing imports before v1.68.0 go through the old compiler (v1.68.0 switched, v1.68.1 reverted the new
  report format), so `COMPILE` lines, their columns and the import text for 1.32 to 1.67 are unread; Linux arm64,
  musl, Windows.
- The docs/source disagreements: docs say `modules` is required (source defaults to `.`), and say dir inputs look for
  `buf.yaml` "in your current directory" (source walks to `/`).
- Whether `BUF_TOKEN` is read on this path (docs-only; we drop every `BUF_*` variable anyway).
