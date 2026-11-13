"""Whole API routes, old against new, through the frameworks' in-process test clients (roadmap C3)."""
import subprocess

import pytest

from guardlib import engine
from indexer import __main__ as indexer_main
from runner import diff, routes_diff

from conftest import write_tree

FASTAPI = {
    "api/__init__.py": "",
    "api/logic.py": "def price_label(n: int) -> str:\n    return 'cheap' if n < 10 else 'pricey'\n",
    "api/main.py": """
from fastapi import FastAPI
from api.logic import price_label

app = FastAPI()


@app.get("/items/{item_id}")
def read_item(item_id: int, q: str | None = None):
    return {"id": item_id, "label": price_label(item_id), "q": q}


@app.post("/orders")
def create_order(order: dict):
    return {"total": order.get("qty", 0) * 3}
""",
    "tests/test_api.py": """
from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)


def test_item():
    assert client.get("/items/5").json()["label"] == "cheap"


def test_order():
    assert client.post("/orders", json={"qty": 2}).json() == {"total": 6}
""",
}

FLASK = {
    "web/__init__.py": "",
    "web/app.py": """
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/greet/<name>")
def greet(name):
    return jsonify(message=f"hello {name}")
""",
}


def test_path_helpers():
    assert routes_diff.path_params("/items/{item_id}/tags/{tag:path}") == [("item_id", ""), ("tag", "")]
    assert routes_diff.path_params("/u/<int:uid>/<name>") == [("uid", "int"), ("name", "")]
    assert routes_diff.template_regex("/items/{id}").match("/items/7")
    assert not routes_diff.template_regex("/items/{id}").match("/items/7/x")
    assert routes_diff.fill("/u/<int:uid>", {"uid": 3}) == "/u/3"


def build(tmp_path, make_cfg, files, new_files, app):
    root = write_tree(tmp_path / "proj", {**files, **new_files})
    cfg = make_cfg(root, app=app)
    base = write_tree(cfg.state_dir / "baseline", files)
    (root / ".gitignore").write_text(".legacyguard/\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    indexer_main.refresh(cfg)
    return cfg, root, base


def route_results(cfg, root, base, rel):
    changed = diff.changed_targets(rel, (base / rel).read_text(), (root / rel).read_text())
    deps = engine.dependents(cfg, rel, changed, {t.qualname for t in changed})
    routes = engine.route_targets(cfg, changed + deps)
    return routes, {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps + routes, harvest={})}


def test_fastapi_response_field_rename_is_caught(tmp_path, make_cfg):
    new = FASTAPI["api/main.py"].replace('"label": price_label', '"name": price_label')
    cfg, root, base = build(tmp_path, make_cfg, FASTAPI, {"api/main.py": new}, "api.main:app")
    routes, results = route_results(cfg, root, base, "api/main.py")
    assert {r.qualname for r in routes} >= {"GET /items/{item_id}"}
    r = results["GET /items/{item_id}"]
    assert r.status == diff.DIFFERENT
    assert any(d["call"].startswith("GET /items/5") for d in r.differences)  # the test's own request comes first


def test_fastapi_body_change_from_test_request(tmp_path, make_cfg):
    new = FASTAPI["api/main.py"].replace('* 3}', '* 4}')
    cfg, root, base = build(tmp_path, make_cfg, FASTAPI, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    r = results["POST /orders"]
    assert r.status == diff.DIFFERENT
    assert r.differences[0]["call"] == 'POST /orders json={"qty": 2}'


def test_route_reached_through_a_caller(tmp_path, make_cfg):
    # Only logic.py changes; the route is found because its handler calls the edited function.
    new = FASTAPI["api/logic.py"].replace("'cheap' if n < 10", "'cheap' if n < 3")
    cfg, root, base = build(tmp_path, make_cfg, FASTAPI, {"api/logic.py": new}, "api.main:app")
    routes, results = route_results(cfg, root, base, "api/logic.py")
    assert results["GET /items/{item_id}"].status == diff.DIFFERENT


def test_unchanged_route_behavior_is_verified(tmp_path, make_cfg):
    new = FASTAPI["api/main.py"].replace('return {"total": order.get("qty", 0) * 3}', 'qty = order.get("qty", 0)\n    return {"total": qty * 3}')
    cfg, root, base = build(tmp_path, make_cfg, FASTAPI, {"api/main.py": new}, "api.main:app")
    _, results = route_results(cfg, root, base, "api/main.py")
    assert results["POST /orders"].status == diff.DONE


def test_flask_route(tmp_path, make_cfg):
    new = FLASK["web/app.py"].replace('f"hello {name}"', 'f"hi {name}"')
    cfg, root, base = build(tmp_path, make_cfg, FLASK, {"web/app.py": new}, "web.app:app")
    _, results = route_results(cfg, root, base, "web/app.py")
    r = results["GET /greet/<name>"]
    assert r.status == diff.DIFFERENT and "hello" in r.differences[0]["before"]
