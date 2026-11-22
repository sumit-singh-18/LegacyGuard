# Sprint 7.2 plan: route-diff correctness (researcher, 2026-11-20, main at 3385224)

**Status:** reviewed; the user's decisions and the reviewer's corrections are folded in below (section 0). 7.2a (parts d, a,
b, e) is built; 7.2b (part c) is built and reviewed (PASS, round 2; see CLAUDE.md "Sprint 7.2b").

## 0. Decisions and corrections (these win over anything below)

User decisions (final):
- "Reached N of M" counts changed **statements**; request counts are shown in the text ("k of R requests reached them").
- Decorator edits are a separate `decorators_changed` kind, not part of the signature hash.
- Index introspection runs under the OS sandbox (option i).
- 7.2 is split: 7.2a = d, a, b, e (built in that order); 7.2b = c.
- Follow-ups, not built: "only compared inputs count" for functions and callers; harvesting literal test-client
  `headers=` (header names only in any printed text).

Reviewer corrections:
1. No "may come from elsewhere" hedge when a route's gate is its own handler's entry gate from `decorators_changed`
   (`Target.entry_gate`); narrowed in review round 1 (item 8). A1 asserts `unreached_note == ""`.
2. Rule order. When M > 0 the statement rule (N of M) decides the status; the early-4xx wording is only added to the text
   when N == 0. The same-early-4xx rule decides alone only when M == 0. Gate lines can run outside the handler (a
   dependency, a before-request hook, a caller in between) while `entered` is False, so "early" never decides when M > 0.
   Unit tests: `test_rule_order_statements_decide_when_there_is_a_gate`, `test_rule_order_early_4xx_decides_only_without_a_gate`.
3. `threading.settrace` only, never `settrace_all_threads`; the previous hook is restored in `finally`. D3 (sync, async
   and Flask endpoints) proves coverage.
4. (e) INTROSPECT gets the worker's set-up: `lg_worker.prepare` (blocks, a run scratch folder, the tree protected) and
   `setup_phase()`, so cache-like writes are redirected; any failure falls back to `from_decorators`. E5 covers a factory
   that writes a cache-like file.
5. Decorator-only path change with the same function name (`@app.get("/a") def h` to `@app.get("/b") def h`): when every
   compared request was answered 404 or 405 before any handler ran in the old tree and entered the handler in the new
   tree, the route is new in this edit; that is untested ("the route is new in this edit ..."), never a finding. A4 covers
   both index shapes: the session's (baseline table: `GET /a` is different, a real removal) and ci.run's (head table:
   `GET /b` is new).
6. A handler that itself answers 404 on every request (Flask `abort`, FastAPI `HTTPException` for an unknown id) with
   the changed line reached is `verified` (one Flask and one FastAPI test). A route gated on `callee_old_gate` uses the
   **old** tree's request gate, mirroring `gate_parts`.

Review round 1 (REVISE), fixed:
7. The new-route rule also needs the old 404/405 to come from routing: the worker reports per request whether a route
   of the app matched the method and path (`routed`). Flask: `app.url_map.bind("localhost").match(path, method)`
   (NotFound/MethodNotAllowed = unmatched; host matching = unknown). Starlette/FastAPI: the TestClient gets a thin ASGI
   wrapper that reads the `endpoint` the router writes into the scope; a mount whose router matched nothing leaves the
   mounted app there (unmatched), and a 405 with an endpoint is a partial match (unmatched). Chosen over the static
   decorator check because it sees prefixes, mounts and `add_url_rule` routes the decorators do not show. Unknown never
   fires the rule. Tests: a dropped `Depends(get_thing_or_404)` and a dropped 404-aborting Flask decorator are
   `different`; A4 still reads "new in this edit" in both index shapes.
   Round 2: Starlette's `routed` reads the router's own scope keys (Starlette 1.10.0 `Router.app` sets `router` before
   matching, `route` on a full or partial match): None when `router` is absent (the router did not run: a middleware
   answered first or passed a copied scope); False when the router matched nothing (no endpoint, or a mount whose router
   matched nothing) or the route's methods lack the request's method (partial); True otherwise, and for an endpoint
   without a route, None on 405. No status-based rewrite. Flask matches with `test_request_context(path,
   method=...).match_request()` without pushing the context (its own adapter, SERVER_NAME and subdomains). Tests: a
   dropped 405 dependency on a matched route is `different`; a middleware 404 before the router leaves `routed` None.
8. The hedge exemption is only for a route gated on its own handler's entry gate. Callers and routes reached through a
   caller keep the note: "never got past the changed decorators of f, or never called it, so this difference may come
   from those decorators or from elsewhere". Unit test: `test_hedge_kept_for_callers_and_caller_routes_with_an_entry_gate`.
   Optional: with M == 0 and entry unknown, a pass on non-4xx responses says "entry unknown" in its reason.

