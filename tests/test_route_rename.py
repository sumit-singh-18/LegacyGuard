"""Sprint 7.2b: routes of handlers an edit deleted or renamed. The old METHOD and path come from baseline_routes (filled
at SessionStart from the routes table, and by ci.run from the baseline tree), then routes, then the old function's own
decorators (with a prefix caveat). Renamed handlers are gated on the new function's entry; deleted ones on the old
function's whole body in the code before the edit."""
import importlib.util
import subprocess

from guardlib import checks, ci, engine
from indexer import __main__ as indexer_main
from indexer import db, routes as index_routes
from runner import diff

from conftest import PLUGIN, TARGET_PY, write_tree


def _git(root):
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def build(tmp_path, make_cfg, files, new_files, app, shape="session"):
    """A project with the edit applied, indexed the way each caller does it:
    - "session": SessionStart's shape. The index is built before the edit and routes are copied into baseline_routes.
    - "ci": ci.run's shape. The index describes the head tree; baseline_routes is introspected in the baseline tree.
    - "head": the head index only, with baseline_routes empty (the static-decorator fallback)."""
    root = write_tree(tmp_path / "proj", dict(files) if shape == "session" else {**files, **new_files})
    cfg = make_cfg(root, app=app)
    base = write_tree(cfg.state_dir / "baseline", files)
    _git(root)
    indexer_main.refresh(cfg)
    conn = db.connect(cfg.root)
    try:
        if shape == "session":
            index_routes.snapshot_baseline(conn)
        elif shape == "ci":
            index_routes.rebuild_baseline(conn, cfg, str(base))
        conn.commit()
    finally:
        conn.close()
    if shape == "session":
        write_tree(root, new_files)
    return cfg, root, base


