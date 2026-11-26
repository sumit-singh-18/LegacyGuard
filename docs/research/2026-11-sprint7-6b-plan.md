# Sprint 7.6 build spec, Part B: code-first OpenAPI (the spec the app generates, old against new)

Researcher agent, 2026-11-26, main at 70f2a00 (Part A built at e90ff7f). **Revised after reviewer REVISE (rounds 1 and 2); reviewer PASS in round 3.**

**User decisions (2026-11-26, final):** D1 (a) FastAPI-style `app.openapi()` only; M1 (a) block the Stop once; D10 (a)
a paused check restarts only on a cache-key change; D2 to D9, M2, M3 and M4 as recommended. Coordinator note on the
round-3 optional items, to be built: update `failed.attempts` in the store-lock block from the session.json read under
that lock (as for `app_openapi_seen`); section 8 says in one line that with `stable: None` cached every later break of
the session is a hedged, non-blocking RISK finding.
Self-contained: the
implementer should not need the Part A plan, but it reuses Part A's code (`guardlib/contract_tools.py`,
`guardlib/migration_drift.py`) and its wiring at PostToolUse, Stop and CI. Spec origin: Part A plan section 10
(unreviewed sketch) and `docs/HANDOFF.md` "Next (start here)".

**Round-2 corrections (reviewer, 2026-11-26) are folded in and override earlier text:** (1) a baseline that cannot be
built no longer re-runs on every guarded edit: a failed attempt is remembered under the current cache key in
session.json `app_openapi.failed`; after 2 failed checks the check pauses until the key changes (new decision D10),
with a once-per-session note and a report line; the real cost is in section 8; test 7c. (2) Hedged Stop findings have
a lifecycle: `_oas_findings` updates status and state when a known item's hedge changes (RISK/`recorded` <->
FAILURE/`open`) and sweeps `recorded` `app-openapi` findings that are no longer reported to `not_applied`; a
not-checked or paused Stop sweeps nothing (5.5; tests 11b, 11c). Optional items: `applies` takes the data snapshot
explicitly; 5.6 says a deadline hit during the noise run leaves the baseline cached with `noise_pending`.

**Round-1 corrections (reviewer, 2026-11-26) are folded in and override earlier text:** (1) the noise hedge applies
only to breaks that share `(operation, path)` with a WARN/ERR change old-vs-old itself reports; typical noise
(import-time defaults, set-ordered operationIds, `Body_*` names) is INFO in oasdiff and leaves the old-vs-old list empty,
so a real break is still a wake and a finding (5.3 step 5, 2.5; the fake's loud mode is labelled a stand-in; tests 5a to
5c). (2) Trigger: with no cached baseline any guarded source edit runs the check once; the trigger set is the union of
the baseline's and the last working-tree run's imported files; `ci.run` runs the check once whenever a guarded `.py`
changed (D6, 5.3, 5.5; tests 10a to 10d). (3) The spec wake is raised before `post_edit.py`'s no-targets return, and
`wakeups` and `app_openapi_seen` are updated in the store block whatever the targets, with the wake decision re-checked
under that lock (5.5; test 20). (4) The old-vs-old change list is cached with the baseline; one deadline caps the spec
stage; the worst case is summed against the 150 s hook limit (5.6). (5) A half-built baseline is retried once and never
cached (5.3 step 1; test 7b). (6) The D5 dedupe key is `(id, operation, path, text)` (test 14). (7) Skip counts and a
guard-side test module that needs no FastAPI; the parser fallback says which renamed schemas it did not compare (6,
5.4; test 6). Refusal texts use the label "the app's generated spec", not the temp file name (5.2). New user decisions
M1 to M4 (section 1). Reviewer's optional items: tests 2b and 2c; a methods-order note in section 8.

Fixtures in this plan are generic shapes (a `svc` package with a pydantic `Note` model). None of the held-out cases in
`docs/validation/holdout-set.md` or `holdout-set-2.md` is used or named. FastAPI is named only as the framework.

What the sketch got right, and what it missed (each checked below):

| Sketch (Part A section 10) | What the code and sources say |
|---|---|
| "returns `json.dumps(app.openapi())`" | Works for FastAPI's own `app.openapi()`: `get_openapi` ends with `jsonable_encoder(...)` (2.1). A project can replace `app.openapi`, and then the output may hold non-JSON values or non-string keys, so the text must be built defensively (5.1). |
| (implied) the spec is deterministic | **Not in general.** Measured (section 3): the bytes depend on `PYTHONHASHSEED` (method order and the operationId of a route with several methods come from a `set`), and model defaults computed at import (`datetime.now()`, `random.random()`) differ per process even with the worker's determinism on. With `PYTHONHASHSEED=0` (which `clean_env` sets) and no import-time defaults, 10 runs gave the same bytes. Hence an old-vs-old check (D4). |
| "a worker spec `kind: "openapi"`" | The main case (a model-only edit) has **no behavioural-diff targets** at all (measured: `diff.changed_targets` on a `body` -> `text` field rename returns `[]`), so no worker runs that could carry the spec. A separate target-side script like `runner/lg_migrations.py` fits better (D2). |
| "Flask has no built-in spec" | Confirmed; Starlette's `SchemaGenerator` only reads docstrings; DRF's built-in schema support is deprecated; django-ninja has `get_openapi_schema()` but is not an `app:` object. Scope: FastAPI-shaped `app.openapi()` only (D1). |
| "`--allow-external-refs=false`, refs internal" | FastAPI's generated refs are `#/components/schemas/...` (measured), and Part A's `oasdiff_breaking` already adds the flag when the scan finds no file refs. A project's own `openapi()` override could add file or URL refs: refuse those outright here (5.2). |
| "can wake, cannot block" | Correct: it runs project code, so never in PreToolUse. Recommended: wake on a new ERR change at PostToolUse, `contract_break` finding at Stop, `::warning` in CI (D3). |

## 0. Summary

When the config names an `app:` whose object has a callable `openapi()` (FastAPI), LegacyGuard builds the app's
generated OpenAPI spec in the baseline and in the working tree, each in its own target-interpreter process under the
worker's set-up (`lg_worker.prepare`, `reset_determinism`, `setup_phase()`, `load_app`, OS sandbox, throwaway cwd), and
compares them:

1. Canonical JSON text per tree (`json.dumps(..., sort_keys=True)` after a string-key round trip).
2. **Byte-equal: done**, no tool runs (the common case for edits that do not touch the API surface).
3. Different: `contract_tools.oasdiff_breaking` on the two texts (internal refs only, so `--allow-external-refs=false`).
   ERR changes are breaks, WARN changes a note. Without oasdiff: the built-in parser on the two dicts, restricted to
   schema names present on both sides (5.4).
4. Noise filter: the baseline spec is built **twice** (two processes, once per session, cached). If the two baseline
   texts differ, oasdiff also runs old-A vs old-B **once** (the list is cached with the baseline). An old-vs-new break
   that old-vs-old also reports is dropped; one that only shares `(operation, path)` with an old-vs-old WARN/ERR change
   is hedged; every other break stands. oasdiff's `breaking` prints only WARN and up, and the usual noise (a default
   computed at import, a set-ordered operationId) is INFO, so the old-vs-old list is usually empty (2.5).

It runs at PostToolUse (a wake for a new ERR change, D3), at Stop (`contract_break` findings, source `app-openapi`) and
in `ci.run` (breaks, `::warning`), when a changed guarded source file is one the app imported in the baseline or in the
last working-tree run, or on the first guarded edit of a session before anything is cached (D6). It
catches a schema-only change such as a pydantic field renamed `body` -> `text`, which no route diff, behavioural diff or
static check sees today (2.4).

## 1. Open decisions for the user (each with a recommendation)