Optional items, done: leftover thread lines of a timed-out request never count for the next one (each frame records into
the bucket current when it started; threads alive at a timeout are not traced again); a bare factory name
(`pkg:create_app`) is called as Flask calls it, never handed to a test client; the `<locals>` qualname limit is named
(handlers defined inside `create_app` do not match symbols, so they get no route target, and their entry cannot be
traced); the (d) parser upper-cases methods; a decorator-only method edit is tested (`functools.lru_cache`); the
cross-thread trace cost is measured (about 1.2 ms per request on a trivial FastAPI endpoint, from about 1 ms to about
2.2 ms, so under 0.1 s for a route's 24 requests per tree). Routes that were "verified" on all-401 or all-422 responses
before 7.2a now show as untested; 7.3 should read that as intended.

Spec: the 7.2 row and the "Headline" defects in `docs/research/2026-11-sprint7.md`, plus the two ROADMAP 3d rules
(a route is not "verified" when every request returned the same early 4xx; the "reached N of M" rule).
Paths below are relative to `legacyguard/` unless they start with `tests/` or `docs/`.

## 1. The four defects, re-checked on 3385224

All four still hold. Sprint 6.8 and 7.4 did not touch the route path: 6.8e explicitly skips routes
(`diff._with_input_lines`, `_wants_second_pass`), and 7.4 only changed the worker's cwd (`indexer/routes.py` still runs in
the tree). Each was reproduced with a throwaway scratch test outside the repo (FastAPI 0.162.7, Starlette 1.10.0,
Flask 3.1.3 from `.venv-dev`). No repo file was changed.

| # | Defect | Evidence | Still holds? |
|---|---|---|---|
| 1 | Decorator-only edits are invisible | `guardlib/symbols.py:105-111`: `sig` and `body_hash` are taken from the `function_definition` node. Decorators belong to the parent `decorated_definition` (`_decorators`, 85-88). `symbol_diff.py:45-50` compares only those two hashes. Reproduced: adding `@require_token` gives `symbol_diff.diff == []` and `changed_targets == []`. | **Yes.** Correction to the research doc: `diff.same_code` (227-233) **already** compares decorators. It compares `ast.dump(FunctionDef)`, which has `decorator_list`, and both `ast` and tree-sitter put the function at its `def` line. Reproduced: `same_code(...) == False` for the edit above. It only needs a test. |
| 2 | Route targets have no line gate | `engine.route_targets` (`guardlib/engine.py:205-225`) builds `Target(..., "route", ..., requests=...)` with no gate fields. `diff.gate_parts` (667-681) and `_tree_specs` (691-698) handle only `target.via`. `lg_worker.run_route` (1415-1426) calls `_call_once(call, item, None, i)` with no trace and returns `"lines": None`. A 401 response is an `outcome: "return"`, so `inputs_completed > 0`, and `compare_one` falls through to `DONE` (643). Reproduced, case-D shape: a `Depends` raises 401 for every request and the handler's `400 -> 403` is never run. Result: the function target is untested ("reached 0 of 1"), but **`DELETE /things/{thing_id}` comes out `verified` on 4 requests**. | **Yes** |
| 3 | Flask `.route(` is not matched | `guardlib/checks.py:12` and `indexer/routes.py:13` match only `.get/post/put/patch/delete(`. `checks.ROUTE_DECORATOR.match("bp.route('/x', methods=['GET','POST'])")` is `None`. Effect: with an `app:` the index uses framework introspection, which is fine. Without one, `from_decorators` misses every `.route` handler. The PreToolUse static check (`check_python_source`, `_routes_of`) never reports "route removed" for `.route` handlers or for narrowed `methods=[...]`. | **Yes** |
| 4 | Renamed or deleted handlers get no route target | `diff.changed_targets` skips `renamed`/`visibility_changed` (189-190) and never sees `deleted`. `routes_diff.routes_for` matches only `(file, qualname)` of current targets. Reproduced, case-B shape (`@app.get("/timeline") def timeline` to `@app.get("/feed") def feed`): `changed == []`, `routes == []`. | **Yes** |

Two more findings from the same read:
- **The routes table belongs to different trees in different places.** In a session it is built at SessionStart
  (`scripts/session_start.py:127`), when the working tree equals the baseline, and is not rebuilt after edits. So it
  describes the baseline. `ci.run` (`guardlib/ci.py:109`), and therefore `tools/validate.py` and the 7.3 run, refreshes
  it on the **head** tree. The existing route tests do the same (`tests/test_routes_diff.py::build` indexes the edited
  tree). Part (c) has to take this into account.
