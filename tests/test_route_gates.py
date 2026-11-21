"""Sprint 7.2a: route correctness. Decorator edits count as code (a), routes are gated on the changed statements their
requests ran and on whether a request entered the handler (b), and factory apps load (e)."""
import subprocess
import time

from guardlib import engine, symbol_diff
from indexer import __main__ as indexer_main
from indexer import db
from runner import diff, lg_codec

from conftest import write_tree


def _git(root):
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def build(tmp_path, make_cfg, files, new_files, app, session=False):
    """A project with the edit applied. The routes table describes the head tree (as ci.run and tools/validate.py build
    it) or, with session=True, the baseline (as SessionStart builds it, before any edit)."""
    root = write_tree(tmp_path / "proj", dict(files) if session else {**files, **new_files})
    cfg = make_cfg(root, app=app)
    base = write_tree(cfg.state_dir / "baseline", files)
    _git(root)
    indexer_main.refresh(cfg)
    if session:
        write_tree(root, new_files)
    return cfg, root, base


def route_results(cfg, root, base, rel):
    changed = diff.changed_targets(rel, (base / rel).read_text(), (root / rel).read_text())
    deps = engine.dependents(cfg, rel, changed, {t.qualname for t in changed})
    routes = engine.route_targets(cfg, changed + deps)
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps + routes, harvest={})}
    return routes, results


def route_rows(cfg):
    conn = db.connect(cfg.root)
    try:
        return [tuple(r) for r in conn.execute("SELECT method, path, handler_qualname, source FROM routes")]
    finally:
        conn.close()


# ---------------------------------------------------------------- (a) decorators count as code

AUTH = """
import functools
from flask import abort, request


def require_token(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not request.headers.get("X-Token"):
            abort(401)
        return fn(*args, **kwargs)
    return wrapper


def passthrough(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper
"""

TOKENS = {
    "web/__init__.py": "",
    "web/auth.py": AUTH,
    "web/app.py": """
from flask import Flask, jsonify
from web.auth import passthrough, require_token

app = Flask(__name__)


@app.route("/tokens", methods=["DELETE"])
def drop_tokens():
    return jsonify(dropped=True)
""",
}


def test_symbol_diff_reports_decorator_only_edits_apart():
    before = "@app.get('/x')\ndef h():\n    return 1\n"
    after = "@app.get('/x')\n@cache\ndef h():\n    return 1\n"
    assert [(c.kind, c.qualname) for c in symbol_diff.diff(before, after)] == [("decorators_changed", "h")]
    # Not a signature change: audit._stale_callers and pre_edit's risk signals key on "signature_changed".
    assert symbol_diff.diff(before, before) == []


def test_changed_targets_gate_a_decorator_only_edit_on_the_first_body_statement():
    before = '@app.get("/x")\ndef h(a):\n    """Doc."""\n    if a:\n        return 1\n    return 2\n'
    after = before.replace('@app.get("/x")\n', '@app.get("/x")\n@require_token\n')
    (t,) = diff.changed_targets("m.py", before, after)
    assert t.qualname == "h" and t.entry_gate
    assert t.new_gate == [[5, 5]] and t.old_gate == []      # the `if a:` header, past the docstring
    assert t.code_hash != diff.changed_targets("m.py", before, before.replace("return 2", "return 3"))[0].code_hash


def test_same_code_sees_decorators_but_not_their_layout():
    before = '@app.get("/x")\ndef h():\n    return 1\n'
    added = before.replace("def h", "@cache\ndef h")
    spaced = before.replace('@app.get("/x")', '@app.get( "/x" )  # the list')
    sym = lambda src: next(s for s in __import__("guardlib.symbols", fromlist=["x"]).extract(src)[0] if s.qualname == "h")
    assert not diff.same_code(before, added, sym(before), sym(added))
    assert diff.same_code(before, spaced, sym(before), sym(spaced))
    assert diff.changed_targets("m.py", before, spaced) == []


