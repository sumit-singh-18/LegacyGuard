"""Databases and HTTP clients answered by stand-ins; the calls each version makes are compared (roadmap C1)."""
from runner import diff

from conftest import call, write_tree

SRC = '''
import sqlite3
import requests


def deactivate(conn, user_id: int) -> None:
    conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    conn.commit()


def count_orders(cursor, status: str) -> int:
    cursor.execute("SELECT COUNT(*) FROM orders WHERE status = %s", (status,))
    row = cursor.fetchone()
    return row[0] if row else 0


def archive(path: str, days: int) -> int:
    db = sqlite3.connect(path)
    db.execute("DELETE FROM events WHERE age > ?", (days,))
    db.commit()
    return days


def fetch_user(uid: int) -> dict:
    return requests.get(f"https://api.example.com/users/{uid}", params={"v": 2}).json()


def notify(uid: int) -> int:
    resp = requests.post("https://hooks.example.com/notify", json={"user": uid, "kind": "welcome"})
    return resp.status_code
'''


def run(tmp_path, make_cfg, new_src, **bd):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new_src})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/m.py": SRC})
    cfg = make_cfg(root, behavioral_diff={"mutants": 0, **bd})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_worker_records_sql(tmp_path, run_worker):
    root = write_tree(tmp_path / "w", {"m.py": SRC})
    out = run_worker(root, [{"module": "m", "qualname": "archive", "inputs": [call("app.db", 30)]}], io="stub")["m:archive"]
    assert out["results"][0]["io"] == [{"io": "sql", "sql": "DELETE FROM events WHERE age > ?", "params": [30]},
                                       {"io": "sql", "sql": "COMMIT"}]
    assert not (root / "app.db").exists()


def test_dropped_commit_is_caught(tmp_path, make_cfg):
    r = run(tmp_path, make_cfg, SRC.replace("    conn.commit()\n", ""))["deactivate"]
    assert r.status == diff.DIFFERENT
    d = r.differences[0]
    assert "SQL COMMIT" in d["before"] and "(none)" in d["after"]


def test_changed_where_clause_is_caught(tmp_path, make_cfg):
    r = run(tmp_path, make_cfg, SRC.replace("WHERE status = %s", "WHERE status != %s"))["count_orders"]
    assert r.status == diff.DIFFERENT and "status != %s" in r.differences[0]["after"]


def test_changed_http_request_is_caught(tmp_path, make_cfg):
    r = run(tmp_path, make_cfg, SRC.replace('params={"v": 2}', 'params={"v": 3}'))["fetch_user"]
    assert r.status == diff.DIFFERENT and '"v": 3' in r.differences[0]["after"]
    r2 = run(tmp_path / "b", make_cfg, SRC.replace('"kind": "welcome"', '"kind": "hello"'))["notify"]
    assert r2.status == diff.DIFFERENT and "hello" in r2.differences[0]["after"]


def test_refactor_with_same_calls_passes_and_says_so(tmp_path, make_cfg):
    new = SRC.replace('    conn.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))',
                      '    sql = "UPDATE users SET active = 0 WHERE id = ?"\n    conn.execute(sql, (user_id,))')
    r = run(tmp_path, make_cfg, new)["deactivate"]
    assert r.status == diff.DONE and r.inputs_with_io > 0
    assert "answered by stand-ins" in diff.format_note([r])


def test_block_mode_keeps_old_behavior(tmp_path, make_cfg):
    r = run(tmp_path, make_cfg, SRC.replace("    conn.commit()\n", ""), io="block")["deactivate"]
    assert r.status == diff.UNTESTED and "database" in r.reason