- **(e) fails quietly today.** `indexer/routes.py:27` does `mod, attr = APP.split(":")`, then
  `getattr(module, "create_app()")`. That raises, so introspection returns None and the index falls back to decorators.
  `lg_worker._http_caller` (1387-1392) does the same `getattr` and gets `AttributeError`. Every route is then
  `untested: could not run: AttributeError ...`. A factory app is never diffed.

## 2. The route-diff path, end to end (as of 3385224)

1. **Routes table** (`indexer/routes.py`):
   - `refresh` calls `rebuild` when names changed or the table is empty.
   - `introspect` runs a `-c` script in the target Python, with `lg_worker.install_blocks()` only. There is no OS
     sandbox and no `setup_phase`, and the cwd is the tree.
   - It imports `module:attr`. For Flask it walks `url_map.iter_rules()` and `view_functions`. For Starlette/FastAPI it
     walks `app.routes`, using `effective_route_contexts`, nested `routes` and mounts. Django gives `ANY` rows.
   - Rows: `(METHOD, full path, handler qualname, handler file, "framework")`. HEAD and OPTIONS are dropped.
   - Fallback: `from_decorators`, the regex over the `symbols.decorators` column. It has no prefixes.
2. **Static check** (`checks.check_python_source`, PreToolUse):
   - `_routes_of` keys routes by `"{owner} {METHOD} {path}"` from the decorators.
   - Reports: route removed, a handler parameter removed (`Depends`/`Security` defaults excluded), response schema
     fields, and response dict keys.
3. **Targets** (`engine.targets_for`):
   - `diff.changed_targets` gives edited functions with `old_gate`/`new_gate` (`examples.gates`: a difflib diff of the
     function text, turned into statement ranges).
   - `dependents` adds callers, gated with `caller_gate`.
   - `route_targets` then gives one `Target(file=handler file, qualname="METHOD path", kind="route", requests=...)`
     per routes-table row whose `(handler_file, handler_qualname)` is a current target. That includes a dependent, so a
     route is also found through an edited function its handler calls.
   - `code_hash` is `sha("METHOD path|" + handler hash)`.
   - Placement: after own functions and open differences, before callers, within `MAX_FUNCTIONS`=12.
4. **Requests** (`runner/routes_diff.requests_for`):
   - Literal test-client calls in test files that name the path prefix (`examples.files_naming`), matched by
     `template_regex`, with `json=` and `params=` literals or a query string.
   - Then generated ones: path params from the handler's hints (`SAMPLES` per int/float/str/bool) and, for GET, each
     simple query param twice.
   - Deduplicated, at most `MAX_REQUESTS`=24. No headers and no cookies, so auth-protected routes get only
     unauthenticated requests. This is why case D is all 401.
5. **Specs** (`diff._run`, 1484-1491):
   - `{"module": app module, "qualname": "METHOD path", "kind": "http", "app": cfg app, "inputs": requests}`.
   - No mutants (`_with_mutants` skips routes), no input lines, no carving, no second pass.
6. **Worker** (`lg_worker.run_function` -> `run_route`):
   - `_http_caller` imports the app under `setup_phase()`.
   - Flask: `app.test_client().open(path, method=, json=, query_string=)`. The app is not in testing mode, so an
     exception becomes a 500 response.
   - Starlette: `TestClient(app, raise_server_exceptions=False).request(...)`, with no `with`, so lifespan does not run.
   - The outcome is `{status, content_type, body}` (JSON body, or text cut to 4000 characters), returned as a `return`
     outcome.
   - The app is built again for every route spec.
7. **Compare** (`diff.compare_one`): the same as for functions.
   - `different`: any compared request differs in status, content type or body (old A and old B must agree).
   - `untested`: blocked I/O, non-determinism, or no `return`. Routes rarely hit this, because errors become 500
     responses.
   - `verified`: anything else. With no gate parts nothing else is checked, which is defect 2.

**Starlette's TestClient runs the app on another thread**:
- Each request opens `anyio.from_thread.start_blocking_portal()` (`starlette/testclient.py:486-491`, 406-416 in the
  installed 1.10.0), so the app runs on the portal's event-loop thread.
- Sync endpoints are then sent to a worker thread through `run_in_threadpool` (`starlette/routing.py:55`,
  `fastapi/routing.py:138-139`).
- `sys.settrace` covers only the calling thread, so a route gate under `sys.settrace` alone would see nothing, and
  `test_unchanged_route_behavior_is_verified` would turn untested.
- `threading.settrace` covers threads started after it is set. A new portal per request means fresh threads, which it
  covers.

## 3. Plan