def test_a1_auth_decorator_added_is_different_without_hedge(tmp_path, make_cfg):
    new = TOKENS["web/app.py"].replace("def drop_tokens", "@require_token\ndef drop_tokens")
    cfg, root, base = build(tmp_path, make_cfg, TOKENS, {"web/app.py": new}, "web.app:app")
    routes, results = route_results(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["DELETE /tokens"]
    r = results["DELETE /tokens"]
    assert r.status == diff.DIFFERENT
    assert r.differences[0]["before"].find("200") >= 0 and r.differences[0]["after"].find("401") >= 0
    assert r.statements_total == 1 and r.statements_reached == 0
    assert r.unreached_note == ""   # the gate is the entry gate: the decorator itself made the difference


def test_a2_pass_through_decorator_is_verified_on_the_entry_gate(tmp_path, make_cfg):
    new = TOKENS["web/app.py"].replace("def drop_tokens", "@passthrough\ndef drop_tokens")
    cfg, root, base = build(tmp_path, make_cfg, TOKENS, {"web/app.py": new}, "web.app:app")
    _, results = route_results(cfg, root, base, "web/app.py")
    r = results["DELETE /tokens"]
    assert r.status == diff.DONE, r.reason
    assert diff.reached_text(r).startswith("reached 1 of 1 changed statements")
    assert r.requests_entered == r.inputs_compared > 0


FAST_X = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI

app = FastAPI()


@app.get("/x")
def get_x():
    return {"ok": True}
""",
}


def test_a3_status_code_in_decorator_is_different(tmp_path, make_cfg):
    new = FAST_X["api/main.py"].replace('@app.get("/x")', '@app.get("/x", status_code=201)')
    cfg, root, base = build(tmp_path, make_cfg, FAST_X, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["GET /x"]
    assert r.status == diff.DIFFERENT
    assert "201" in r.differences[0]["after"]
    assert r.statements_reached == 1 and r.unreached_note == ""


MOVED = {
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/a")
def handler():
    return jsonify(value=1)
""",
}


