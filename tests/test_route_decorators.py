"""Sprint 7.2d: one decorator parser for the static route check and the index fallback, Flask `.route` included."""
import subprocess

from guardlib import checks, route_decorators
from indexer import __main__ as indexer_main
from indexer import db

from conftest import write_tree


def test_shortcuts_give_their_method():
    assert route_decorators.parse('app.get("/items/{item_id}")') == [("app", "GET", "/items/{item_id}")]
    assert route_decorators.parse("router.delete('/x', status_code=204)") == [("router", "DELETE", "/x")]
    assert route_decorators.parse('api.v1.post("/y")') == [("api.v1", "POST", "/y")]


def test_flask_route_defaults_to_get():
    assert route_decorators.parse('app.route("/x")') == [("app", "GET", "/x")]


def test_route_methods_list_tuple_and_case():
    assert route_decorators.parse('bp.route("/x", methods=["GET", "POST"])') == [("bp", "GET", "/x"), ("bp", "POST", "/x")]
    assert route_decorators.parse('bp.route("/x", methods=("put",))') == [("bp", "PUT", "/x")]
    # HEAD and OPTIONS are added by Flask on its own; they are left out as the framework introspection does.
    assert route_decorators.parse('bp.route("/x", methods=["GET", "HEAD", "OPTIONS"])') == [("bp", "GET", "/x")]


def test_api_route_and_keyword_paths():
    assert route_decorators.parse('router.api_route("/x", methods=["PATCH"])') == [("router", "PATCH", "/x")]
    assert route_decorators.parse('app.route(rule="/r", methods=["DELETE"])') == [("app", "DELETE", "/r")]
    assert route_decorators.parse('router.get(path="/p")') == [("router", "GET", "/p")]


def test_unknown_methods_and_non_routes_give_nothing():
    assert route_decorators.parse('app.route("/x", methods=METHODS)') == []
    assert route_decorators.parse('app.route("/x", methods=["GET", verb])') == []
    assert route_decorators.parse("functools.cache") == []
    assert route_decorators.parse("staticmethod") == []
    assert route_decorators.parse('cache.get("key")') == []          # not a path
    assert route_decorators.parse('make_app().get("/x")') == []      # owner is not a plain name
    assert route_decorators.parse('app.route(PATH)') == []
    assert route_decorators.parse("app.get(") == []                  # does not parse


def test_multiline_decorator_with_comment():
    dec = 'app.route(\n    "/x",  # the list\n    methods=["POST"],\n)'
    assert route_decorators.parse(dec) == [("app", "POST", "/x")]


FLASK_VIEWS = '''
@bp.route("/x", methods=["GET", "POST"])
def things():
    return "ok"

@bp.route("/y")
def other():
    return "y"
'''


def test_static_check_sees_flask_routes_and_narrowed_methods():
    narrowed = FLASK_VIEWS.replace('methods=["GET", "POST"]', 'methods=["GET"]')
    found = [b for b in checks.check_python_source("web/views.py", FLASK_VIEWS, narrowed, None, None) if b.item.startswith("route:")]
    assert [b.subject for b in found] == ["route removed: POST /x (handler things)"]
    gone = FLASK_VIEWS.replace('@bp.route("/y")\n', "")
    assert any(b.subject == "route removed: GET /y (handler other)"
               for b in checks.check_python_source("web/views.py", FLASK_VIEWS, gone, None, None))


def test_index_fallback_finds_flask_route_handlers(tmp_path, make_cfg):
    root = write_tree(tmp_path / "proj", {"web/__init__.py": "", "web/views.py": FLASK_VIEWS})
    cfg = make_cfg(root)  # no app: the routes table comes from decorators
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    report = indexer_main.refresh(cfg)
    assert report["routes"]["source"] == "decorators"
    conn = db.connect(cfg.root)
    try:
        rows = {tuple(r) for r in conn.execute("SELECT method, path, handler_qualname, source FROM routes")}
    finally:
        conn.close()
    assert rows == {("GET", "/x", "things", "decorator"), ("POST", "/x", "things", "decorator"), ("GET", "/y", "other", "decorator")}