def results_for(cfg, root, base, rel):
    """The targets engine.targets_for plans for the edit, and their results."""
    targets = engine.targets_for(cfg, rel, {"findings": []})
    routes = [t for t in targets if t.kind == "route"]
    return routes, {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def rows(cfg, table):
    conn = db.connect(cfg.root)
    try:
        return sorted(tuple(r) for r in conn.execute(f"SELECT method, path, handler_qualname, handler_file, source FROM {table}"))
    finally:
        conn.close()


TIMELINE = {
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/timeline")
def timeline():
    return jsonify(items=[1, 2])


@app.get("/other")
def other():
    return jsonify(ok=True)
""",
}
RENAMED_AND_MOVED = TIMELINE["web/app.py"].replace('@app.get("/timeline")\ndef timeline', '@app.get("/feed")\ndef feed')
RENAMED = TIMELINE["web/app.py"].replace("def timeline", "def feed")
DELETED = TIMELINE["web/app.py"].replace('@app.get("/timeline")\ndef timeline():\n    return jsonify(items=[1, 2])\n\n\n', "")


# ---------------------------------------------------------------- B1-B5

def test_b1_renamed_handler_with_a_new_path_is_different(tmp_path, make_cfg):
    cfg, root, base = build(tmp_path, make_cfg, TIMELINE, {"web/app.py": RENAMED_AND_MOVED}, "web.app:app")
    routes, results = results_for(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]          # GET /feed is new: no target
    (t,) = routes
    assert t.file == "web/app.py" and t.handler["qualname"] == "feed" and t.old_handler["qualname"] == "timeline"
    assert t.entry_gate and t.callee_new_gate and not t.callee_old_gate and t.route_note == ""
    r = results["GET /timeline"]
    assert r.status == diff.DIFFERENT, r.reason
    assert "200" in r.differences[0]["before"] and "404" in r.differences[0]["after"]
    assert r.unreached_note == ""   # the gate is the renamed handler's own entry: the route is gone, nothing else


def test_b2_renamed_handler_on_the_same_path_is_verified_with_the_new_handler_entered(tmp_path, make_cfg):
    cfg, root, base = build(tmp_path, make_cfg, TIMELINE, {"web/app.py": RENAMED}, "web.app:app")
    routes, results = results_for(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    r = results["GET /timeline"]
    assert r.status == diff.DONE, r.reason
    assert diff.reached_text(r).startswith("reached 1 of 1 changed statements")
    assert r.requests_entered == r.inputs_compared > 0


def test_b3_deleted_handler_is_different_and_the_static_check_reports_the_removal(tmp_path, make_cfg):
    cfg, root, base = build(tmp_path, make_cfg, TIMELINE, {"web/app.py": DELETED}, "web.app:app")
    routes, results = results_for(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    (t,) = routes
    assert t.handler == {} and t.old_handler["qualname"] == "timeline"
    assert t.callee_old_gate and not t.callee_new_gate and not t.entry_gate
    r = results["GET /timeline"]
    assert r.status == diff.DIFFERENT, r.reason
    assert "404" in r.differences[0]["after"]
    assert r.statements_reached == r.statements_total == 1     # some request ran the old handler
    breaks = checks.check_python_source("web/app.py", (base / "web/app.py").read_text(), DELETED.lstrip("\n"), None, None)
    assert any("route removed: GET /timeline" in b.text() for b in breaks)


def test_b4_head_index_finds_the_old_route_through_baseline_routes(tmp_path, make_cfg):
    cfg, root, base = build(tmp_path, make_cfg, TIMELINE, {"web/app.py": RENAMED_AND_MOVED}, "web.app:app", shape="ci")
    assert ("GET", "/feed", "feed", "web/app.py", "framework") in rows(cfg, "routes")        # the head tree
    assert ("GET", "/timeline", "timeline", "web/app.py", "framework") in rows(cfg, "baseline_routes")
    routes, results = results_for(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    assert routes[0].route_note == ""
    r = results["GET /timeline"]
    assert r.status == diff.DIFFERENT and "404" in r.differences[0]["after"]


def test_b5_without_baseline_rows_the_old_decorators_are_read_with_a_prefix_caveat(tmp_path, make_cfg):
    cfg, root, base = build(tmp_path, make_cfg, TIMELINE, {"web/app.py": RENAMED_AND_MOVED}, "web.app:app", shape="head")
    assert rows(cfg, "baseline_routes") == []
    routes, results = results_for(cfg, root, base, "web/app.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    r = results["GET /timeline"]
    assert r.status == diff.DIFFERENT
    assert "may lack a router or blueprint prefix" in r.reason and "timeline" in r.reason


BLUEPRINT = {
    "web/__init__.py": "",
    "web/views.py": """
from flask import Blueprint, jsonify

bp = Blueprint("things", __name__)


@bp.get("/timeline")
def timeline():
    return jsonify(items=[1, 2])
""",
    "web/app.py": """
from flask import Flask
from web.views import bp

app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/api")
""",
}


def test_b5_decorator_path_without_its_blueprint_prefix_stays_untested_with_the_caveat(tmp_path, make_cfg):
    # The decorator says /timeline; the app serves /api/timeline. Requests to /timeline get 404 in both trees and never
    # run the old handler: untested, never verified, and the reason says why the path may be wrong.
    new = BLUEPRINT["web/views.py"].replace("def timeline", "def feed").replace('"/timeline"', '"/feed"')
    cfg, root, base = build(tmp_path, make_cfg, BLUEPRINT, {"web/views.py": new}, "web.app:app", shape="head")
    routes, results = results_for(cfg, root, base, "web/views.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    r = results["GET /timeline"]
    assert r.status == diff.UNTESTED, r.reason
    assert "may lack a router or blueprint prefix" in r.reason


def test_b5_blueprint_prefix_is_known_through_baseline_routes(tmp_path, make_cfg):
    new = BLUEPRINT["web/views.py"].replace("def timeline", "def feed").replace('"/timeline"', '"/feed"')
    cfg, root, base = build(tmp_path, make_cfg, BLUEPRINT, {"web/views.py": new}, "web.app:app", shape="ci")
    routes, results = results_for(cfg, root, base, "web/views.py")
    assert [r.qualname for r in routes] == ["GET /api/timeline"]
    r = results["GET /api/timeline"]
    assert r.status == diff.DIFFERENT and "404" in r.differences[0]["after"]
    assert "prefix" not in r.reason


# ---------------------------------------------------------------- FastAPI variants

FAST = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI

app = FastAPI()


@app.get("/timeline")
def timeline():
    return {"items": [1, 2]}


@app.get("/things/{thing_id}")
def read_thing(thing_id: int):
    return {"id": thing_id}
""",
}


def test_b1_fastapi_renamed_handler_with_a_new_path_is_different(tmp_path, make_cfg):
    new = FAST["api/main.py"].replace('@app.get("/timeline")\ndef timeline', '@app.get("/feed")\ndef feed')
    cfg, root, base = build(tmp_path, make_cfg, FAST, {"api/main.py": new}, "api.main:app", shape="ci")
    routes, results = results_for(cfg, root, base, "api/main.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    r = results["GET /timeline"]
    assert r.status == diff.DIFFERENT and "404" in r.differences[0]["after"]


def test_b2_fastapi_renamed_handler_on_the_same_path_is_verified(tmp_path, make_cfg):
    new = FAST["api/main.py"].replace("def read_thing", "def get_thing")
    cfg, root, base = build(tmp_path, make_cfg, FAST, {"api/main.py": new}, "api.main:app")
    routes, results = results_for(cfg, root, base, "api/main.py")
    assert [r.qualname for r in routes] == ["GET /things/{thing_id}"]
    r = results["GET /things/{thing_id}"]
    assert r.status == diff.DONE, r.reason
    assert r.requests_entered == r.inputs_compared > 1    # requests built from the old handler's int parameter


def test_b3_fastapi_deleted_handler_is_different(tmp_path, make_cfg):
    new = FAST["api/main.py"].replace('@app.get("/things/{thing_id}")\ndef read_thing(thing_id: int):\n    return {"id": thing_id}\n', "")
    cfg, root, base = build(tmp_path, make_cfg, FAST, {"api/main.py": new}, "api.main:app", shape="ci")
    routes, results = results_for(cfg, root, base, "api/main.py")
    assert [r.qualname for r in routes] == ["GET /things/{thing_id}"]
    r = results["GET /things/{thing_id}"]
    assert r.status == diff.DIFFERENT and "404" in r.differences[0]["after"]
    assert r.statements_reached == 1


# ---------------------------------------------------------------- delete plus add, moved handlers

def test_delete_plus_add_serving_the_same_path_is_a_rename(tmp_path, make_cfg):
    # The body changed too, so symbol_diff cannot pair them; the new function's decorator serves the old METHOD and path.
    new = TIMELINE["web/app.py"].replace("def timeline():\n    return jsonify(items=[1, 2])",
                                         "def feed():\n    return jsonify(items=[1, 2, 3])")
    cfg, root, base = build(tmp_path, make_cfg, TIMELINE, {"web/app.py": new}, "web.app:app")
    routes, results = results_for(cfg, root, base, "web/app.py")
    (t,) = routes
    assert t.qualname == "GET /timeline" and t.handler["qualname"] == "feed" and t.entry_gate
    r = results["GET /timeline"]
    assert r.status == diff.DIFFERENT and "3" in r.differences[0]["after"]
    assert r.statements_reached == 1 and r.unreached_note == ""


SPLIT = {
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask

app = Flask(__name__)

from web import views, more  # noqa: E402,F401
""",
    "web/views.py": """
from flask import jsonify
from web.app import app


@app.get("/timeline")
def timeline():
    return jsonify(items=[1, 2])
""",
    "web/more.py": """
from flask import jsonify
from web.app import app


@app.get("/other")
def other():
    return jsonify(ok=True)
""",
}
VIEWS_EMPTY = "from flask import jsonify\nfrom web.app import app\n"


def test_handler_moved_to_another_file_is_verified_when_the_old_handler_ran(tmp_path, make_cfg):
    moved = SPLIT["web/more.py"] + '\n\n@app.get("/timeline")\ndef timeline():\n    return jsonify(items=[1, 2])\n'
    cfg, root, base = build(tmp_path, make_cfg, SPLIT, {"web/views.py": VIEWS_EMPTY, "web/more.py": moved}, "web.app:app")
    routes, results = results_for(cfg, root, base, "web/views.py")
    assert [r.qualname for r in routes] == ["GET /timeline"]
    r = results["GET /timeline"]
    assert r.status == diff.DONE, r.reason
    assert r.statements_reached == r.statements_total == 1
    assert diff.reached_text(r).startswith("the old handler ran (")


def test_handler_moved_and_changed_is_different(tmp_path, make_cfg):
    moved = SPLIT["web/more.py"] + '\n\n@app.get("/timeline")\ndef timeline():\n    return jsonify(items=[2, 1])\n'
    cfg, root, base = build(tmp_path, make_cfg, SPLIT, {"web/views.py": VIEWS_EMPTY, "web/more.py": moved}, "web.app:app", shape="ci")
    _, results = results_for(cfg, root, base, "web/views.py")
    assert results["GET /timeline"].status == diff.DIFFERENT


def test_a4_decorator_only_path_change_is_not_a_rename(tmp_path, make_cfg):
    # Same function name, new path: decorators_changed, handled by route_targets from the routes table as in 7.2a (A4).
    # baseline_routes adds nothing for it, in either index shape.
    src = TIMELINE["web/app.py"]
    new = src.replace('@app.get("/timeline")', '@app.get("/feed")')
    for shape, expected in (("session", "GET /timeline"), ("ci", "GET /feed")):
        cfg, root, base = build(tmp_path / shape, make_cfg, TIMELINE, {"web/app.py": new}, "web.app:app", shape=shape)
        assert engine.removed_route_targets(cfg, "web/app.py", src.lstrip("\n"), new.lstrip("\n")) == []
        routes = [t for t in engine.targets_for(cfg, "web/app.py", {"findings": []}) if t.kind == "route"]
        assert [r.qualname for r in routes] == [expected]


# ---------------------------------------------------------------- lookup order, table filling, planning

def test_old_handler_lookup_order(tmp_path):
    conn = db.connect(str(tmp_path))
    decorators = ['app.get("/plain")']
    try:
        assert index_routes.old_handler_routes(conn, "web/app.py", "h", decorators) == [("GET", "/plain", "decorator")]
        conn.execute("INSERT INTO routes VALUES ('GET', '/head/h', 'h', 'web/app.py', 'framework')")
        conn.execute("INSERT INTO routes VALUES ('ANY', '/dj/h', 'h', 'web/app.py', 'framework')")
        assert index_routes.old_handler_routes(conn, "web/app.py", "h", decorators) == [("GET", "/head/h", "framework")]
        conn.execute("INSERT INTO baseline_routes VALUES ('POST', '/base/h', 'h', 'web/app.py', 'framework')")
        conn.execute("INSERT INTO baseline_routes VALUES ('GET', '/base/h', 'h', 'web/app.py', 'framework')")
        conn.execute("INSERT INTO baseline_routes VALUES ('GET', '/base/g', 'g', 'web/app.py', 'framework')")
        assert index_routes.old_handler_routes(conn, "web/app.py", "h", decorators) == [
            ("GET", "/base/h", "framework"), ("POST", "/base/h", "framework")]
        # A decorator row in an index (introspection failed when it was built) keeps its source, and so the caveat.
        conn.execute("INSERT INTO baseline_routes VALUES ('GET', '/d', 'k', 'web/app.py', 'decorator')")
        assert index_routes.old_handler_routes(conn, "web/app.py", "k", []) == [("GET", "/d", "decorator")]
        assert index_routes.old_handler_routes(None, "web/app.py", "k", decorators) == [("GET", "/plain", "decorator")]
    finally:
        conn.close()


def _session_start():
    spec = importlib.util.spec_from_file_location("lg_session_start_hook", PLUGIN / "scripts" / "session_start.py")
    import sys
    sys.path.insert(0, str(PLUGIN / "scripts"))
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(PLUGIN / "scripts"))
    return module


def test_session_start_copies_routes_into_baseline_routes(tmp_path, make_cfg):
    root = write_tree(tmp_path / "proj", TIMELINE)
    cfg = make_cfg(root, app="web.app:app")
    _git(root)
    assert _session_start().start_session(cfg, {"session_id": "s1", "source": "startup"}) is None
    expected = [("GET", "/other", "other", "web/app.py", "framework"), ("GET", "/timeline", "timeline", "web/app.py", "framework")]
    assert rows(cfg, "routes") == rows(cfg, "baseline_routes") == expected
    # A later refresh on the edited tree rebuilds routes; baseline_routes still describes the baseline.
    (root / "web/app.py").write_text(RENAMED_AND_MOVED.lstrip("\n"))
    indexer_main.refresh(cfg)
    assert ("GET", "/feed", "feed", "web/app.py", "framework") in rows(cfg, "routes")
    assert rows(cfg, "baseline_routes") == expected


def test_ci_run_fills_baseline_routes_from_the_base_and_reports_the_renamed_route(tmp_path):
    root = write_tree(tmp_path / "proj", TIMELINE)
    _git(root)
    (root / "web/app.py").write_text(RENAMED_AND_MOVED.lstrip("\n"))
    cfg, out = ci.run(str(root), "HEAD", TARGET_PY, overrides={"app": "web.app:app", "behavioral_diff": {"mutants": 0}})
    assert ("GET", "/timeline", "timeline", "web/app.py", "framework") in rows(cfg, "baseline_routes")
    assert ("GET", "/feed", "feed", "web/app.py", "framework") in rows(cfg, "routes")
    by = {r.qualname: r for r in out.results}
    assert by["GET /timeline"].status == diff.DIFFERENT
    assert "GET /feed" not in by
    assert any("route removed: GET /timeline" in b.text() for _, b in out.breaks)
    # Without indexing, rows from an earlier run describe another base: they are cleared.
    ci.run(str(root), "HEAD", TARGET_PY, index=False, overrides={"app": "web.app:app", "behavioral_diff": {"mutants": 0}})
    assert rows(cfg, "baseline_routes") == []


def test_introspect_asks_the_given_tree(tmp_path, make_cfg):
    root = write_tree(tmp_path / "proj", {**TIMELINE, "web/app.py": RENAMED_AND_MOVED})
    cfg = make_cfg(root, app="web.app:app")
    base = write_tree(tmp_path / "base", TIMELINE)
    head = {r[2] for r in index_routes.introspect(cfg)}
    old = {r[2] for r in index_routes.introspect(cfg, root=str(base))}
    assert "feed" in head and "timeline" not in head
    assert "timeline" in old and "feed" not in old


def test_removed_route_targets_need_an_app_and_skip_routes_already_planned(tmp_path, make_cfg):
    src = TIMELINE["web/app.py"].lstrip("\n")
    new = RENAMED_AND_MOVED.lstrip("\n")
    root = write_tree(tmp_path / "proj", TIMELINE)
    assert engine.removed_route_targets(make_cfg(root), "web/app.py", src, new) == []
    cfg = make_cfg(root, app="web.app:app")
    (t,) = engine.removed_route_targets(cfg, "web/app.py", src, new)
    assert t.qualname == "GET /timeline" and t.route_note    # no index: the decorator, with the caveat
    assert engine.removed_route_targets(cfg, "web/app.py", src, new, {"GET /timeline"}) == []
    deleted = engine.removed_route_targets(cfg, "web/app.py", src, DELETED.lstrip("\n"))
    assert deleted[0].code_hash != t.code_hash
    assert deleted[0].callee_old_gate == [[8, 8]]     # the old body: `return jsonify(...)`


# ---------------------------------------------------------------- review round 1: a deleted handler's own 404

OWN_404_FAST = {
    "api/__init__.py": "",
    "api/main.py": """
from fastapi import FastAPI, HTTPException

app = FastAPI()
THINGS = {}


@app.get("/things/{thing_id}")
def read_thing(thing_id: int):
    if thing_id not in THINGS:
        raise HTTPException(status_code=404, detail="Not Found")
    return THINGS[thing_id]


@app.get("/other")
def other():
    return {"ok": True}
""",
}

OWN_404_FLASK = {
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask, abort, jsonify

app = Flask(__name__)
THINGS = {}


@app.get("/things/<int:thing_id>")
def read_thing(thing_id):
    if thing_id not in THINGS:
        abort(404)
    return jsonify(THINGS[thing_id])


@app.get("/other")
def other():
    return jsonify(ok=True)
""",
}


def _drop_things(src: str) -> str:
    head, _, rest = src.partition('@app.get("/things')
    return head + '@app.get("/other")' + rest.split('@app.get("/other")', 1)[1]


def test_deleted_handler_that_answered_404_itself_is_not_verified_fastapi(tmp_path, make_cfg):
    # The old handler ran and answered 404 for the unknown ids; the edited app answers the same 404 from its router.
    # Agreeing responses must not make the removed route "verified".
    cfg, root, base = build(tmp_path, make_cfg, OWN_404_FAST, {"api/main.py": _drop_things(OWN_404_FAST["api/main.py"])},
                            "api.main:app", shape="ci")
    _, results = results_for(cfg, root, base, "api/main.py")
    r = results["GET /things/{thing_id}"]
    assert r.status == diff.UNTESTED, r.reason
    assert "no longer serves GET /things/{thing_id}" in r.reason and "404 in both versions" in r.reason
    assert r.statements_reached == 1 and r.route_deleted


def test_deleted_handler_that_answered_404_itself_is_not_verified_flask(tmp_path, make_cfg):
    cfg, root, base = build(tmp_path, make_cfg, OWN_404_FLASK, {"web/app.py": _drop_things(OWN_404_FLASK["web/app.py"])},
                            "web.app:app", shape="ci")
    _, results = results_for(cfg, root, base, "web/app.py")
    r = results["GET /things/<int:thing_id>"]
    assert r.status == diff.UNTESTED, r.reason
    assert "no longer serves" in r.reason and "the old handler read_thing ran" in r.reason


def test_deleted_handler_texts_say_the_old_handler_ran():
    r = diff.Result("web/views.py", "GET /timeline", diff.DONE, "h", inputs_compared=2, statements_reached=1,
                    statements_total=1, route=True, requests_reached=2, route_deleted=True)
    assert diff.reached_text(r) == "the old handler ran (2 of 2 requests ran it)"
    assert "1 of 1 changed statements" not in diff.format_note([r])