| # | Decision | Recommendation |
|---|---|---|
| D1 | Scope. (a) Any `app:` object with a callable `openapi()` returning a dict that has `openapi` and `paths` keys (FastAPI, and any Starlette app a project gives such a method); (b) also Flask extensions (flask-openapi3 `api_doc`, flask-smorest `api.spec`), django-ninja `get_openapi_schema()`, DRF/drf-spectacular. | **(a) only.** FastAPI generates the spec from the code with no extra package (2.1). The others need an extension object that `app:` does not name (ninja, smorest) or the Django URL conf and settings (DRF), and the built-in DRF support is deprecated (2.2). A later key `contract_tools.openapi_callable: "module:attr"` could cover them; not in Part B. Flask and plain Starlette apps: no run, no note, one word in the report line. |
| D2 | Where the spec is built. (a) A new target-side script `runner/lg_openapi.py`, one process per tree, like `runner/lg_migrations.py`; (b) a worker spec `kind: "openapi"` in `lg_worker.execute`. | **(a).** A model-only edit has no diff targets, so (b) would have to start a worker anyway; (a) reuses Part A's run/parse/cache pattern line for line (`migration_drift.run_tree`, 2.3) and leaves the worker's request flow, warm servers and OLD-A/OLD-B/NEW triple untouched. Cost: one extra process per tree (about 0.2 s each on the fixture, section 3). Piggybacking on the diff's workers when they run anyway is a later optimisation. |
| D3 | What PostToolUse does with a new ERR change. (a) Wake (exit 2), once per break item per session; (b) a context note only, as Part A's drift (D2 there). | **(a).** Unlike a model edit that the next step's migration completes, a breaking API schema change is what the guard exists to show, and nothing else reports it before Stop. The wake is said once per item (`app_openapi_seen`, re-checked under the store lock so two overlapping hooks do not both wake), and it is raised whether or not the edit had function targets (5.5). WARN changes and hedged breaks are context notes, never a wake on their own. |
| D4 | Old-vs-old noise. (a) Byte equality first; the baseline built twice once per session; when the two texts differ, oasdiff old-A vs old-B once, its change list cached with the baseline; an old-vs-new break also in that list is dropped, one sharing `(operation, path)` with a WARN/ERR change in it is hedged, the rest stand; (b) byte equality only; (c) build old twice on every check. | **(a).** Equality skips oasdiff for most edits at no cost; the second baseline build costs one extra process **once per session** (parallel with the first, about +0.2 s wall on the fixture). Hedging every break whenever the texts differ (the round-0 rule) would have silenced real breaks on any app with an import-time default, because that noise is INFO in oasdiff and never printed by `breaking` (2.5). (c) doubles the per-check cost for no gain once the baseline is cached. |
| D5 | A project that also has a static `contracts.http` file. (a) Both run; at Stop and in CI a code-first break whose oasdiff `(id, operation, path, text)` equals a static-file break in the same run is dropped, and the static finding's evidence says "also in the app's generated spec"; (b) both run, no dedupe; (c) code-first off when a static file is configured. | **(a).** The static file may be hand-written (so it can disagree with the code) or exported from the app (then the same break shows twice). The key includes `text`, so two different properties removed on the same operation stay two breaks. (c) would miss the case where the code drifts from a hand-written contract. PostToolUse cannot dedupe against the static check (that runs in PreToolUse on a different edit), so a wake may repeat what PreToolUse asked about; documented limit. |
| D6 | When it runs. (a) Session: with no cached baseline, any guarded source edit runs the check once (that run builds the cache); afterwards, a changed guarded file in the union of the baseline's imported files, the last working-tree run's imported files (stored after every run) and the app module. CI: once per run whenever any guarded `.py` changed. (b) Only files under the app's top-level package folder; (c) every guarded source edit, every time. | **(a).** It matches what can change the generated spec, including a top-level `models.py` next to `main.py` and a module created during the session and imported later (its path enters the set from the working-tree run that first imports it). The first-edit run costs one check per session; a cached `kind: none` (Flask) stops all later runs. In CI there is no cache, and building the baseline only to test membership costs the same processes as the check, so it simply runs once. (b) misses models outside the package; (c) costs a process on every edit. |
| D10 | What restarts a paused check (round 2). After `MAX_BASE_ATTEMPTS` = 2 checks in which the baseline spec could not be fully built under the same cache key, the check pauses. (a) Only a change of the cache key restarts it (baseline commit or generation, `app`, interpreter, `io`, `source_roots`, `env`); (b) also an edit to the app module or to a file the working tree imported. | **(a).** The baseline tree does not change during a session, so no edit can make the baseline build succeed; (b) would bring back the cost this rule removes (every such edit pays up to the 45 s cap again). Two attempts cover a transient failure (a slow first import that timed out). When both attempts built one identical baseline text, the check goes on with that text and hedged breaks instead of pausing. |
| M1 | Stop blocking. `app-openapi` findings are `contract_break` with status FAILURE, so the Stop audit blocks once on them (`audit.BLOCKING_STATUS`, guardlib/audit.py:11, 200-201), as for drift and static contract breaks. (a) Block once; (b) record without blocking. | **(a).** "Cannot block" in the sketch meant it cannot stop the edit before it lands (no PreToolUse). At Stop every other contract break blocks once and Claude explains or the developer waives; a generated-spec break is no weaker evidence (it comes from the code itself). |
| M2 | Hedged breaks at Stop (rare after correction 1: only breaks sharing an operation with old-vs-old WARN/ERR noise, or from a baseline built once). (a) A recorded, non-blocking finding (status RISK, state `recorded`, kind `contract_break`, source `app-openapi`, extra `hedged: true`); (b) a report note only. | **(a).** It shows in the report and session map with its hedge, can be waived, and never blocks. A note alone is easy to miss. |
| M3 | Where the baseline spec texts are cached. (a) A separate `.legacyguard/app_openapi_base.json` (atomic write), with only `{key, sha_a, sha_b, files, kind}` in session.json; (b) everything in session.json. | **(a).** session.json is read and written under the ledger lock by every hook; generated specs can be hundreds of KB (inference for large apps). The file is written outside the lock (tmp + `os.replace`), and session.json's key and shas tell whether it is current; a mismatch or a missing file means "not cached". |
| M4 | CI `--fail-on`. (a) Code-first breaks count only under `fail_on: any`, like every other contract break (`ci.exit_code`, guardlib/ci.py:319-326); (b) they also fail under the default `difference`. | **(a).** Consistent with Part A; the default fails only on behavioural differences. |
| D7 | No oasdiff on PATH (CI, per Part A D4). (a) The built-in parser on the two generated dicts, comparing fields only for schema names present on both sides, with the "oasdiff not found" note; (b) no check without oasdiff. | **(a).** It catches the headline case (a field removed or retyped on a model both trees have) and is the Part A fallback rule. The restriction avoids false "field dropped" breaks when pydantic renames a component (a second model of the same name appears and both become module-qualified, measured in section 3). |
| D8 | Config key. (a) `contract_tools.openapi_from_app: auto\|false`; (b) `contracts.openapi_from_app`. | **(a).** `contracts.*` holds file paths (`config.contract_path`, guardlib/config.py:50-56); the tool switches live under `contract_tools` (Part A). `auto` = run when `app:` is set and the app has `openapi()`. |
| D9 | Finding source. (a) New `ledger.SOURCES` entry `"app-openapi"`, `contract_kind: "http"`; (b) reuse `"oasdiff"`. | **(a).** The Stop logic must leave these findings alone when a run fails (as `makemigrations` findings are, guardlib/audit.py:69-80), which needs its own source; the evidence also differs (generated, not a file). Parser-fallback breaks use the same source. |

## 2. Sources and confirmed claims

"Inference" marks my own reasoning. Installed sources are FastAPI 0.162.7, pydantic 2.14.3 (pydantic-core 2.50.4),
Starlette 1.10.0 in `.venv-dev` (Python 3.12.16). **The 3.10 venv (`/private/tmp/claude-501/venv310`, Python 3.10.22)
has no FastAPI, pydantic, Starlette or Flask** (nor Django, nor PyYAML, checked with an import loop); nothing was
installed. No apispec, flask-smorest, django-ninja or DRF in either venv. `which oasdiff`: not found.

### 2.1 FastAPI `app.openapi()`