def test_a4_path_moved_in_decorator_head_index_is_a_new_route(tmp_path, make_cfg):
    # The ci.run / tools/validate.py shape: the routes table describes the head tree, so the route is GET /b.
    new = MOVED["web/app.py"].replace('@app.route("/a")', '@app.route("/b")')
    cfg, root, base = build(tmp_path, make_cfg, MOVED, {"web/app.py": new}, "web.app:app")
    routes, results = route_results(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /b"]
    r = results["GET /b"]
    assert r.status == diff.UNTESTED and not r.differences
    assert "route is new in this edit" in r.reason and "404" in r.reason


def test_a4_path_moved_in_decorator_session_index_is_different(tmp_path, make_cfg):
    # The session shape: the routes table describes the baseline, so the route is GET /a, now gone (404 before the
    # handler). That is a real removal: different, with no "may come from elsewhere" hedge.
    new = MOVED["web/app.py"].replace('@app.route("/a")', '@app.route("/b")')
    cfg, root, base = build(tmp_path, make_cfg, MOVED, {"web/app.py": new}, "web.app:app", session=True)
    routes, results = route_results(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /a"]
    r = results["GET /a"]
    assert r.status == diff.DIFFERENT and "404" in r.differences[0]["after"]
    assert r.unreached_note == ""


METHOD_CACHE = {
    "pkg/__init__.py": "",
    "pkg/calc.py": """
import functools


class Calc:
    def __init__(self):
        self.base = 3

    def total(self, n: int) -> int:
        return self.base + n
""",
}


def test_decorator_only_edit_on_a_method_is_verified_past_the_decorator(tmp_path, make_cfg):
    root = write_tree(tmp_path / "proj", METHOD_CACHE)
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", METHOD_CACHE)
    new = METHOD_CACHE["pkg/calc.py"].replace("    def total", "    @functools.lru_cache(maxsize=None)\n    def total")
    (root / "pkg/calc.py").write_text(new)
    (t,) = diff.changed_targets("pkg/calc.py", (base / "pkg/calc.py").read_text(), new)
    assert t.qualname == "Calc.total" and t.entry_gate and t.new_gate
    (r,) = diff.run(cfg, str(base), str(root), [t], harvest={})
    assert r.status == diff.DONE, r.reason
    assert diff.reached_text(r) == "reached 1 of 1 changed statements"


# ---------------------------------------------------------------- (b) gates and early answers

GUARDED = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import Depends, FastAPI, Header, HTTPException

app = FastAPI()
LIMIT = 10


def current_user(x_token: str = Header(default="")):
    if not x_token:
        raise HTTPException(status_code=401, detail="no token")
    return x_token


@app.delete("/things/{thing_id}")
def remove_thing(thing_id: int, user: str = Depends(current_user)):
    if thing_id < 0:
        raise HTTPException(status_code=400, detail="bad id")
    return {"removed": thing_id, "limit": LIMIT}
""",
}


def test_d1_every_request_rejected_by_a_dependency_is_untested(tmp_path, make_cfg):
    new = GUARDED["api/main.py"].replace("status_code=400", "status_code=403")
    cfg, root, base = build(tmp_path, make_cfg, GUARDED, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["DELETE /things/{thing_id}"]
    assert r.status == diff.UNTESTED
    assert "reached 0 of 1" in r.reason and "401" in r.reason and "before the handler ran" in r.reason
    assert r.status_counts == {"401": r.inputs_compared} and r.inputs_compared > 0
    assert r.requests_reached == 0 and r.requests_entered == 0


def test_d2_no_gate_route_behind_a_dependency_is_untested(tmp_path, make_cfg):
    new = GUARDED["api/main.py"].replace("LIMIT = 10", "LIMIT = 20")
    cfg, root, base = build(tmp_path, make_cfg, GUARDED, {"api/main.py": new}, "api.main:app")
    routes, results = route_results(cfg, root, base, "api/main.py")
    r = results["DELETE /things/{thing_id}"]
    assert r.statements_total == 0
    assert r.status == diff.UNTESTED
    assert r.reason.startswith("every request was answered 401 before the handler ran, in both versions")


CONTROLS = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI

app = FastAPI()


@app.get("/sync/{n}")
def sync_total(n: int):
    return {"total": n * 2}


@app.get("/async/{n}")
async def async_total(n: int):
    return {"total": n * 3}
""",
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/total/<int:n>")
def flask_total(n):
    return jsonify(total=n * 4)
""",
}


def test_d3_sync_and_async_endpoints_are_traced_across_threads(tmp_path, make_cfg):
    new = (CONTROLS["api/main.py"].replace('return {"total": n * 2}', 'doubled = n + n\n    return {"total": doubled}')
           .replace('return {"total": n * 3}', 'tripled = n + n + n\n    return {"total": tripled}'))
    cfg, root, base = build(tmp_path, make_cfg, CONTROLS, {"api/main.py": new}, "api.main:app")
    started = time.monotonic()
    _, results = route_results(cfg, root, base, "api/main.py")
    elapsed = time.monotonic() - started
    for name in ("GET /sync/{n}", "GET /async/{n}"):
        r = results[name]
        assert r.status == diff.DONE, (name, r.reason)
        assert r.statements_reached == r.statements_total == 2
        assert r.requests_reached == r.requests_entered == r.inputs_compared == 4
    assert elapsed < 30


def test_d3_flask_endpoint_is_traced_on_the_calling_thread(tmp_path, make_cfg):
    new = CONTROLS["web/app.py"].replace("return jsonify(total=n * 4)", "quad = n * 2 * 2\n    return jsonify(total=quad)")
    cfg, root, base = build(tmp_path, make_cfg, CONTROLS, {"web/app.py": new}, "web.app:app")
    _, results = route_results(cfg, root, base, "web/app.py")
    r = results["GET /total/<int:n>"]
    assert r.status == diff.DONE, r.reason
    assert r.statements_reached == r.statements_total == 2 and r.requests_entered == r.inputs_compared


BRANCHES = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI

app = FastAPI()


@app.get("/labels/{name}")
def label(name: str):
    if name == "zzz-special":
        return {"kind": "special"}
    return {"kind": "plain"}
""",
}


def test_d4_partial_reach_is_untested_with_n_of_m(tmp_path, make_cfg):
    new = BRANCHES["api/main.py"].replace('{"kind": "special"}', '{"kind": "spec" + "ial"}').replace('{"kind": "plain"}', '{"kind": "pl" + "ain"}')
    cfg, root, base = build(tmp_path, make_cfg, BRANCHES, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["GET /labels/{name}"]
    assert r.status == diff.UNTESTED
    assert r.reason.startswith("requests reached 1 of 2 changed statements of label; line 9 never ran")
    assert f"({r.inputs_compared} of {r.inputs_compared} requests reached any)" in r.reason


NOT_FOUND_FLASK = {
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask, abort, jsonify

app = Flask(__name__)
THINGS = {}


@app.route("/things/<int:thing_id>")
def get_thing(thing_id):
    if thing_id not in THINGS:
        abort(404)
    return jsonify(name=THINGS[thing_id])
""",
}

NOT_FOUND_ASGI = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI, HTTPException

app = FastAPI()
THINGS = {}


@app.get("/things/{thing_id}")
def get_thing(thing_id: int):
    if thing_id not in THINGS:
        raise HTTPException(status_code=404, detail="unknown id")
    return {"name": THINGS[thing_id]}
""",
}


def test_handler_own_404_on_every_request_is_verified_flask(tmp_path, make_cfg):
    new = NOT_FOUND_FLASK["web/app.py"].replace("if thing_id not in THINGS:", "if THINGS.get(thing_id) is None:")
    cfg, root, base = build(tmp_path, make_cfg, NOT_FOUND_FLASK, {"web/app.py": new}, "web.app:app")
    _, results = route_results(cfg, root, base, "web/app.py")
    r = results["GET /things/<int:thing_id>"]
    assert r.status_counts == {"404": r.inputs_compared}
    assert r.status == diff.DONE, r.reason
    assert r.requests_entered == r.inputs_compared


def test_handler_own_404_on_every_request_is_verified_asgi(tmp_path, make_cfg):
    new = NOT_FOUND_ASGI["api/main.py"].replace("if thing_id not in THINGS:", "if THINGS.get(thing_id) is None:")
    cfg, root, base = build(tmp_path, make_cfg, NOT_FOUND_ASGI, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["GET /things/{thing_id}"]
    assert r.status_counts == {"404": r.inputs_compared}
    assert r.status == diff.DONE, r.reason
    assert r.requests_entered == r.inputs_compared


LOOKUP_ASGI = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import Depends, FastAPI, HTTPException

app = FastAPI()
THINGS = {}


def get_thing_or_404(thing_id: int):
    if thing_id not in THINGS:
        raise HTTPException(status_code=404, detail="unknown id")
    return THINGS[thing_id]


@app.get("/things/{thing_id}")
def show_thing(thing_id: int, thing=Depends(get_thing_or_404)):
    return {"id": thing_id}
""",
}


def test_dropping_a_404_dependency_is_different_not_a_new_route(tmp_path, make_cfg):
    # Old: every request 404 from the dependency, the handler never entered. New: the handler answers. The route
    # matched in the old app, so this is a real change, not an addition.
    new = LOOKUP_ASGI["api/main.py"].replace(", thing=Depends(get_thing_or_404)", "")
    cfg, root, base = build(tmp_path, make_cfg, LOOKUP_ASGI, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["GET /things/{thing_id}"]
    assert r.status == diff.DIFFERENT, r.reason
    assert "404" in r.differences[0]["before"] and "200" in r.differences[0]["after"]


LOOKUP_FLASK = {
    "web/__init__.py": "",
    "web/app.py": """
import functools

from flask import Flask, abort, jsonify

app = Flask(__name__)
THINGS = {}


def require_thing(fn):
    @functools.wraps(fn)
    def wrapper(thing_id):
        if thing_id not in THINGS:
            abort(404)
        return fn(thing_id)
    return wrapper


@app.route("/things/<int:thing_id>")
@require_thing
def show_thing(thing_id):
    return jsonify(id=thing_id)
""",
}


def test_dropping_a_404_decorator_is_different_not_a_new_route(tmp_path, make_cfg):
    new = LOOKUP_FLASK["web/app.py"].replace("@require_thing\n", "")
    cfg, root, base = build(tmp_path, make_cfg, LOOKUP_FLASK, {"web/app.py": new}, "web.app:app")
    _, results = route_results(cfg, root, base, "web/app.py")
    r = results["GET /things/<int:thing_id>"]
    assert r.status == diff.DIFFERENT, r.reason
    assert "404" in r.differences[0]["before"] and "200" in r.differences[0]["after"]


DENIED_ASGI = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import Depends, FastAPI, HTTPException

app = FastAPI()


def method_guard():
    raise HTTPException(status_code=405, detail="not now")


@app.get("/things/{thing_id}")
def show_thing(thing_id: int, guard=Depends(method_guard)):
    return {"id": thing_id}
""",
}


def test_dropping_a_405_dependency_on_a_matched_route_is_different(tmp_path, make_cfg):
    # The route matched (routed True); the 405 came from a dependency, not from routing.
    new = DENIED_ASGI["api/main.py"].replace(", guard=Depends(method_guard)", "")
    cfg, root, base = build(tmp_path, make_cfg, DENIED_ASGI, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["GET /things/{thing_id}"]
    assert r.status_counts == {"405": r.inputs_compared}
    assert r.status == diff.DIFFERENT, r.reason


TENANT_ASGI = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()
KNOWN = ("/a",)


@app.middleware("http")
async def known_paths_only(request, call_next):
    if request.url.path not in KNOWN:
        return JSONResponse({"detail": "unknown tenant"}, status_code=404)
    return await call_next(request)


@app.get("/a")
def handler():
    return {"value": 1}
""",
}


def test_a_middleware_404_before_the_router_is_not_read_as_routing(tmp_path, make_cfg):
    # The path moves to /b, and the middleware's list moves with it. In the old app the middleware answers /b with 404
    # before the router runs, so whether a route matched is unknown (routed None) and the new-route rule must not fire.
    new = TENANT_ASGI["api/main.py"].replace('@app.get("/a")', '@app.get("/b")').replace('KNOWN = ("/a",)', 'KNOWN = ("/b",)')
    cfg, root, base = build(tmp_path, make_cfg, TENANT_ASGI, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["GET /b"]
    assert "route is new in this edit" not in r.reason
    assert r.status == diff.DIFFERENT


def test_starlette_routed_reads_the_router_scope():
    from runner import lg_worker

    class Route:
        methods = {"GET", "HEAD"}

    class App:
        routes = []
    assert lg_worker._starlette_routed({"method": "GET"}) is None                                   # router never ran
    assert lg_worker._starlette_routed({"router": 1, "method": "GET"}) is False                     # matched nothing
    assert lg_worker._starlette_routed({"router": 1, "method": "GET", "endpoint": App(), "route": object()}) is False  # mount, nothing inside
    assert lg_worker._starlette_routed({"router": 1, "method": "POST", "endpoint": len, "route": Route()}) is False   # partial
    assert lg_worker._starlette_routed({"router": 1, "method": "GET", "endpoint": len, "route": Route()}) is True
    assert lg_worker._starlette_routed({"router": 1, "method": "GET", "endpoint": len}) == "no route"


# ---------------------------------------------------------------- the rule order, without running anything

def _route(**gate):
    return diff.Target("api/main.py", "GET /x", "route", [], "h", handler={"module": "api.main", "qualname": "h"},
                       callees=["helper"] if gate else [], callee_file="api/logic.py" if gate else "", **gate)


def _req():
    return {"args": [{"name": "method", "kind": "pos", "value": lg_codec.encode_input("GET")},
                     {"name": "path", "kind": "pos", "value": lg_codec.encode_input("/x")}], "source": "generated"}


def _run(status, entered, request_gate=None, k=3, routed=True):
    out = {"status": "ran", "results": [{"outcome": "return", "value": lg_codec.canon({"status": status, "body": "x"})}] * k,
           "statuses": [status] * k, "entered": [entered] * k, "routed": [routed] * k, "lines": []}
    if request_gate is not None:
        out["request_gate"] = [list(request_gate)] * k
        out["gate_lines"] = list(request_gate)
    return out


def test_rule_order_statements_decide_when_there_is_a_gate():
    # A dependency calls the edited function (gate lines run) and then rejects with 401: the handler is never entered,
    # yet every changed statement ran. M > 0, so the statement rule decides: verified, whatever the early 4xx says.
    t = _route(callee_new_gate=[[5, 5]])
    items = [_req()] * 3
    r = diff.compare_one(t, items, _run(401, False, [5]), _run(401, False, [5]), _run(401, False, [5]), True)
    assert r.status == diff.DONE and r.statements_reached == 1 and r.requests_reached == 3 and r.requests_entered == 0
    # Same answers with the gate never run: untested, with the early-4xx wording added because N == 0.
    r = diff.compare_one(t, items, _run(401, False, []), _run(401, False, []), _run(401, False, []), True)
    assert r.status == diff.UNTESTED
    assert "reached 0 of 1" in r.reason and "every request was answered 401 before the handler ran" in r.reason


def test_rule_order_early_4xx_decides_only_without_a_gate():
    t = _route()
    items = [_req()] * 3
    r = diff.compare_one(t, items, _run(422, False), _run(422, False), _run(422, False), True)
    assert r.status == diff.UNTESTED and r.reason.startswith("every request was answered 422 before the handler ran")
    # The handler ran and answered 404 itself: not early.
    r = diff.compare_one(t, items, _run(404, True), _run(404, True), _run(404, True), True)
    assert r.status == diff.DONE
    # Not entered in the edited code, and not a 4xx: no request reached the handler.
    r = diff.compare_one(t, items, _run(302, False), _run(302, False), _run(302, False), True)
    assert r.status == diff.UNTESTED and r.reason.startswith("no request reached the handler h")
    # Entry unknown (handler not resolved): status alone, with the hedge.
    r = diff.compare_one(t, items, _run(401, None), _run(401, None), _run(401, None), True)
    assert r.status == diff.UNTESTED and "could not be traced, so it may never have run" in r.reason


def test_rule_new_route_is_not_a_finding():
    t = _route(callee_new_gate=[[5, 5]])
    items = [_req()] * 3
    old = _run(404, False, [], routed=False)
    r = diff.compare_one(t, items, old, old, _run(200, True, [5]), True)
    assert r.status == diff.UNTESTED and not r.differences and "route is new in this edit" in r.reason
    # The same answers, but a route of the old app matched (a dependency or decorator gave the 404): a real change.
    old = _run(404, False, [], routed=True)
    r = diff.compare_one(t, items, old, old, _run(200, True, [5]), True)
    assert r.status == diff.DIFFERENT
    # Routing not known (no "routed" from the worker): never an addition.
    old = _run(404, False, [], routed=None)
    r = diff.compare_one(t, items, old, old, _run(200, True, [5]), True)
    assert r.status == diff.DIFFERENT


def test_entry_unknown_pass_without_gate_says_so():
    t = _route()
    items = [_req()] * 3
    r = diff.compare_one(t, items, _run(200, None), _run(200, None), _run(200, None), True)
    assert r.status == diff.DONE and r.reason.startswith("entry unknown: the handler h could not be traced")


def _differing(t, gate_hit):
    items = [_req()] * 3
    old = _run(200, True, [], routed=True)
    new = _run(401, False, [5] if gate_hit else [], routed=True)
    new["gate_lines"] = [5] if gate_hit else []
    return diff.compare_one(t, items, old, old, new, True)


def test_hedge_kept_for_callers_and_caller_routes_with_an_entry_gate():
    # A caller whose edited callee changed only its decorators: the caller may never have called it, so the note stays.
    caller = diff.Target("api/svc.py", "use_helper", "function", [], "h", via="calls helper", via_file="api/logic.py",
                         callees=["helper"], callee_file="api/logic.py", callee_new_gate=[[5, 5]], entry_gate=True)
    old = {"status": "ran", "results": [{"outcome": "return", "value": lg_codec.canon(1)}] * 3, "lines": [], "gate_lines": []}
    new = {"status": "ran", "results": [{"outcome": "return", "value": lg_codec.canon(2)}] * 3, "lines": [], "gate_lines": []}
    r = diff.compare_one(caller, [_req()] * 3, old, old, new, True)
    assert r.status == diff.DIFFERENT
    assert r.unreached_note.startswith("The inputs never got past the changed decorators of helper, or never called it")
    # A route whose handler only calls the edited function: the note stays too.
    r = _differing(_route(callee_new_gate=[[5, 5]], entry_gate=True), gate_hit=False)
    assert r.status == diff.DIFFERENT and "never got past the changed decorators of helper" in r.unreached_note
    # A route gated on its own handler's entry gate: no note (A1 checks this end to end).
    own = diff.Target("api/main.py", "GET /x", "route", [], "h", handler={"module": "api.main", "qualname": "h"},
                      callees=["h"], callee_file="api/main.py", callee_new_gate=[[5, 5]], entry_gate=True)
    r = _differing(own, gate_hit=False)
    assert r.status == diff.DIFFERENT and r.unreached_note == ""


def test_dropped_requests_do_not_count_as_reached():
    # Only the compared requests' own gate lines count: request 0 ran the gate but old disagreed with itself there.
    t = _route(callee_new_gate=[[5, 5]])
    items = [_req()] * 2
    a = _run(200, True, [], k=2)
    b = _run(200, True, [], k=2)
    b["results"] = [{"outcome": "return", "value": lg_codec.canon({"status": 200, "body": "other"})}, b["results"][1]]
    n = _run(200, True, [], k=2)
    n["request_gate"] = [[5], []]
    r = diff.compare_one(t, items, a, b, n, True)
    assert r.compared == [1] and r.statements_reached == 0 and r.status == diff.UNTESTED


# ---------------------------------------------------------------- (e) factory apps

FACTORY = {
    "web/__init__.py": "",
    "web/views.py": """
from flask import Blueprint, current_app, jsonify

bp = Blueprint("main", __name__)


@bp.route("/greet/<name>")
def greet(name):
    return jsonify(message=f"hello {name}", mode=current_app.config["MODE"])


@bp.route("/count/<int:n>")
def count(n):
    return jsonify(n=n + 1)
""",
    "web/app.py": """
from flask import Flask
from web.views import bp

MADE = []


def create_app(mode="prod"):
    if MADE:
        raise RuntimeError("create_app was already called in this process")
    MADE.append(mode)
    app = Flask(__name__)
    app.config["MODE"] = mode
    app.register_blueprint(bp)
    return app
""",
}


def test_e1_e4_e5_factory_app_runs_once_per_process_and_is_introspected(tmp_path, make_cfg):
    new = FACTORY["web/views.py"].replace('f"hello {name}"', 'f"hi {name}"').replace("n + 1", "n + 2")
    cfg, root, base = build(tmp_path, make_cfg, FACTORY, {"web/views.py": new}, "web.app:create_app()")
    assert {(m, p, q, s) for m, p, q, s in route_rows(cfg)} == {("GET", "/greet/<name>", "greet", "framework"),
                                                                ("GET", "/count/<int:n>", "count", "framework")}
    _, results = route_results(cfg, root, base, "web/views.py")
    # Two routes in one check: the factory raises when called twice, so both running proves the per-process cache.
    assert results["GET /greet/<name>"].status == diff.DIFFERENT
    assert results["GET /count/<int:n>"].status == diff.DIFFERENT


def test_e2_factory_with_literal_arguments(tmp_path, make_cfg):
    new = FACTORY["web/views.py"].replace('f"hello {name}"', 'f"hi {name}"')
    cfg, root, base = build(tmp_path, make_cfg, FACTORY, {"web/views.py": new}, "web.app:create_app('testing')")
    _, results = route_results(cfg, root, base, "web/views.py")
    r = results["GET /greet/<name>"]
    assert r.status == diff.DIFFERENT and "testing" in r.differences[0]["before"]


def test_bare_factory_name_is_called_like_flask_does(tmp_path, make_cfg):
    new = FACTORY["web/views.py"].replace('f"hello {name}"', 'f"hi {name}"')
    cfg, root, base = build(tmp_path, make_cfg, FACTORY, {"web/views.py": new}, "web.app:create_app")
    assert ("GET", "/greet/<name>", "greet", "framework") in route_rows(cfg)
    _, results = route_results(cfg, root, base, "web/views.py")
    assert results["GET /greet/<name>"].status == diff.DIFFERENT


def test_e3_factory_references_that_are_not_literal_are_refused(tmp_path, make_cfg):
    new = FACTORY["web/views.py"].replace('f"hello {name}"', 'f"hi {name}"')
    for ref in ("web.app:create_app(os.getenv('X'))", "web.app:a.b()"):
        cfg, root, base = build(tmp_path / ref.split(":")[1][:8].replace("(", "_").replace(".", "_"), make_cfg, FACTORY,
                                {"web/views.py": new}, ref)
        assert all(s == "decorator" for *_, s in route_rows(cfg))   # introspection failed, the decorators answered
        _, results = route_results(cfg, root, base, "web/views.py")
        r = results["GET /greet/<name>"]
        assert r.status == diff.UNTESTED
        assert "must be module:name or module:name(literal arguments)" in r.reason


def test_e5_factory_writing_a_cache_file_is_introspected_with_the_write_redirected(tmp_path, make_cfg):
    files = dict(FACTORY)
    files["web/app.py"] = FACTORY["web/app.py"].replace("    MADE.append(mode)\n", """    MADE.append(mode)
    import os
    folder = os.path.join(os.path.dirname(__file__), ".cache")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "routes.json"), "w") as fh:
        fh.write("{}")
""")
    new = files["web/views.py"].replace('f"hello {name}"', 'f"hi {name}"')
    cfg, root, base = build(tmp_path, make_cfg, files, {"web/views.py": new}, "web.app:create_app()")
    assert ("GET", "/greet/<name>", "greet", "framework") in route_rows(cfg)
    assert not (root / "web" / ".cache").exists()


def test_e6_factory_that_opens_a_socket_is_untested_blocked(tmp_path, make_cfg):
    files = dict(FACTORY)
    files["web/app.py"] = FACTORY["web/app.py"].replace("    MADE.append(mode)\n", """    MADE.append(mode)
    import socket
    socket.create_connection(("192.0.2.1", 9), timeout=1)
""")
    new = files["web/views.py"].replace('f"hello {name}"', 'f"hi {name}"')
    cfg, root, base = build(tmp_path, make_cfg, files, {"web/views.py": new}, "web.app:create_app()")
    _, results = route_results(cfg, root, base, "web/views.py")
    r = results["GET /greet/<name>"]
    assert r.status == diff.UNTESTED and "network" in r.reason.lower(), r.reason


# ---------------------------------------------------------------- leftover threads of a timed-out request

def _slow(started, go):
    started.set()
    go.wait(5)
    late = 1
    return late


def test_leftover_thread_lines_stay_with_their_own_request():
    import threading

    from runner import lg_worker
    span = lg_worker._span(_slow)
    cur, stale = [{"own": set(), "gate": set()}], set()
    trace = lg_worker._route_tracer(span, None, cur, stale)
    started, go = threading.Event(), threading.Event()
    threading.settrace(trace)
    try:
        worker = threading.Thread(target=_slow, args=(started, go))
        worker.start()
        assert started.wait(5)
        first, cur[0] = cur[0], {"own": set(), "gate": set()}   # the next request starts while the thread still runs
        go.set()
        worker.join(5)
    finally:
        threading.settrace(None)
    late_line = _slow.__code__.co_firstlineno + 3
    assert late_line in first["own"] and cur[0]["own"] == set()


def test_threads_alive_at_a_timeout_are_not_traced_later():
    import threading

    from runner import lg_worker
    span = lg_worker._span(_slow)
    cur, stale = [{"own": set(), "gate": set()}], set()
    trace = lg_worker._route_tracer(span, None, cur, stale)
    ready, started, go = threading.Event(), threading.Event(), threading.Event()
    go.set()

    def leftover():
        ready.wait(5)
        _slow(started, go)
    threading.settrace(trace)
    try:
        worker = threading.Thread(target=leftover)
        worker.start()
        stale.add(worker.ident)
        ready.set()
        worker.join(5)
    finally:
        threading.settrace(None)
    assert started.is_set() and cur[0]["own"] == set()


# ---------------------------------------------------------------- C: a router prefix moved, paths unchanged

def test_c_router_prefix_moved_between_files_stays_quiet():
    from guardlib import checks
    routes_before = 'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n@router.get("/")\ndef list_things():\n    return []\n'
    routes_after = routes_before.replace("APIRouter()", 'APIRouter(prefix="/things", tags=["things"])')
    main_before = ('from fastapi import FastAPI\nfrom api.routes import things\n\napp = FastAPI()\n'
                   'app.include_router(things.router, prefix="/things", tags=["things"])\n')
    main_after = main_before.replace('things.router, prefix="/things", tags=["things"]', "things.router")
    # The decorators' text did not change, and a handler does not "read" `router` through its decorator.
    assert diff.changed_targets("api/routes/things.py", routes_before, routes_after) == []
    assert diff.changed_targets("api/main.py", main_before, main_after) == []
    assert checks.check_python_source("api/routes/things.py", routes_before, routes_after, None, None) == []
    assert checks.check_python_source("api/main.py", main_before, main_after, None, None) == []