Proposed split:
- **7.2a (one pass):** (a), (b) with both rules, (d), (e).
- **7.2b (second pass):** (c), because it needs baseline routes in CI and in tests, which is new index plumbing.

(e) is small and self-contained, and it is needed before 7.3 if the Flask case app is a factory. If the reviewer
prefers, (e) can move to 7.2b instead of (c). The two are independent.

### (a) Decorators count as code

- `guardlib/symbols.py`: `Symbol.decorators_hash = sha("\n".join(decorators))`. `node_text`, `signature` and
  `signature_hash` stay as they are.
  - **Deviation from the research doc**, which said to put decorators in the signature hash.
    `audit._stale_callers` (`guardlib/audit.py:111`) and `pre_edit._risk_context` (`scripts/pre_edit.py:65`) treat
    `signature_changed` as "callers may break". Adding `@cache` or `@require_token` would then report stale callers,
    which is a new false risk finding.
  - `node_text` must also keep excluding decorators. `changed_targets` uses `_reads_any(s.node_text, changed_names)`.
    If decorators were included, every handler reading `router` in its decorator would become a target when
    `router = APIRouter()` changes to `APIRouter(prefix=...)`. That is case C: 8 targets per edit and a cut budget.
- `guardlib/symbol_diff.py`: after the signature and body checks, `elif s.decorators_hash != t.decorators_hash:
  Change("decorators_changed", ...)`.
- `runner/diff.changed_targets`: accept `decorators_changed` along with `body_changed`/`signature_changed`. `same_code`
  needs no change (it already compares `decorator_list`), only a test.
- **Gate for an edit that changed only decorators.** `examples.gates` returns empty ranges, because the bodies are equal.
  - New `examples.entry_gate(after_src, after_sym)`: the range of the first executable body statement in the new
    tree, skipping a docstring. A docstring constant gives no line event.
  - `changed_targets` uses it when both gates are empty and the decorators differ.
  - Meaning: "some input got through the decorators into the body". It applies to function targets and, through (b),
    to route targets.
  - Reason text when it is not reached: "no input got past the changed decorators into the body (line N); reached 0
    of 1".
  - Limit: a wrapper without `functools.wraps` makes `_span` miss the body, so the result is untested, never a
    false verified.
  - Built: `Target.entry_gate` marks such a gate; `code_hash` adds the decorators' hash when they changed.

### (b) Route targets are gated on the handler's changed statements

- **`guardlib/engine.route_targets`.** Keep `by_key = {(file, qualname): t}` for non-route targets. For each routes
  row, `h = by_key[(file, qualname)]`. The gate fields come from:
  - `differ.caller_gate([h])` when `h` is itself edited. This gives its `new_gate`, or its `old_gate` when the edit
    only deleted code. For a decorator-only edit it gives the entry gate from (a).
  - `h`'s own `callees`/`callee_file`/`callee_new_gate`/`callee_old_gate` when `h` is a dependent (`h.via`). The route
    is then judged by the edited function its handler reaches, exactly like the caller.
  - A route gated on `callee_old_gate` (an edit that only deleted code) is judged by the **old** tree's request gate,
    and `callee_new_gate` by the new tree's, mirroring `gate_parts`.

  Also add `Target.handler = {"module": module_for(file, root)[0], "qualname": qualname}` (a new field). It is used for
  the entry trace below. Routes do **not** get `via`: `via` drives caller-only logic (carving `_carve_plan`,
  `unreached_note`, ledger "via" text, CI).
- **`runner/diff.py`.**
  - `gate_parts`: `if target.via or target.kind == "route":`.
  - `_tree_specs`: the same condition for adding `spec["gate"]`.
  - `_run` (route branch): add `"handler": t.handler` to the http spec.
  - `compare_one`:
    - `where` names the handler for routes ("of the handler remove_thing", or "of price_label" through a caller).
    - `untraced and (target.via or route)` returns untested ("could not be traced").
    - The route rules below come before the final `DONE`.
- **`runner/lg_worker.py`** (standard library only).
  - `run_route(spec, root_real)`. `run_function` passes `root_real`.
  - It resolves `spec["handler"]` with `_resolve` inside `setup_phase()`. If the handler is missing, the span is
    `None` and entry is unknown.
  - Then `span = _span(fn)`, `gate = _gate_spec(spec, root_real)`, and
    `trace = _tracer(span, hits, gate, gate_hits, per_call)`.
  - Extend `_tracer` with an optional `per_call_gate` (a one-item list holding a set), filled in `local_both` and
    `local_gate`, so each request's own gate lines are known.
  - Around the request loop: `threading.settrace(trace)` only (never `settrace_all_threads`, correction 3); the previous
    hook is restored in `finally`. Built as a separate `_route_tracer` (per-request buckets), leaving `_tracer` as it was.
  - Pass `trace` to `_call_once`, which already does `sys.settrace` for the calling thread, as Flask needs.
  - Output adds:
    - `"gate_lines"`: the union, as for callers;
    - `"request_gate"`: the gate lines of each request;
    - `"entered"`: for each request, whether any line of the handler ran (a bool, or `None` when the handler was not
      resolved);
    - `"lines"`: the sorted own hits.
  - The tracer's closures are shared across threads. They only add to sets, which is safe under the GIL. The cost
    stays with the handler's code object and gate-file code objects that overlap the gate (`touches_gate`).
