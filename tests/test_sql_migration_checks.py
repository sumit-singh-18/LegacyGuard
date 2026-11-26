"""Sprint 7.6 Part C, tests 1-4: today's regex path for SQL migrations, pinned before squawk was added (no squawk)."""
from __future__ import annotations

import os

import pytest

import squawk_fake as fake
from guardlib import checks
from guardlib import contracts_parse as cp

from conftest import write_tree

CASES = [  # (after text, item, subject)
    ("ALTER TABLE t DROP COLUMN c;\n", "stmt:column dropped:c", "column dropped: c in a migration"),
    ("ALTER TABLE t RENAME COLUMN a TO b;\n", "stmt:column renamed:a", "column renamed: a in a migration"),
    ("DROP TABLE IF EXISTS accounts;\n", "stmt:table dropped:accounts", "table dropped: accounts in a migration"),
    ("ALTER TABLE t ALTER COLUMN x TYPE text;\n", "stmt:column retyped:x", "column retyped: x in a migration"),
    ("ALTER TABLE t RENAME TO u;\n", "stmt:table renamed:t", "table renamed: t in a migration"),
]


# 1
def test_drop_column_is_one_regex_item():
    out = checks.check_migration("db/0002.sql", [], [], "", "ALTER TABLE t DROP COLUMN c;\n")
    assert [(b.item, b.subject, b.source, b.kind, b.line) for b in out] == [
        ("stmt:column dropped:c", "column dropped: c in a migration", "schema", "db", None)]


# 2
@pytest.mark.parametrize("after,item,subject", CASES)
def test_each_statement_once_and_not_when_old_or_commented(after, item, subject):
    out = checks.check_migration("db/0002.sql", [], [], "", after)
    assert [(b.item, b.subject) for b in out] == [(item, subject)]
    assert checks.check_migration("db/0002.sql", [], [], after, after + "SELECT 1;\n") == []
    assert checks.check_migration("db/0002.sql", [], [], "", "-- " + after) == []


# 3
def test_spans_match_statements_and_point_at_the_keyword():
    corpus = ("-- ALTER TABLE z DROP COLUMN hidden;\n" + "".join(a for a, _, _ in CASES)
              + "operations = [migrations.RemoveField(model_name='item', name='price')]\n")
    spans = cp.dangerous_statement_spans(corpus)
    assert set(spans) == set(cp.dangerous_statements(corpus))
    assert "column dropped:price" in spans and "column dropped:hidden" not in spans
    expect = {"column dropped:c": "DROP COLUMN c", "column renamed:a": "RENAME COLUMN a", "table dropped:accounts": "DROP TABLE",
              "column retyped:x": "ALTER COLUMN x", "table renamed:t": "ALTER TABLE t RENAME", "column dropped:price": "migrations.RemoveField"}
    for item, offsets in spans.items():
        assert len(offsets) == 1
        assert corpus[offsets[0]:].startswith(expect[item]), item
    # offsets are into the original text: the leading comment was blanked, not removed
    assert spans["column dropped:c"][0] == corpus.index("ALTER TABLE t DROP COLUMN c") + len("ALTER TABLE t ")


# 4
@pytest.mark.parametrize("after,item,subject", CASES)
def test_squawk_switched_off_is_today_exactly(tmp_path, make_cfg, monkeypatch, after, item, subject):
    root = write_tree(tmp_path / "proj", {"db/0001_init.sql": "CREATE TABLE t (id bigint, c int);\n"})
    cfg = make_cfg(root, contracts={"db": "db"}, contract_tools={"squawk": False})
    bin_dir = fake.install(tmp_path / "fake")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    plain = checks.run_checks(cfg, "db/0002.sql", "", after, {})
    notes: list = []
    with_trees = checks.run_checks(cfg, "db/0002.sql", "", after, {}, trees=(str(root), str(root)), notes=notes, tool_state={})
    assert plain == with_trees == checks.check_migration("db/0002.sql", [], [], "", after)
    assert [(b.item, b.subject) for b in with_trees] == [(item, subject)]
    assert notes == [] and fake.calls(tmp_path / "fake") == []