- **Caching.** `FastAPI.openapi()` returns `self.openapi_schema` when it is set and the router's routes version is
  unchanged, else calls `get_openapi(...)` and stores the result
  (`.venv-dev/lib/python3.12/site-packages/fastapi/applications.py:1085-1118`). The docs: "By default, what the method
  `.openapi()` does is check the property `.openapi_schema` to see if it has contents and return them" and a project
  may replace it (`app.openapi = custom_openapi`) —
  [Extending OpenAPI](https://fastapi.tiangolo.com/how-to/extending-openapi/). Each tree runs in a fresh process, so the
  cache never carries between trees (inference). The method does not look at `openapi_url` (same lines), so
  `FastAPI(openapi_url=None)` apps still produce a spec.
- **JSON-safe output.** `get_openapi` ends with `return jsonable_encoder(OpenAPI(**output), by_alias=True,
  exclude_none=True)` (`fastapi/openapi/utils.py:671`), so FastAPI's own spec is plain JSON types; `components.schemas`
  is sorted by name (`utils.py:661`). A replaced `openapi()` is the project's code and may return anything (inference),
  hence 5.1 step 5.
- **operationId.** `route.unique_id = route.operation_id or generate_unique_id(route)` (`fastapi/routing.py:1081`);
  `generate_unique_id` is `f"{route.name}{route.path_format}"` with non-word characters replaced, plus
  `_{list(route.methods)[0].lower()}` (`fastapi/utils.py:95-100`), and `route.methods` is a **set**
  (`routing.py:1074`). The docs say the id is built from "the **function name**, the **path**, and the **HTTP
  method/operation**" and is also used "for the names of any needed custom models, for requests or responses" —
  [Generating SDKs](https://fastapi.tiangolo.com/advanced/generate-clients/); in the code those names are
  `Body_<unique_id>` (`fastapi/dependencies/utils.py:1376`) and `Response_<unique_id>` (`routing.py:1160`). So for a
  route with several methods, the operationId and those model names depend on set order, i.e. on `PYTHONHASHSEED`
  (measured, section 3). `get_openapi_path` also iterates `for method in route.methods` (`openapi/utils.py:335`), so
  the key order inside a path depends on it too. A repeated operationId only warns ("Duplicate Operation ID",
  `openapi/utils.py:289-295`).
- **Model names (`$ref`).** Definitions come from pydantic's `GenerateJsonSchema(ref_template=REF_TEMPLATE)
  .generate_definitions` (`fastapi/_compat/v2.py:287-348`). Pydantic chooses each definition name from a prioritised
  list: the short class name, then `-Input`/`-Output` (mode), then the module-qualified name, then with an occurrence
  index (`pydantic/json_schema.py:2426-2470`). Measured: two models named `Note` in `svc/models.py` and `svc/other.py`
  become `svc__models__Note` and `svc__other__Note` — the first one's name changes when the second appears; a model
  whose validation and serialization schemas differ appears as `Note-Input` and `Note-Output`. The pydantic docs
  confirm the `ref_template` mechanism and that keys are sorted recursively except under `properties` —
  [JSON Schema](https://pydantic.dev/docs/validation/latest/concepts/json_schema/); they do not describe the collision
  rule (only the source does).
- **Pydantic v1.** "FastAPI version 0.100.0 had support for either Pydantic v1 or v2", 0.126.0 "dropped support for
  Pydantic v1", 0.128.0 "dropped support for `pydantic.v1` as well, so the latest versions of FastAPI require Pydantic
  v2" — [Migrate from Pydantic v1 to Pydantic v2](https://fastapi.tiangolo.com/how-to/migrate-from-pydantic-v1-to-pydantic-v2/).
  The installed FastAPI raises for v1 models (`fastapi/utils.py:66-68`). So the spec's shape can differ between
  projects on old FastAPI + pydantic v1 (FastAPI's own v1-era naming) and current ones; **both trees always run on the
  same interpreter**, so old and new agree on the generator (inference); the versions go into the cache key (5.3). Old
  FastAPI / pydantic v1 was not run here.
- **Imports under the sandbox.** Measured (section 3): `lg_worker.prepare` + `setup_phase()` + `load_app` +
  `app.openapi()` under `sandbox-exec`, fixture app with an enum, `Decimal`, `date` and `set` defaults: no block, no
  write, 0.20 to 0.31 s wall per process. Sprint 3 already fixed the one known clash (frozen `datetime` broke pydantic
  imports; CLAUDE.md "Sprint 3 ... Fix").

### 2.2 Other frameworks (scope, D1)

- **Starlette:** `SchemaGenerator` builds the document from `app.routes` and the endpoints' docstrings; the page shows
  only docstring-based responses and points to third-party tools for richer output —
  [Starlette docs/schemas.md](https://raw.githubusercontent.com/Kludex/starlette/main/docs/schemas.md). Not code-first
  in the sense needed here (a model field rename does not change a docstring).
- **Flask:** no built-in spec (Flask's own docs have none; inference from the absence). apispec needs the user to
  register paths ("Register the path and the entities within it") and returns `spec.to_dict()` —
  [apispec](https://apispec.readthedocs.io/en/latest/); flask-smorest keeps an `api.spec` object and a `flask openapi
  print` command — [flask-smorest OpenAPI](https://flask-smorest.readthedocs.io/en/latest/openapi.html);
  flask-openapi3's `OpenAPI.api_doc` property returns the spec as a dict —
  [flask_openapi3/openapi.py](https://raw.githubusercontent.com/luolingchun/flask-openapi3/master/flask_openapi3/openapi.py).
  Each needs its extension object, not the Flask app, except flask-openapi3 whose app *is* the `OpenAPI` object
  (inference: a later duck-typed `api_doc` branch could cover it).
- **django-ninja:** `NinjaAPI.get_openapi_schema(*, path_prefix=None, path_params=None) -> OpenAPISchema` —
  [ninja/main.py](https://raw.githubusercontent.com/vitalik/django-ninja/master/ninja/main.py), and `class
  OpenAPISchema(dict)` — [ninja/openapi/schema.py](https://raw.githubusercontent.com/vitalik/django-ninja/master/ninja/openapi/schema.py).
  The API object is not the `app:` (Django projects set `django_settings`), so it needs its own key. Later.
- **DRF:** `SchemaGenerator(...).get_schema()` walks the URL conf (`settings.ROOT_URLCONF` by default); the page says
  the built-in OpenAPI support "is deprecated" and recommends drf-spectacular —
  [DRF Schemas](https://www.django-rest-framework.org/api-guide/schemas/). Later, if at all.

### 2.3 This repository (file:line at 70f2a00)

- `lg_worker.load_app(ref)`: `module:name` or `module:factory(literal args)`, built under `setup_phase()`, cached per
  process including failures (`runner/lg_worker.py:1387-1440`); `setup_phase` (236-245); `prepare` installs blocks,
  stubs, determinism, the import path, tree protection and the cwd (2057-2081); `reset_determinism` seeds `random`,
  uuid ids and the clocks (1368-1385) and is **not** called by `prepare` (so import-time `random.random()` is unseeded;
  measured, section 3).
- Route introspection runs the same set-up in a `-c` script under `os_sandbox.wrap(..., writable=[tempdir, tmp],
  readonly=[root])` with `clean_env(cfg)` (`indexer/routes.py:18-71, 74-103`); `clean_env` drops secret-looking
  variables and sets `PYTHONHASHSEED=0` (`indexer/coverage_map.py:20-24`).
- Part A pattern to copy: `migration_drift.run_tree` (request JSON, `scratch.current` cwd, sandbox, timeout, one marker
  line on stdout; `guardlib/migration_drift.py:121-160`), `cache_key` (102-114), `compute` returning an outcome with
  `state` checked/not_checked/hedged (244-282), `context_text` (285-296), `record_run` (299-307); the target script
  `runner/lg_migrations.py:117-168`.
- `contract_tools.oasdiff_breaking(cfg, rel, before, after, trees, timeout, specs=None, pre_edit=False)` materialises
  both texts under a temp folder, scans `$ref`s, adds `--allow-external-refs=false` when only internal refs are found,
  runs oasdiff with a temp cwd and no `OASDIFF_CONFIG`, parses JSON and never raises
  (`guardlib/contract_tools.py:399-456`; scan 342-392); `break_fields` (459-465); `take_notes` once per session per kind
  (468-481); `report_line` (484-509); `settings` defaults (98-107).
- PostToolUse wiring for drift: `scripts/post_edit.py:33-89` (read cached base under the lock, compute outside it
  inside one `scratch.for_trees` block, store under a short lock, output per branch).
- Stop: `audit.run` opens one `for_trees` block, computes drift before `_contract_findings`, whose `_drift_findings`
  joins `seen_items` before the `not_applied` sweep and leaves findings alone on a failed run
  (`guardlib/audit.py:36-111, 184-201`).
- CI: `ci.run` per-file loop then `_drift` inside the same `for_trees` block; breaks after the waiver check;
  annotations are `::warning file=...` for contract breaks (`guardlib/ci.py:128-205, 219-240`).
- Parser fallback building blocks: `contracts_parse.http_items_from(spec)` (routes, params, `field:<schema>.<prop>`;
  `guardlib/contracts_parse.py:33-48`) and `checks._http_breaks` (`guardlib/checks.py:43-52`).
- Ledger: `SOURCES` (guardlib/ledger.py:22), `fresh` keys (29-33).
- Hook timeouts: PostToolUse 150 s, async with `asyncRewake`; Stop 240 s (`hooks/hooks.json`).

### 2.4 The gap is real (checked)

- `diff.changed_targets("svc/models.py", before, after)` for a class whose only change is `body: str` -> `text: str`
  returns `[]` (run here), so no behavioural diff, no route target, no wake.
- `checks.check_python_source` reports a dropped or retyped model field only for class names that appear in the
  **static** `contracts.http` file (`cp.schema_names(http)`, guardlib/checks.py:196-208); with no static file, nothing.

### 2.5 oasdiff on JSON text (Q3)

- **JSON input.** oasdiff v1.39.0 loads specs with kin-openapi `v0.153.0` (`go.mod`,
  [go.mod](https://raw.githubusercontent.com/oasdiff/oasdiff/v1.39.0/go.mod)); single files go through the
  `openapi3.Loader` (`LoadFromFile`/`LoadFromDataWithPath`,
  [load/spec_info.go](https://raw.githubusercontent.com/oasdiff/oasdiff/v1.39.0/load/spec_info.go)). In kin-openapi
  v0.153.0 `unmarshal` tries YAML first and falls back to JSON when `mayBeJSON` holds —
  [openapi3/marsh.go](https://raw.githubusercontent.com/getkin/kin-openapi/v0.153.0/openapi3/marsh.go). JSON text is
  therefore accepted (either as YAML, of which it is nearly a subset, or by the JSON fallback; inference from the
  code, **not run**). Part A's `_materialise` parses the text with `yaml.safe_load`, which reads JSON too.
- **`--allow-external-refs`.** Defined in `addCommonDiffFlags` with default `true` and help "allow external $refs in
  specs; disable to prevent SSRF when processing untrusted specs" —
  [internal/cmd_flags.go](https://raw.githubusercontent.com/oasdiff/oasdiff/v1.39.0/internal/cmd_flags.go). With
  `=false` oasdiff refuses external `$ref`s with exit 123, and the `=` form is required —
  [docs/EXTERNAL-REFS.md](https://raw.githubusercontent.com/oasdiff/oasdiff/v1.39.0/docs/EXTERNAL-REFS.md).
- **What the rename gives.** Rule levels at v1.39.0 —
  [checker/rules.go](https://raw.githubusercontent.com/oasdiff/oasdiff/v1.39.0/checker/rules.go): new required request
  property ERR, request property removed WARN, response required property removed ERR, response optional property
  removed INFO, component schema removed (`components.schemas.*:remove`) INFO, and also INFO:
  `RequestPropertyDefaultValueChangedId`, `ResponsePropertyDefaultValueChangedId`, `APIOperationIdRemovedId`,
  `APIOperationIdAddId` (same file, read for round 1). So the measured noise kinds (an import-time default, an
  operationId or `Body_*` name that follows set order) should give an **empty** `breaking` list old-vs-old (inference,
  not run; a `Body_*` rename also moves a `$ref`, whose handling is unverified). So `body` -> `text` on a model used in a
  request and a response should give at least one ERR (inference, not run); a component that is only renamed by pydantic
  is INFO and not printed by `breaking` (which prints WARN and up, Part A plan 2.1). Whether oasdiff diffs the resolved
  operation schemas so that a pure `$ref` rename gives no ERR at all is **unverified**.
- **Unverified (the binary was never run here):** that `breaking` loads a JSON file with this exact layout; its run
  time on a generated spec (FastAPI specs of large apps can be hundreds of KB); the rule outputs above; whether a model
  default value change is reported and at what level.

## 3. Measurements (this Mac, `.venv-dev`, nothing installed; scripts in the session scratchpad, not in the repo)

Fixture: package `svc` with `app.py` (FastAPI, `POST /notes` taking and returning `Note`, `api_route("/multi",
methods=["GET","POST","PUT"])`, two routes returning two different models both named `Item` from `svc/a/models.py`
and `svc/b/models.py`, a body with both), `Note` with `date`, `Decimal`, `set` and enum defaults.

| Measurement | Result |
|---|---|
| `json.dumps(app.openapi())` sha, 11 processes, `PYTHONHASHSEED` 0,0,1,2,3,4,5 and 4 random | 4 different texts; seed 0 twice: identical |
| Same with `sort_keys=True` | 3 different texts (operationId `multi_multi_get` vs `multi_multi_post` survives sorting) |
| Seed 0, 10 processes, heap shifted by 1k to 10k objects before import | 1 text (no address dependence) |
| Model names | `svc__a__models__Item`, `svc__b__models__Item`, `Note-Input`, `Note-Output`, `Body_multi_body_multi_body_post` |
| Second same-named model added in another module | `Note` -> `svc__models__Note` and `svc__other__Note`; every `$ref` to the first changes |
| `body: str` -> `text: str` in `Note` (single schema, seed 0) | the only differences: property `body` gone, `text` added, `required` changed |
| `lg_worker.prepare` + `setup_phase` + `load_app` + `openapi()` under `sandbox-exec`, `io: stub` | ok, `blocks == []`; wall 0.20 to 0.31 s per process, of which import 0.11 to 0.17 s and `openapi()` 0.004 to 0.006 s; spec 5,060 bytes |
| Same with `deterministic: True` | same text as without |
| Import-time default `datetime.datetime.now()`, deterministic on, 2 processes | texts differ |
| `random.random()` default | differ (prepare does not call `reset_determinism`) |
| `str(uuid.uuid4())` default, `time.time()` default | identical (faked by `install_determinism`) |
| `diff.changed_targets` on the rename | `[]` |

## 4. Current code paths that change

`runner/lg_openapi.py` (new, target side), `guardlib/app_openapi.py` (new, guard side), `guardlib/contract_tools.py`
(settings default, `internal_only` and `label` on `oasdiff_breaking`, report line), `guardlib/config.py` (DEFAULTS),
`guardlib/ledger.py` (SOURCES `app-openapi`; fresh keys `app_openapi: None`, `app_openapi_seen: []`),
`scripts/post_edit.py`, `guardlib/audit.py`, `guardlib/ci.py`, `scripts/session_start.py` (delete
`.legacyguard/app_openapi_base.json` with the ledger reset, so a new session never reads an old session's specs; the
key would reject it anyway), `tests/oasdiff_fake.py` (`diff` and `diff-loud` modes), `tests/test_app_openapi.py` and
`tests/test_app_openapi_guard.py` (new), `CLAUDE.md`, `docs/PROGRESS.md`, `docs/HANDOFF.md`. Nothing in
`lg_worker.py`, `diff.py`, `engine.py` or the warm servers.

## 5. Build plan

### 5.1 New `runner/lg_openapi.py` (target side; standard library only)

Run as `[py, "-B", lg_openapi.py, request.json]`, under `os_sandbox.wrap(cmd, py, writable=[tempfile.gettempdir(), tmp]
+ ([cwd.path] if cwd.strategy != "off" else []), readonly=[cfg.root, tree], mode=behavioral_diff.os_sandbox)`, `env =
clean_env(cfg)` minus `PYTHONPATH` (so `PYTHONHASHSEED=0`), cwd = `scratch.current(cfg, tree)` — exactly
`migration_drift.run_tree`'s lines (guardlib/migration_drift.py:124-149). Request: `{root, sys_path, protect, cwd,
cwd_root, scratch, app, io, deterministic: true}`.

`main(request_path) -> dict`, printing one `LGOAS{json}` line on `sys.__stdout__` (project prints go to devnull):

1. `sys.stdout = open(os.devnull, "w")`; `root_real, _ = lg_worker.prepare(request, with_django=False)`.
2. `lg_worker.reset_determinism()` (seeds `random`, so a `random.random()` default is the same in every process).
3. `with lg_worker.setup_phase(): app = lg_worker.load_app(request["app"])`. Any exception: `{"error": "import",
   "detail": "<Type>: <msg>"[:400]}`.
4. `fn = getattr(app, "openapi", None)`; not callable: `{"kind": "none"}` (Flask, Starlette: not an error).
   `with setup_phase(): spec = fn()`; not a dict, or no `"openapi"`/`"paths"` key: `{"kind": "none", "detail": "openapi()
   returned <type>"}`.
5. Canonical text: `text = json.dumps(json.loads(json.dumps(spec, allow_nan=False, default=_default)), sort_keys=True,
   separators=(",", ":"))`. The inner round trip turns int keys into strings (so `sort_keys` cannot meet mixed key
   types); `_default` tries `fastapi.encoders.jsonable_encoder(obj)` only when `fastapi` is already in `sys.modules`,
   else raises `TypeError`. `ValueError`/`TypeError` (NaN, an unencodable value): `{"error": "encode", ...}`.
6. `files` = sorted real paths of `getattr(m, "__file__", None)` for every module in `sys.modules` whose real path is
   under `root_real` (the D6 trigger set); `app_file` = the real path of the app's module (`sys.modules[module].__file__`
   for the `module` part of `app:`).
7. Versions: `importlib.metadata.version` of `fastapi` and `pydantic` (None when missing).
8. Out: `{"kind": "fastapi" if "fastapi" in sys.modules else "openapi", "text", "sha": sha256(text)[:16], "bytes",
   "files", "app_file", "versions", "blocks": list(lg_worker.BLOCKS), "error": None, "seconds"}`. Any `BaseException`
   at the top: an `error` result, still one line. `lg_worker.drop_scratch()` at the end.

A result is **complete** when `error` is None, `blocks` is empty and `kind != "none"` (as `migration_drift.complete`).

### 5.2 `guardlib/contract_tools.py`

- `settings`: `out.setdefault("openapi_from_app", "auto")`; `config.DEFAULTS["contract_tools"]` gains the same key (D8).
- `oasdiff_breaking(..., internal_only=False)`: with `internal_only=True`, any `$ref` whose part before `#` is not empty
  is a `Refusal(..., "refused")` ("the app's generated spec refers to <ref>; LegacyGuard gives oasdiff only specs with
  internal references") before anything is copied or run, so `--allow-external-refs=false` is always passed and the
  sandbox branch is never taken. `trees` is then unused (pass `(tmp, tmp)`). The file name `rel` is
  `"openapi-from-app.json"` (inside the temp folder only).
- `oasdiff_breaking(..., label=None)`: every Refusal text that names `rel` uses `label or rel` instead, and the code-first
  caller passes `label="the app's generated spec"` (so a timeout reads "oasdiff took longer than 20 s on the app's
  generated spec", never the temp file name). Part A callers pass nothing and are unchanged.
- `report_line`: adds "app OpenAPI: FastAPI <v>, <n> runs" from `data["contract_tools"]["app_openapi"]`, or "app
  OpenAPI: not available for this app (no openapi() method)" when the last result was `kind: none`.

### 5.3 New `guardlib/app_openapi.py` (guard side)

- `enabled(cfg)`: `cfg.raw.get("app")` set and `openapi_from_app` not false (`False`, "false", "off", "no").
- **Trigger** `applies(cfg, rel, state, data) -> bool` (D6). `state` is session.json `app_openapi` (read in the first
  lock block): `{key, sha_a, sha_b, stable, kind, base_files, work_files, app_file, failed: {attempts, detail,
  paused}}` or None; `data` is the snapshot taken in the same block (`{"baseline_commit": ...}`, as post_edit's
  `data_snapshot`), passed explicitly so `cache_key(cfg, data)` can be computed outside the lock.
  - False unless `enabled` and `engine.is_guarded_source(cfg, rel)`.
  - `state` is None, or its `key` differs from `cache_key(cfg, data)`: **True** (the run builds the cache). This covers
    any layout, e.g. `app: "main:app"` with a top-level `models.py`.
  - `state["failed"]["paused"]` with the current key: **False** (D10; the baseline could not be built in two checks).
  - `state["failed"]` with the current key and not paused (one failed attempt, nothing cached): **True** (the second
    attempt).
  - `state["kind"] == "none"` (no `openapi()`, e.g. Flask): False.
  - Else True when `rel` is in `base_files ∪ work_files` (repo-relative) or equals `app_file`. `work_files` is replaced
    by every complete working-tree run, so a module created in the session and imported by a later edit (the check runs
    on that edit; the new module enters `work_files`) triggers on its own later edits.
- `run_tree(cfg, tree, timeout) -> dict | None`: the 5.1 run, parsed; None on an internal failure; `{"error":
  "timeout"}` on a timeout.
- `cache_key(cfg, data)`: JSON of `baseline_commit`, `warm._generation(cfg)`, `app`, the target interpreter's realpath and
  `st_mtime_ns`, `behavioral_diff.io`, `source_roots`, `behavioral_diff.env` (as `migration_drift.cache_key`, plus
  `app`). Versions are known only after a run: if the working tree's `versions` differ from the cached ones, the
  baseline is rebuilt.
- **Baseline cache (M3).** Texts and the old-vs-old change list live in `.legacyguard/app_openapi_base.json`:
  `{key, a: {text, sha, files, app_file, versions, kind}, b: {...} | null (when equal to a), noise: {changes:
  [{id, operation, path, text, level}], via: "oasdiff"|"parser"} | null}`, written atomically (tmp + `os.replace`)
  outside the ledger lock. session.json `app_openapi` holds only the small summary used by `applies` and the key/shas
  that prove the file current; a missing file, a key or sha mismatch, or unreadable JSON means "not cached". Only a
  **fully built** baseline is cached: both builds complete (or a complete `kind: none`). `noise_pending: true` in the
  cache file marks a baseline whose noise run did not finish (deadline or tool failure, 5.6): the next check computes
  the noise list before comparing. `ci.run` does not cache.
- **Failure memory (round 2, D10).** A check whose baseline was not fully built writes, under the current key, only
  `state.failed = {attempts: n + 1, detail, paused: n + 1 >= MAX_BASE_ATTEMPTS}` with `MAX_BASE_ATTEMPTS = 2` (no spec
  text, no files). One attempt is one check (its internal retry of a missing build included). A different key (new
  baseline commit or generation, `app`, interpreter, `io`, `source_roots`, `env`) starts again at 0. When the second
  attempt fails: if one build completed in **both** attempts and they are equal, that text is cached with
  `stable: None` and the check goes on (breaks hedged as "built once"); otherwise `paused`, and the note (kind
  `app-openapi-paused`, once per session) says: "LegacyGuard: the app's OpenAPI spec could not be built in the
  baseline in 2 checks (<detail>), so the code-first schema check is paused for this session. It restarts when the
  baseline, the `app` setting, the Python interpreter or the worker settings change." The Stop report line says
  "app OpenAPI: paused (<detail>)".
- `compute(cfg, base_root, work_root, state, data, deadline) -> SpecOutcome` with `SpecOutcome(state:
  "checked"|"not_checked"|"hedged"|"none", breaks: list[(file, Break)], hedged: list[(file, Break)], warns, note,
  note_kind, cache_write, summary, runs, versions, seconds)`. `deadline` is an absolute time (5.6); every subprocess and
  oasdiff timeout is `min(timeout_s, deadline - now)`, and a stage that would start with under 1 s left is "not
  checked: the time budget for this check ran out". Whole body in `try/except Exception` -> `not_checked` "internal".
  1. **Baseline.** From the cache when current. Else run base A, base B and the working tree in parallel
     (`ThreadPoolExecutor(3)`). If exactly one base build is complete, run the missing one **once more** (sequentially).
     Outcomes:
     - both complete: `stable = (a.text == b.text)`; cacheable.
     - still only one complete: use it with `stable = None` for this check only; **not cached**, one failed attempt
       recorded (failure memory above); every break of this check is hedged ("the baseline spec was built only once").
     - none complete: `not_checked`, note "the app's OpenAPI spec could not be built in the baseline (<detail>); schema
       changes were not checked" (kind `app-openapi-base-<error>`); one failed attempt recorded; nothing cached.
  2. **Noise list**, only when `stable is False` and not cached yet: oasdiff (or the parser, 5.4) on `a.text` vs
     `b.text`, once; its full change list (ERR and WARN; the parser's breaks) goes into the cache file as `noise`.
  3. **Working tree** (from step 1's parallel run, else one `run_tree`). `kind: none` in base and work: `state "none"`,
     no note, cached. Not complete: `not_checked`, note "the app's OpenAPI spec could not be built in the edited code
     (<detail>)" (kind `app-openapi-work-<error>`); an import error in the edited code is also reported by the
     behavioural diff as `broken` when a function target exists. Complete: `summary.work_files` = its `files`.
  4. `work.text == a.text`: `checked`, no breaks, no tool run.
  5. Else `res = contract_tools.oasdiff_breaking(cfg, "openapi-from-app.json", a.text, work.text, (tmp, tmp), timeout,
     specs=(json.loads(a.text), json.loads(work.text)), internal_only=True, label="the app's generated spec")`.
     - `OasResult`: each ERR -> `Break("http", "app-oas:" + item, subject, "app-openapi", file)` with `item, subject =
       contract_tools.break_fields(...)`; WARN texts to `warns`.
     - `Refusal` (missing, refused, failed, timeout): the parser fallback (5.4), plus the Refusal's note under the
       existing Part A note kinds (`oasdiff`, `oasdiff-refused`, ...), so "oasdiff was not found" is said once per
       session in total.
  6. **Noise filter (D4, corrected).** With a `noise` list: an old-vs-new break whose `(id, operation, path, text)`
     (parser: its item) is in `noise` is **dropped**; a break that only shares `(operation, path)` with a WARN or ERR
     entry of `noise` (an empty operation and path match only each other) is **hedged** ("the baseline's generated spec
     also changes between two builds at <operation path>, so this may not come from the edit"). Every other break
     **stands**: it wakes and becomes a finding as if the spec were stable. With `stable is None`: every break hedged
     (step 1). `state` is `checked` when some break stands or none is left, `hedged` when all left are hedged.
  7. `file` for every break: the repo-relative `app_file` of the working tree when inside it, else `"app:<app ref>"`.
     `code_hash = symbols.sha(item)`.
- `context_text(outcome) -> (text, kind)` and `wake_items(outcome, seen) -> list[item]`: the wake list is the standing
  breaks whose items are not in `seen`. Wake text, exactly:
  `LegacyGuard: the OpenAPI spec this app generates (app: <ref>, built in the baseline and in the edited code) changed in
  ways oasdiff reports as breaking: <subject 1>; <subject 2>; ... (+N more). Clients written against the old spec may
  fail. If the change was intended, say so; the developer can waive it with /legacyguard:waive.` (parser fallback:
  "...in ways the built-in parser reports as breaking (oasdiff not available): ..."). Hedged breaks: the same lines as
  context with their hedge. `warns`: one context line "oasdiff also reports N possible breaking changes it cannot
  confirm in the app's generated spec: <first 3>". `not_checked`: the note (once per session through `take_notes`). It
  names what was compared and never claims more.
- `record_run(data, outcome)`: `data["contract_tools"]["app_openapi"] = {runs (+=), versions, kind}`; `data["app_openapi"]`
  = `outcome.summary` when given.

### 5.4 Parser fallback on generated specs (D7)

`a, b = cp.http_items_from(base_dict), cp.http_items_from(work_dict)`; keep `field:` items only for schema names present
in **both** dicts' `components.schemas`; `checks._http_breaks("openapi-from-app.json", a', b')`; items prefixed
`app-oas-parser:`; source `app-openapi`; subjects as the parser's ("response field dropped: Note.body"). Route and
parameter removals stay in (they are real; the static route check also reports a removed route in the handler's file,
which the D5 dedupe does not merge, documented). **Never silent:** when schema names were present in only one side, a
context note (and a report note at Stop, `out.notes` in CI) says "the built-in parser did not compare the fields of N
schemas whose names changed in the generated spec (<old names> -> <new names>, first 3), for example when a second
model of the same name appears" (kind `app-openapi-renamed`, once per session).

### 5.5 Hooks

**PreToolUse: never** (it would run project code on an edit not yet applied).

**PostToolUse** (`scripts/post_edit.py`), next to the drift branch:
- Inside the first lock block (25-34): also copy `oas_state = data.get("app_openapi")`.
- Outside the lock: `oas_wanted = app_openapi.applies(cfg, rel, oas_state, data_snapshot)`; line 37 becomes `if not targets and not
  drift_wanted and not oas_wanted: return None`.
- Inside the `for_trees` block, after drift and before `differ.run`: `oas = app_openapi.compute(cfg, baseline_dir, root,
  oas_state, data_snapshot, deadline=start + SPEC_BUDGET_S)` in `try/except Exception` -> `log_error`, `oas = None`. The
  cache file (M3) is written right after `compute`, outside the lock. No subprocess under the lock.
- After the newer-edit check (56-57, applied to this too), in the **same store lock block** as drift (60-72), and
  **outside** its `if targets:` branch:
  - `record_run(data, oas)` (this also stores `oas.summary`, including the failure memory of 5.3, whether or not the
    baseline was built); not-checked, paused and fallback notes through `take_notes`;
  - `oas_wake = [i for i in app_openapi.wake_items(oas, data.get("app_openapi_seen") or [])]`, computed **from the
    session.json read under this lock**, so of two overlapping hooks only the first to store wakes; the items are
    appended to `data["app_openapi_seen"]` there;
  - `if oas_wake: data["wakeups"] += 1` (also when `targets` is empty; the existing `+= 1` for the diff's wake stays
    inside `if targets:`, and when both wake it is one wake: add 1 once, `if wake or oas_wake`).
- Output, with the spec wake decided **before** the no-targets return at line 82:
  - `oas_wake` non-empty: `raise Wake("\n\n".join(texts + [oas_text] + [x for x in (drift_text, skipped_text) if x]))`
    (texts are the diff's and affected tests' wakes, empty when there are no targets). This new block goes between line
    81 (`skipped_text = ...`) and line 82 (`if not targets:`).
  - otherwise the spec text joins the context note like `drift_text`, both in the no-targets branch and in the
    no-wake branch.
- No finding at PostToolUse (Stop writes them), as drift.

**Stop** (`guardlib/audit.py` `run`, 184-201), inside the existing `for_trees` block, after `_drift`:
`oas = _app_openapi(cfg, data, changed, log)` (None when no changed file `applies`; else `compute` with the session
state and the cache file, `record_run`). `_contract_findings(..., drift=drift, oas=oas)`; a new
`_oas_findings(cfg, data, oas, seen_items)` before the `not_applied` sweep, a copy of `_drift_findings` (69-92) with
source `app-openapi`, `extra={"contract_kind": "http"}`, evidence "<subject>; found by comparing the OpenAPI spec the
app generates in the baseline and now (oasdiff|built-in parser)". **Lifecycle (round 2; `_oas_findings` does not copy
`_drift_findings`' "known finding: continue", audit.py:84-88):**
- A reported **standing** break: new -> status FAILURE, state `open` (blocks the Stop once, M1). Known with the same
  `(file, item)`: status set to FAILURE; state `recorded` or `not_applied` -> `open`; `open`/`applied` unchanged;
  `waived` unchanged. So a break first written hedged that later stands is upgraded and blocks once (its id is not in
  `stop_blocked_for` yet).
- A reported **hedged** break: new -> status RISK, state `recorded`, `extra.hedged = True`, evidence with the hedge
  (never blocks, M2). Known: status set to RISK, `extra.hedged = True`, state `open`/`applied`/`not_applied` ->
  `recorded`; `waived` unchanged. So a standing finding that becomes hedged stops blocking.
- Every reported item joins `seen_items`.
- **Sweep for this source:** after the above, every `app-openapi` finding in state `recorded` whose `(file, item)` is
  not reported in this run goes to `not_applied` (the generic sweep, audit.py:64-67, handles `open`/`applied` only).
  This sweep also runs when `oas` is None because no changed file applies (e.g. the rename was reverted). It does
  **not** run on `not_checked` or a paused check.
- State `not_checked`, or the check paused (D10) while `app-openapi` findings exist: every `open`/`applied`/`recorded`
  `app-openapi` finding joins `seen_items` and none is swept (left unchanged). At Stop, `_app_openapi` returns a
  `not_checked` outcome with the paused note (not None) when the check is paused and some changed file is a guarded
  source; None means only "no changed file could affect the spec". D5
dedupe: a code-first break whose `(id, operation, path, text)` matches a `contract_break` with source `oasdiff` added
or seen in this run is skipped, and that finding's evidence gains "also in the app's generated spec" (once). Notes go
into `_report_notes`.

**CI** (`guardlib/ci.py` `run`): after `_drift`, inside the same `for_trees` block, `_app_openapi(cfg, out, base_dir,
root, changed)`: runs **once** when `enabled` and any file in `changed` is a guarded source (D6; no membership test,
no cache; the baseline is still built twice and the noise list computed when needed). Standing breaks after
`waivers.is_waived(cfg, "contract_break", file, b.item, sha(item))` into `out.breaks`; hedged breaks and every note into
`out.notes`; the D5 dedupe against `out.breaks` from oasdiff on the static file. Annotations are the existing
`::warning file=<app file>,title=LegacyGuard contract::...` (ci.py:235-239); an `app:` pseudo-file (`app:<ref>`) is
written without `file=`, as `django-app:` is. Only `fail_on == "any"` counts them (M4).

### 5.6 Time budget and fail-open

- **One deadline for the spec stage:** `SPEC_BUDGET_S = 45` per hook run, from the start of `compute` (5.3). Inside it,
  every process and oasdiff run uses `min(timeout_s, time left)`.
- **Worst case per PostToolUse check, before `differ.run`:**
  - first qualifying check of a session (baseline not cached): the three builds in parallel (<= 20 s), the retry of a
    missing base build (<= 20 s), the noise oasdiff run (<= 20 s) and the old-vs-new oasdiff run (<= 20 s): 80 s
    without the deadline, **capped at 45 s** with it;
  - later checks: the working-tree build (<= 20 s) and one oasdiff run (<= 20 s): <= 40 s;
  - plus Part A drift when a models file was edited: `migration_drift.compute` runs work and base **serially**, 2 x 20 s
    = 40 s (the base is cached after its first complete run, so later checks pay <= 20 s).
  - Sum: first check <= 45 + 40 = 85 s, later checks <= 40 + 20 = 60 s, against the hook limit of 150 s, leaving at
    least 65 s for `differ.run` and the affected tests, whose own limits (5 s per call, 10 s per function, at most 12
    targets) can exceed that on a large edit. If Claude Code kills the hook at 150 s the result is lost, not wrong (the
    hook fails open; the Stop audit runs the check again). Typical fixture cost: about 0.2 to 0.3 s per check.
- A deadline hit during the noise run (or a noise run that fails) still caches the fully built baseline, with
  `noise_pending: true` and no noise list; that check reports `not_checked` ("the time budget for this check ran
  out") and the next qualifying check computes the noise list first. A deadline hit while building the baseline counts
  as one failed attempt (D10).
- Fails open everywhere: every function in `app_openapi` returns an outcome or None and never raises; hook call sites
  are wrapped; a failed or not-checked run never adds or removes a finding.

## 6. Tests (run one file at a time, never in parallel)

Two modules:
- `tests/test_app_openapi.py` (target-side, real FastAPI apps): skipped as a module when `TARGET_PY -c "import
  fastapi"` fails. Test 9 also needs Flask: `pytest.importorskip("flask")` inside it (in the target interpreter check).
- `tests/test_app_openapi_guard.py` (guard side only, `run_tree` and `oasdiff_breaking` monkeypatched with canned
  results; needs no FastAPI): tests 7b, 7c, 11b, 11c, 10a to 10d, 13, 14, 18, 20, 21.

**Skip counts:** with `requirements-dev.txt` installed (as CI's 3.10, 3.12 and 3.13 jobs do, `.github/workflows/tests.yml`
line 13; FastAPI and Flask are in it) every new test runs. In the local 3.10 venv as it is today (no packages; the
HANDOFF says to install `requirements-dev.txt` first) the target-side module adds one module-level skip to the current
ones (reviewer's count: 2 -> 3, and one more if Django is also missing, which it is today); the guard-side module runs.

Helpers: `write_tree`, `make_cfg` (conftest), the session fixture shape of `tests/test_migration_drift.py` (baseline
under `cfg.state_dir/baseline`, `baseline_ok` in the ledger). Fixture `svc/__init__.py`, `svc/models.py` (`class
Note(BaseModel): body: str; n: int = 0`), `svc/app.py` (`app = FastAPI(title="t", version="1")`, `POST /notes` taking and
returning `Note`), `tools/script.py` (not imported by the app); config `app: "svc.app:app"`.

`tests/oasdiff_fake.py` gains two modes. Both read the two JSON files from argv and record argv as today.
- `diff` (**mirrors oasdiff's levels as read in rules.go**): per schema present in both `components.schemas`, a removed
  property is `{"id": "fake-property-removed", "text": "property '<p>' removed from <schema>", "level": 3}`; a changed
  `default` is level **1** (INFO, as `ResponsePropertyDefaultValueChangedId`); a changed `operationId` is level 1 (as
  `APIOperationIdRemovedId`/`AddId`). `breaking` prints WARN and up, so the fake prints only levels >= 2: an all-INFO
  diff prints `[]`.
- `diff-loud`: the same, but a changed `default` is level 2 with `operation`/`path` set to the first path whose request
  body refers to the schema. **A stand-in** to exercise the hedge path; real oasdiff gives INFO for a default change.

1. `run_tree` on the fixture: complete, `json.loads(text)["paths"]` has `/notes`, `files` contains `svc/models.py` and not
   `tools/script.py`, `blocks == []`, the tree's file listing (names, sizes, mtimes) is unchanged afterwards; two runs
   give the same `sha`.
2. Hash seed: add `api_route("/multi", methods=["GET","POST","PUT"])`; two `run_tree` calls give the same text
   (`clean_env` sets `PYTHONHASHSEED=0`).
   - 2b (reviewer's optional item): `behavioral_diff.env: {PYTHONHASHSEED: "random"}` and a route with two methods and a
     body (so a `Body_*` name and the operationId follow set order): `compute` over several runs, fake `diff` (operationId
     change INFO): never a standing or hedged break for an unchanged tree.
   - 2c: reordering `methods=["GET","POST"]` to `["POST","GET"]` in the decorator under seed 0 changes the operationId
     (a real spec change, INFO in oasdiff): with fake `diff`, no break and no wake.
3. Rename `body` -> `text`, fake `diff` on PATH: `post_edit.handle` raises `Wake` whose text contains "property 'body'
   removed from Note" and "app: svc.app:app", **with no function targets in the edit**; `wakeups == 1`; the fake's argv
   has `--allow-external-refs=false` and two files ending in `openapi-from-app.json`; `app_openapi_seen` holds the item.
   A second edit to `svc/models.py` (a comment) and one to `tools/script.py` do not wake again.
4. A comment-only edit to `svc/models.py` after the cache exists: texts equal, the fake is not called (`calls.jsonl`
   empty), no note.
5. Import-time noise, `Note` gains `when: datetime.datetime = datetime.datetime.now()` in **both** trees:
   - 5a (correction 1): plus the rename in the working tree, fake `diff`: the cached baseline has `stable False` and an
     **empty** noise list; the rename **wakes** at PostToolUse and becomes a FAILURE finding at Stop.
   - 5b: fake `diff-loud` (stand-in WARN at `POST /notes`): the rename's break at `POST /notes` is hedged: context note,
     no wake, a RISK `recorded` finding with `hedged` at Stop (M2); a break at another operation (add `GET /other`
     returning a second model with a removed property) still wakes.
   - 5c: the noise oasdiff run happens once per session (count fake calls with two base files across three checks).
6. No oasdiff on PATH: the rename gives the parser break "response field dropped: Note.body" (source `app-openapi`) and
   the "oasdiff was not found" note once per session even across the static and code-first checks. Duplicate names: a
   working tree that adds `svc/other.py` with a second `Note` model and a route using it gives **no** field-dropped break
   and **does** give the note "did not compare the fields of 1 schemas whose names changed ... (Note -> svc__models__Note
   ...)".
7. Baseline cannot import (the baseline's `svc/models.py` raises at import): `not_checked`, note "could not be built in
   the baseline", no wake, no finding; at Stop an existing open `app-openapi` finding stays open.
   - 7c (guard module, round 2): base builds fail (`{"error": "import"}`) on every call, four guarded edits in a row
     (including `tools/script.py`): `run_tree` on the baseline root is called in the first two checks only (at most 2
     builds plus 1 retry each), the third and fourth edits call nothing; the paused note appears once; a Stop audit
     then gives a `not_checked` outcome and sweeps nothing; changing `behavioral_diff.io` (a new key) runs the check
     again. Variant: build A succeeds with the same text in both attempts and B fails: no pause, the second check
     caches A with `stable: None` and hedges.
   - 7b (guard module, correction 5): base A complete and base B `{"error": "import"}` twice: the retry happens once,
     this check's breaks are hedged, nothing is cached; at the next qualifying check both builds succeed, the cache is
     written, and the same rename wakes.
8. Working tree cannot import: `not_checked` with "in the edited code", no wake from this check.
9. Flask app (`app = Flask(__name__)`; importorskip flask): one run per session (`kind: none` cached), no note, the
   report line says "not available"; the fake is never called; a second edit runs nothing.
10. Trigger (D6, guard module with `run_tree` counted, except 10c):
    - 10a: no cache, edit to `tools/script.py` (guarded, not imported): the check runs once and caches; a second edit to
      it runs nothing. Edits to a test file or with `openapi_from_app: false` never run it.
    - 10b: top-level-module app `app: "main:app"` with a top-level `models.py`: the first edit to `models.py` runs the
      check (no cache); after that, `models.py` is in `base_files` and triggers.
    - 10c (target module, CI path): `ci.run` on a base/head pair for the `main:app` layout with the rename in
      `models.py` (outside any package) reports the break.
    - 10d: new file: Claude creates `svc/schemas.py` (not imported: no run after the cache exists), edits
      `svc/models.py` to import it (runs; `work_files` now has `svc/schemas.py`), then edits only `svc/schemas.py` (runs).
11. Stop: after the rename, `audit.run` adds a `contract_break` with source `app-openapi`, status FAILURE, `file ==
    "svc/app.py"`, `code_hash == sha(item)`, and the audit returns it as blocking once (M1); a second audit keeps it
    open; reverting the rename sweeps it to `not_applied`; `run_tree` monkeypatched to return None keeps it open; a
    waiver on it holds across an unrelated edit.
11b. Hedged lifecycle (guard module, canned outcomes): a hedged break at Stop is a RISK `recorded` finding; reverting
    the rename (no changed file applies) sweeps it to `not_applied`; a `not_checked` Stop leaves it `recorded`.
11c. Status changes (guard module): the same item reported hedged at one Stop and standing at the next becomes FAILURE
    `open` and is returned as blocking once; reported standing then hedged, it becomes RISK `recorded` and is no longer
    returned as blocking; a waived item stays `waived` in both directions.
12. CI: `ci.run` with the rename puts `("svc/app.py", Break(...))` in `out.breaks`; `annotations` gives a `::warning
    file=svc/app.py` line; `exit_code` is 1 only with `fail_on="any"` (M4); a waiver hides it; a failed run gives a
    note and no break.
13. Cache: a second qualifying PostToolUse check runs only the working tree (count `run_tree` calls per root); changing
    `app` in the config, or a different `versions["fastapi"]` reported by the working tree, rebuilds the baseline; a
    missing or corrupt `app_openapi_base.json` with a current session key rebuilds it; session.json holds no spec text.
14. D5: a static `contracts.http` plus the code-first spec, canned oasdiff results: a code-first break with the same
    `(id, operation, path, text)` as a static one is dropped and the static finding's evidence ends with "also in the
    app's generated spec"; a second code-first break at the same `POST /notes` with a different `text` (another property
    removed) is kept as its own finding.
15. Refs: an app whose `openapi()` is replaced to return `{"$ref": "other.json#/X"}` inside a schema: the fake is not
    called, the refusal note names "the app's generated spec", the parser result stands.
16. Encoding: a replaced `openapi()` returning a `Decimal` value is encoded (`jsonable_encoder`) and compared; one
    returning `float("nan")` gives `not_checked` "encode"; one returning int status-code keys is compared without error.
17. Timeout: an app module that sleeps 30 s at import with `timeout_s: 2`: `not_checked` "timed out" within 5 s; the
    note says "timed out", never the temp file name.
18. Lock: `run_tree` monkeypatched to assert the ledger lock is free (`LOCK_NB`), as Part A test 24.
19. Timing: print seconds per tree and the whole PostToolUse check; assert `< 10 s` only; record the numbers in
    `docs/PROGRESS.md`.
20. Overlapping hooks (guard module, correction 3): two `post_edit.handle` calls for the same rename whose `compute`
    results are both ready before either stores (barrier in a monkeypatched `compute`): exactly one raises `Wake`, the
    other returns a context note; `app_openapi_seen` holds the item once; `wakeups == 1`.
21. Deadline (guard module, correction 4): `run_tree` monkeypatched to sleep past a 2 s `SPEC_BUDGET_S`: `compute`
    returns `not_checked` "time budget" within 3 s and nothing is cached.

The existing `tests/test_contract_tools.py`, `tests/test_pre_edit_oasdiff.py`, `tests/test_migration_drift.py` and
`tests/test_checks.py` must pass unchanged (the new fake modes are additive).

## 7. Acceptance criteria

1. The `body` -> `text` rename in a model-only edit (no function targets) wakes Claude once at PostToolUse with the
   factual text of 5.3 (test 3), also when the baseline spec is unstable only through INFO-level noise (test 5a),
   becomes a blocking-once Stop finding (test 11) and a CI break (tests 10c, 12), with oasdiff (fake) and without it
   (test 6). Overlapping hooks wake once (test 20).
2. No tool run and no message when the generated specs are byte-equal (test 4); never a standing break from the
   baseline's own nondeterminism (tests 2, 2b, 5b); the noise list is computed once per session (test 5c).
3. A baseline that cannot be built costs at most two checks per session per cache key, then pauses with a note
   (test 7c); hedged findings are closed when no longer reported and change status with their hedge (tests 11b, 11c).
4. Fail open: import failures in either tree, timeouts, the spec-stage deadline, encode failures and internal errors
   give a "not checked" note, never a wake or a finding, and leave existing findings unchanged (tests 7, 8, 11, 16, 17,
   21); a half-built baseline is never cached (test 7b); the parser fallback names the schemas it did not compare
   (test 6).
5. Only `#/...` refs reach oasdiff, always with `--allow-external-refs=false`, with a temp cwd and no `OASDIFF_CONFIG`;
   notes never show the temp file name (tests 3, 15, 17).
6. Project code runs only under the worker set-up and the OS sandbox, never under the ledger lock, never in PreToolUse,
   and leaves the tree unchanged (tests 1, 18).
7. Trigger: the first guarded edit of a session, then only imported files (baseline or last working-tree run) or the
   app module; any layout, including a top-level module app and files created in the session (tests 10a to 10d); a
   Flask or Starlette app costs one run per session and no note (test 9).
8. Worst-case spec-stage time stays within `SPEC_BUDGET_S` (test 21; 5.6 sum documented).
9. Docs: CLAUDE.md "Sprint 7.6 Part B" section with section 8's limits; `docs/PROGRESS.md` timings; HANDOFF updated.

## 8. Known limits to document

- FastAPI-shaped `app.openapi()` only; Flask extensions, django-ninja, DRF and Starlette's docstring schemas are not
  read (D1).
- Mounted sub-applications and routes with `include_in_schema=False` are not in FastAPI's spec, so not compared
  (inference from `iter_route_contexts` and `include_in_schema`; not measured).
- A project that replaces `app.openapi()` is compared on whatever its function returns; one that caches to disk would
  write under the worker's rules (cache-like paths redirected, others refused: then "not checked").
- Import-time values in the spec (`datetime.now()` defaults) make the baseline unstable. A break is hedged (context
  note, non-blocking RISK finding) only when old-vs-old itself reports a WARN/ERR change at the same operation and path;
  the filter relies on oasdiff's levels as read in rules.go (default and operationId changes INFO), which were not run.
  Under the parser fallback, noise in a schema field type or name would be filtered by the same rule on parser items.
- Reordering `methods=[...]` in a decorator changes the operationId under seed 0 (FastAPI takes the first element of a
  set); this is a real change of the generated spec, INFO in oasdiff, so not reported.
- A baseline that cannot be built: with no cache every guarded edit qualifies, so the first two guarded edits of the
  session each pay up to `SPEC_BUDGET_S` (45 s) before the behavioural diff (three builds, one retry, before the
  deadline); then the check pauses for the rest of the session (D10) and later edits pay nothing. The paused note is
  shown once; the report line repeats it. A transient failure that clears later in the session is not retried until
  the cache key changes.
- pydantic renames a component when a second model of the same name appears, or when a model splits into `-Input` and
  `-Output`; the parser fallback then compares no fields of that model (D7), and oasdiff's handling of a pure `$ref`
  rename is unverified.
- The working tree is built once per check (only the baseline twice); a nondeterministic working tree with a stable
  baseline is not detected (inference: rare, since both trees run the same code paths at import).
- A wake can repeat a break the PreToolUse oasdiff check already asked about for a static spec file edited in the same
  session (D5 dedupes only at Stop and in CI).
- Waiver items differ between oasdiff and the parser fallback (as in Part A).
- The trigger set comes from the imports seen at spec build time (baseline and last working-tree run); a module imported
  only inside a function is not in it. The first guarded edit of each session runs the check whatever file it is.
- In CI the check runs on every pull request that changes a guarded `.py` (three app imports per run), with no cache.
- The worst-case PostToolUse sum (5.6) can still exceed the 150 s hook limit when a large behavioural diff follows; the
  hook is then killed and its result lost (fails open; the Stop audit repeats the check).
- Validation of this feature on real projects must not use held-out set 2's cases (it would tune on them).
- The real oasdiff binary was never run here (2.5).

## 9. What is unverified

- oasdiff: loading JSON files, run time on generated specs, the exact rule output for the rename, a default-value
  change, and a pure component rename (2.5). Tests pin only the fake.
- Old FastAPI versions with pydantic v1, and any FastAPI or pydantic other than 0.162.7 / 2.14.3; Python 3.10 (no
  FastAPI in that venv today; CI's 3.10 job installs `requirements-dev.txt`).
- The oasdiff rule levels behind the corrected noise filter (default value and operationId changes INFO) are read
  from `checker/rules.go` at v1.39.0, not observed.
- Linux bubblewrap for `lg_openapi.py` (only `sandbox-exec` was run).
- A real application's import and `openapi()` time (fixture only: 0.20 to 0.31 s per process).
- That `random`-seeding via `reset_determinism()` before import makes every random default repeatable (the measured
  variance was without it; the fix is planned, not measured).
