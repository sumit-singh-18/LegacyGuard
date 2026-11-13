from guardlib import checks

TEST_BEFORE = '''
def test_total():
    assert total([1, 2]) == 3
    assert total([]) == 0
'''


def items(breaks):
    return {b.item for b in breaks}


def test_assertion_removed():
    after = TEST_BEFORE.replace("    assert total([]) == 0\n", "")
    assert "assertion_removed:test_total" in items(checks.check_test_file("tests/test_x.py", TEST_BEFORE, after))


def test_assertion_weakened():
    after = TEST_BEFORE.replace("assert total([]) == 0", "assert total([])")
    assert "assertion_weakened:test_total" in items(checks.check_test_file("tests/test_x.py", TEST_BEFORE, after))


def test_test_deleted_and_disabled():
    deleted = checks.check_test_file("tests/test_x.py", TEST_BEFORE, "")
    assert "deleted:test_total" in items(deleted)
    skipped = "import pytest\n@pytest.mark.skip\n" + TEST_BEFORE.lstrip("\n")
    assert "disabled:test_total" in items(checks.check_test_file("tests/test_x.py", TEST_BEFORE, skipped))


def test_unchanged_test_is_fine():
    assert checks.check_test_file("tests/test_x.py", TEST_BEFORE, TEST_BEFORE) == []


ROUTES = '''
@app.get("/items/{item_id}")
def read_item(item_id: int, q: str = None):
    return {"id": item_id}

@app.post("/items")
def create_item(body: dict):
    return body
'''


def test_route_removed_and_param_removed():
    no_route = ROUTES.replace('@app.post("/items")\n', "")
    assert any(i.startswith("route:") for i in items(checks.check_python_source("api.py", ROUTES, no_route, None, None)))
    no_param = ROUTES.replace("item_id: int, q: str = None", "item_id: int")
    assert any(i.startswith("param:") and i.endswith(":q") for i in items(checks.check_python_source("api.py", ROUTES, no_param, None, None)))


def test_is_test_path():
    assert checks.is_test_path("tests/test_a.py")
    assert checks.is_test_path("pkg/a_test.py")
    assert not checks.is_test_path("pkg/testing_utils.py")