- **Rules** (in `compare_one`, routes only). Some definitions:
  - **Compared request:** a request that passed today's drops (old A agrees with old B, no setup failure, not
    blocked).
  - **Reached N of M:**
    - M is the number of changed statements in the route's gate (`statements_total`, as today).
    - N is the number of those statements run by at least one **compared** request. It comes from `request_gate`
      restricted to `r.compared`, not from the union over all requests. This is stricter than the function rule, which
      uses the union (noted as a follow-up for functions).
    - New `Result.requests_reached` counts compared requests that ran at least one changed statement.
    - **Verified needs `M > 0` and `N == M`**, or the no-gate case below.
    - `N == 0`: untested, "none of the R requests reached the changed lines of <handler> (reached 0 of M changed
      statements); responses: 401 x4 in both versions".
    - `0 < N < M`: untested, "requests reached N of M changed statements of <handler>; line X never ran
      (k of R requests reached any)".
  - **Early response:** a request whose response was produced while no line of the handler's own code ran, in that
    tree (`entered[i] is False`). This covers framework validation (422), dependencies (401/403 from `Depends`, see
    FastAPI's "Dependencies" page), `before_request` hooks, decorators, routing (404/405) and middleware.
  - **Same early 4xx:** every compared request has the same status in old and new, `400 <= status < 500`, and is early
    in both trees.
    - Then the route is **untested**: "every request was answered 401 before the handler ran, in both versions
      (reached 0 of M changed statements)". The status counts are listed when there is more than one code.
    - This rule applies with or without a gate. With a gate it agrees with "N == 0" and only adds the status wording.
      Its real job is **routes with no gate**: handlers that are targets only because they read a changed module
      constant, which have empty gates (`changed_targets`, 192-198).
    - If entry is unknown (the handler could not be resolved), the rule falls back to status alone. The text then says
      "...; the handler could not be traced, so it may never have run".
  - **No gate (M == 0)** also needs at least one compared request that entered the handler in the new tree.
    Otherwise the route is untested: "no request reached the handler". This is the general form of the early-4xx rule
    and covers 404/405/3xx early answers.
  - Differences stay `different` whatever is reached. When `M > 0` and `N == 0`, `unreached_note` (today callers only)
    is set for routes too: "may come from elsewhere", **except** with an entry gate (correction 1).
  - **Rule order (correction 2):** M > 0, the statement rule decides; M == 0, the same-early-4xx rule, then the no-gate
    entry rule. The new-route rule (correction 5) comes before differences.
- **Reporting.**
  - `reached_text` also gives "k of R requests reached them" for routes. It appears in the pass note
    (`format_note`), the ledger evidence, the Stop report and the CI comment, all through the existing `reached_text`
    and `reason`.
  - New `Result` fields: `requests_reached`, `requests_entered`, and `status_counts` (`{"401": 4}`, old tree).
    `tools/validate.py` records these with the existing `statements_reached/total`, so 7.3 can say "D: untested,
    reached 0 of 1, 401 x4".
  - No change to wake text except the hedge note above.

### (c) Deleted or renamed handlers (7.2b)

- **Baseline routes, the same in session and CI.**
  - New table `baseline_routes` (same columns as `routes`) in `indexer/db.py`.
  - SessionStart: right after `refresh(cfg)`, copy `routes` into `baseline_routes`. The tree equals the baseline then.
  - `ci.run`: run `routes.introspect(cfg, root=base_dir)` and store the rows in `baseline_routes`. `introspect` gains a
    `root` parameter for `ROOTS` and the cwd.
  - Tests: a helper indexes the baseline tree the same way.
- **Lookup order for an old handler `(file, old_qualname)`:**
  1. `baseline_routes`;
  2. `routes` (a session index is the baseline);
  3. a static parse of the old function's decorators in the baseline file, using the shared parser from (d). This has
     no router or blueprint prefix, so its rows are marked `source "decorator"` and the reason says the path may lack
     a prefix.
- **Delete** (`symbol_diff` `deleted`, handler had baseline route rows):
  - One target per `METHOD path`, with requests built from the **old** symbol's params (`Change.before.params`). The
    index no longer has them in CI.
  - Gate: the old handler's whole body as `callee_old_gate`, traced in the old tree. "Reached" then means some request
    ran the old handler.
  - Expected: old 2xx/4xx against new 404/405, so `different`. The PreToolUse "route removed" break also fires.
  - If the new tree still serves the same path identically (the handler moved to another file), the route is verified
    only when the old handler ran and the responses agree.
- **Rename** (`renamed`/`visibility_changed`, and a delete plus add in the same file whose new function serves the same
  `METHOD path` in the new tree's decorators):
  - Target from the old row.
  - Gate: the entry gate of the **new** function (new tree), so a pass needs a request that ran the renamed handler.
  - A path change in the same edit (case B) gives a 404 in the new tree, so `different`.
- **New paths** (a route only in the new tree) get no target. Old 404 against new 200 is an addition, and additions
  are never findings. This is a follow-up if wanted.
- `code_hash` is `sha("METHOD path|deleted|" + old node_text)` or `"|renamed|"`. The target `file` is the handler's
  file.

### (d) Flask `.route` in both places (one parser, not two regexes)

- New `guardlib/route_decorators.py::parse(dec_text) -> list[(owner, METHOD, path)]`, used by `checks._routes_of` and
  `indexer/routes.from_decorators`. Both `ROUTE_DECORATOR` regexes are removed.
- It uses `ast.parse(dec, mode="eval")` and accepts a `Call` whose `func` is an `Attribute` with `attr` in
  `get/post/put/patch/delete` or `route/api_route`.
- The path is the first argument or `rule=`/`path=`, as a string literal.
- `methods=` must be a list or tuple of string literals.
- `route` with no `methods` is `["GET"]`: Flask's documented default, with HEAD and OPTIONS added automatically, which
  are dropped as today. Non-literal `methods` gives no rows (unknown), never a guess.
- Effects:
  - The "route removed" check now covers `.route` handlers.
  - Narrowing `methods=["GET","POST"]` to `["GET"]` reports "route removed: POST /x".
  - The static index fallback finds Flask handlers.
- `guardlib` already imports nothing heavy. `indexer/routes.py` already imports `guardlib.config`.

### (e) `app: pkg:create_app()` factories (worker only)

- New `lg_worker.load_app(ref)` (standard library), modelled on Flask's own `find_app_by_string`
  (`flask/cli.py:120-185` in the installed 3.1.3):
  - Split on the first `:`. The default attribute is `app`.
  - `ast.parse(attr, mode="eval")` must be a `Name` or a `Call` whose func is a `Name`.
  - Arguments and keyword arguments go through `ast.literal_eval`. The Flask CLI documents this rule: "their contents
    are parsed as Python literals". Anything else raises `ValueError("app ... must be name or name(literal args)")`,
    which is reported as untested with that text. No `eval`, no attribute chains, no starred arguments.
  - A `Call` runs the factory only if the attribute is callable. The factory is called inside `setup_phase()` in the
    worker, so the OS sandbox, the blocks, cache-write redirection and the run's scratch folder all apply. A factory
    that opens a database or the network gets `GuardBlocked`, giving `import_blocked` and untested with the reason.
  - Cache per process (`_APPS[ref]`): `run_route` builds the app once per worker or warm child, not once per route.
    Factories that register global metrics or extensions often fail on a second call.
- `_http_caller` calls `load_app`. `diff._run` keeps `app.split(":", 1)[0]` as the spec module.
- `indexer/routes.INTROSPECT` calls `lg_worker.load_app(APP)` under `lg_worker.setup_phase()` in place of `split(":")`.
  - Today's introspect runs outside the OS sandbox with blocks only, so a factory would run project set-up code there.
  - Two options:
    - (i) wrap the introspect command with `os_sandbox.wrap(..., writable=[tempdir], readonly=[cfg.root])` and run it
      in a `scratch` folder;
    - (ii) when `app` contains `(`, skip introspection and use the decorator fallback.
  - **Decided: (i)** (user), with the worker's set-up (correction 4), since `os_sandbox.wrap` is already a pure command builder (`runner/os_sandbox.py:84`).
    `install_blocks` stays as the in-process layer. If the sandbox is unavailable, introspect still runs as today,
    with blocks only. That is the same exposure as importing the module, which it already does. Open question below.
- `autosetup` does not detect `app:`, and this pass does not change that.

### Files touched (7.2a)

| File | Change |
|---|---|
| `guardlib/symbols.py`, `guardlib/symbol_diff.py` | `decorators_hash`, `decorators_changed` |
| `runner/examples.py` | `entry_gate` |
| `runner/diff.py` | `changed_targets` (new kind, entry gate); `Target.handler`; `gate_parts`, `_tree_specs`, `_run` http spec; `compare_one` route rules; `Result.requests_reached`, `requests_entered`, `status_counts`; `reached_text` |
| `guardlib/engine.py` | `route_targets` gates and handler |
| `runner/lg_worker.py` | `run_route` tracing (threads), `_tracer` per-request gate set, `load_app`, `_APPS` cache, `_http_caller` |
| `guardlib/route_decorators.py` (new), `guardlib/checks.py`, `indexer/routes.py` | the shared decorator parser; INTROSPECT uses `load_app`, and is optionally sandboxed |
| `tools/validate.py` | record the new Result fields |
| `CLAUDE.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md` | the sprint entry |

7.2b adds: `indexer/db.py` (`baseline_routes`), `indexer/routes.py` (`introspect(root=)`, `snapshot_baseline`),
`scripts/session_start.py`, `guardlib/ci.py`, and `engine.route_targets` (deleted/renamed).

## 4. Tests

FastAPI 0.162.7, Starlette 1.10.0, httpx 0.28.1 and Flask 3.1.3 are already dev dependencies (`requirements-dev.txt`
lines 4-7, installed in `.venv-dev`), so no new dependency is needed. All fixtures are generic (`api/`, `web/`, `svc/`,
`things`, `tokens`, `timeline`/`feed`). No held-out project name appears anywhere. Most go in `tests/test_routes_diff.py`.
Unit tests go in `tests/test_symbol_diff.py` and `tests/test_checks.py`.

**A, a decorator adding auth.**
- A1: Flask, `@app.route("/tokens", methods=["DELETE"])` handler gains `@require_token`, a `functools.wraps` wrapper
  that aborts 401 without a header. Expect a `DELETE /tokens` route target, `different` (old 200, new 401).
- A2: the same with a pass-through decorator. Expect `verified`, "reached 1 of 1" (entry gate).
- A3: FastAPI, `@app.get("/x")` to `@app.get("/x", status_code=201)`. Expect `different`.
- Unit tests:
  - `symbol_diff` gives `decorators_changed`;
  - `changed_targets` gives a target whose `new_gate` is the first body statement;
  - `same_code` is False for an added decorator and True for a whitespace or comment change inside a decorator;
  - `audit._stale_callers` and `pre_edit` do **not** treat `decorators_changed` as a signature change.

**B, renamed handlers or changed paths (7.2b).**
- B1: Flask `@app.get("/timeline") def timeline` to `@app.get("/feed") def feed`. Expect a `GET /timeline` target,
  `different` (new 404).
- B2: renamed, same path. Expect `verified` with the new handler entered.
- B3: handler deleted. Expect `different`, and the static "route removed" break.
- B4: the same as B1 with the index built on the head tree only (the CI shape). Expect the same results through
  `baseline_routes`.
- B5: no `baseline_routes` row. Expect the static-decorator fallback, with the prefix caveat in the reason.

**C, router prefix moved, paths unchanged (must stay quiet).**
- Two files:
  - `api/routes/things.py`: `router = APIRouter()` becomes `APIRouter(prefix="/things", tags=["things"])`;
  - `api/main.py`: `include_router(things.router, prefix="/things", tags=["things"])` becomes
    `include_router(things.router)`.
- Run `engine.targets_for` on each edited file. Expect no route results that are `different`, no function targets
  (decorator text is unchanged), and no `Break` from `check_python_source`.
- C2: the same, with one handler body also edited in a behaviour-preserving way. Expect that route `verified`.

**D, 400 to 403 with every request rejected early (must be untested with "reached 0 of N").**
- D1: FastAPI `Depends(current_user)` raises 401 when the header is missing. The handler's
  `HTTPException(400, ...)` becomes `403`.
  - Expect the route `untested`, reason containing "reached 0 of 1" and "401".
  - `status_counts == {"401": n}`, `requests_reached == 0`, `requests_entered == 0`.
- D2: no gate. The handler reads a module constant that changed, behind the same dependency. Expect `untested` with
  "every request was answered 401 before the handler ran".
- D3, positive controls, without auth:
  - a sync endpoint (worker thread) and an async endpoint (portal thread), both `verified` with "reached 1 of 1";
  - Flask, same thread.
  - These prove the cross-thread trace. The existing `test_unchanged_route_behavior_is_verified` must still pass.
- D4: a partial reach. Two changed statements on different branches, only one reached by the requests. Expect
  "reached 1 of 2".

**(d) parser.**
- `.route("/x")` gives GET.
- `methods=["GET","POST"]` and `("PUT",)` give both, or PUT.
- `.api_route`, and `rule=`/`path=` keywords.
- Non-literal `methods` gives no rows.
- `checks`: narrowing methods gives "route removed: POST /x".
- `routes.from_decorators` finds `.route` handlers when introspection is off.

**(e) factories.**
- E1: `web:create_app()` gives route results (`different` on an edit).
- E2: `create_app("testing")`.
- E3: `web:create_app(os.getenv("X"))` and `web:a.b()` give `untested` with the parse reason.
- E4: a factory that raises on a second call, with two routes in one check. Both routes run, which proves the cache.
- E5: introspection with a factory gives rows with `source "framework"`.
- E6: a factory that opens a socket gives `untested`, blocked.

Run `tests/test_routes_diff.py` alone while building. Run the full suite once at the end, one suite at a time
(Python 3.12, then 3.10 per HANDOFF). Route tests cost about 1-2 s each (the scratch run of three fixtures took 2.2 s).

## 5. Sources (opened)

- Flask API, `Flask.route`, `add_url_rule` and the `get`/`post`/... shortcuts —
  [Flask API](https://flask.palletsprojects.com/en/stable/api/).
  - `methods` defaults to `["GET"]`, and HEAD and OPTIONS are added automatically.
  - The shortcuts were "Added in version 2.0" and are "Shortcut for route() with methods=[...]".
  - The installed `flask/sansio/scaffold.py:284-293` raises `TypeError("Use the 'route' decorator to use the 'methods'
    argument.")` if a shortcut is given `methods`.
- Flask CLI, application discovery —
  [Command Line Interface](https://flask.palletsprojects.com/en/stable/cli/).
  - `module:name`, and a factory "optionally followed by arguments in parentheses".
  - "their contents are parsed as Python literals".
  - The automatic factory names are `create_app`/`make_app`.
  - The installed `flask/cli.py::find_app_by_string` uses `ast.parse(mode="eval")` and `ast.literal_eval`, and allows
    only a simple `Name` as the function.
- Starlette TestClient — [docs/testclient.md](https://github.com/encode/starlette/blob/master/docs/testclient.md).
  - The backend arguments "are passed to anyio.start_blocking_portal()".
  - `raise_server_exceptions=False` lets you test 500 bodies.
  - Lifespan runs only with the client used as a context manager.
  - The docs do not describe threads. Threading is from the installed source: `starlette/testclient.py:406-416` and
    `486-491` (a portal per request without `with`), `starlette/routing.py:55` and `fastapi/routing.py:138-139` (sync
    endpoints through `run_in_threadpool`).
  - www.starlette.io and www.uvicorn.org did not resolve from here. The uvicorn `--factory` flag was not confirmed and
    is not relied on.
- FastAPI dependencies run before the path operation function and inject their result —
  [Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/).
  - That page does not say what happens when a dependency raises. That a dependency's `HTTPException` returns before
    the endpoint runs is inference, and the scratch case-D run confirms it in practice (all 401, function body never
    reached).
- Python `sys.settrace`: "The function is thread-specific; ... use threading.settrace()" —
  [sys](https://docs.python.org/3/library/sys.html).
- `threading.settrace` covers threads "started from the threading module" (before `run()`), and
  `threading.settrace_all_threads` is "Added in version 3.12" and also covers running threads —
  [threading](https://docs.python.org/3/library/threading.html).
- `ast.FunctionDef` has `decorator_list` — [ast](https://docs.python.org/3/library/ast.html).
  - The page does not say where `lineno` points for a decorated function. A local check showed it is the `def` line on
    3.12, which matches tree-sitter's `line_start`.
- Case C's actual change, read on GitHub, not cloned: only `APIRouter(...)` constructor calls and
  `include_router(...)` calls changed, and no decorator line —
  [commit 3083207](https://github.com/fastapi/full-stack-fastapi-template/commit/30832076f5d289356da465c2f20e6be34216d72f).

## 6. Open questions

1. Is "reached N of M" counted in **statements** (proposed, consistent with functions and callers) or in **requests**?
   The plan gates on statements and reports request counts as extra text.
2. Should the 7.2 route-only rule ("N counts only compared requests") also apply to functions and callers? Today they
   use the union over all inputs, including ones dropped as non-deterministic. This is proposed as a follow-up, not in
   7.2.
3. (a): accept the `decorators_changed` kind in place of decorators in the signature hash, which avoids false
   stale-caller findings?
4. (e): sandbox the index introspection (option i), or skip introspection for factories (option ii)?
5. Should test-client `headers=` literals be harvested into requests (a cheap follow-up)? Auth-protected routes would
   then often reach their handlers, instead of being honestly untested.
6. Is the split right: 7.2a = a, b, d, e and 7.2b = c?
