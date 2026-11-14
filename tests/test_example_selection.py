"""Sprint 6 Task 6.2: example files that import the function from its own module come first, and test arguments built
through pytest fixtures (conftest included) and str.format / f-strings resolve.

Mirrors thefuck 7 and thefuck 1 from validation run 2 (docs/validation/2026-11-run2.md, misses 4 and 5): `match` is a
common name, so files ranked by call count alone never include the rule's own small test file."""
import subprocess

import pytest

from indexer import __main__ as indexer_main
from runner import diff, examples

from conftest import write_tree


TYPES = '''
class Command:
    def __init__(self, script, output):
        self.script = script
        self.output = output
'''
PHP_OLD = '''
def match(command):
    return "php -s" in command.script
'''
PHP_NEW = PHP_OLD.replace('"php -s" in command.script', '" -s " in command.script')
SED = '''
def match(command):
    return "sed" in command.script
'''
OWN_TEST = '''
from pkg_tf.rules.php_s import match
from pkg_tf.types import Command


def test_match():
    assert match(Command('php -t pub -s 0.0.0.0:8080', ''))
'''


def other_test(i: int) -> str:
    calls = "\n".join(f"    assert match(Command('sed s/a{i}/b{j}/ file{j}', 'sed: unterminated'))" for j in range(6))
    return f"from pkg_tf.rules.sed import match\nfrom pkg_tf.types import Command\n\n\ndef test_match_{i}():\n{calls}\n"


BASE_FILES = {"pkg_tf/__init__.py": "", "pkg_tf/rules/__init__.py": "", "pkg_tf/types.py": TYPES,
              "pkg_tf/rules/sed.py": SED, "tests/rules/test_php_s.py": OWN_TEST,
              **{f"tests/rules/test_a{i:02d}.py": other_test(i) for i in range(10)}}
OWN = "pkg_tf/rules/php_s.py"


def common_name_repo(tmp_path, make_cfg, with_index: bool):
    base = write_tree(tmp_path / "base", {**BASE_FILES, OWN: PHP_OLD})
    work = write_tree(tmp_path / "work", {**BASE_FILES, OWN: PHP_NEW})
    cfg = make_cfg(work)
    if with_index:
        (work / ".gitignore").write_text(".legacyguard/\n")
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
            subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)
        indexer_main.refresh(cfg)
    examples._TEST_FILES.clear()
    targets = diff.changed_targets(OWN, (base / OWN).read_text(), (work / OWN).read_text())
    return cfg, str(base), str(work), targets


# ---- selection ----

def test_index_ranks_own_module_test_first(tmp_path, make_cfg):
    cfg, _, _, _ = common_name_repo(tmp_path, make_cfg, with_index=True)
    from indexer import db
    conn = db.connect(cfg.root)
    try:
        counts = dict(conn.execute("SELECT file, COUNT(*) FROM calls WHERE called_name='match' GROUP BY file").fetchall())
        assert counts["tests/rules/test_a00.py"] > counts["tests/rules/test_php_s.py"]  # call count alone would drop it
        files = examples.calling_files(conn, "match", OWN)  # the root comes from the index's own location
        assert files[0] == "tests/rules/test_php_s.py" and len(files) == examples.MAX_FILES
        assert examples.calling_files(conn, "match", OWN, root=cfg.root)[0] == "tests/rules/test_php_s.py"
        sed_files = examples.calling_files(conn, "match", "pkg_tf/rules/sed.py")
        assert sed_files == [f"tests/rules/test_a{i:02d}.py" for i in range(examples.MAX_FILES)]
    finally:
        conn.close()


def test_no_index_fallback_ranks_own_module_test_first(tmp_path, make_cfg):
    cfg, _, _, _ = common_name_repo(tmp_path, make_cfg, with_index=False)
    files = examples.files_naming(cfg.root, "match", own_file=OWN)
    assert files[0] == "tests/rules/test_php_s.py" and OWN not in files
    assert examples.files_naming(cfg.root, "match")[:2] == ["tests/rules/test_a00.py", "tests/rules/test_a01.py"]  # unchanged


def test_param_objects_take_own_module_tests_first(tmp_path, make_cfg):
    cfg, _, _, targets = common_name_repo(tmp_path, make_cfg, with_index=False)
    others = [f"tests/rules/test_a{i:02d}.py" for i in range(examples.MAX_FILES)]
    objects = examples.param_objects(cfg.root, OWN, "match", targets[0].params, others)
    assert repr(objects["command"][0]) == "Command('php -t pub -s 0.0.0.0:8080', '')"


@pytest.mark.parametrize("text", [
    "import pkg_tf.rules.php_s\n\ndef test_m():\n    pkg_tf.rules.php_s.match(None)\n",
    "import pkg_tf.rules.php_s as rule\n\ndef test_m():\n    rule.match(None)\n",
    "from pkg_tf.rules import php_s\n\ndef test_m():\n    php_s.match(None)\n",
    "from pkg_tf.rules.php_s import match as php_match\n\ndef test_m():\n    php_match(None)\n",
])
def test_import_forms_count_as_own_module(tmp_path, text):
    root = write_tree(tmp_path / "r", {"tests/test_x.py": text})
    assert examples._file_imports_own(str(root), "tests/test_x.py", "match", OWN)


def test_relative_and_foreign_imports(tmp_path):
    root = write_tree(tmp_path / "r", {
        "pkg_tf/rules/test_inline.py": "from .php_s import match\n",
        "pkg_tf/rules/tests/test_up.py": "from ..php_s import match\n",
        "tests/test_sed.py": "from pkg_tf.rules.sed import match\nfrom pkg_tf.rules import php_s\n",
        "tests/test_src.py": "from rules.php_s import match\n",
    })
    assert examples._file_imports_own(str(root), "pkg_tf/rules/test_inline.py", "match", OWN)
    assert examples._file_imports_own(str(root), "pkg_tf/rules/tests/test_up.py", "match", OWN)
    assert not examples._file_imports_own(str(root), "tests/test_sed.py", "match", OWN)  # php_s imported, never used
    assert examples._file_imports_own(str(root), "tests/test_src.py", "match", "src/rules/php_s.py")


def test_common_name_edit_detected_through_own_test(tmp_path, make_cfg):
    cfg, base, work, targets = common_name_repo(tmp_path, make_cfg, with_index=True)
    r = next(r for r in diff.run(cfg, base, work, targets, harvest={}) if r.qualname == "match")
    assert r.status == diff.DIFFERENT, r.reason
    calls = [d["call"] for d in r.differences]
    assert "match(command=Command('php -t pub -s 0.0.0.0:8080', ''))" in calls
    assert r.differences[0]["source"] == "examples"


# ---- fixtures, conftest and str.format (thefuck 1) ----

PIP_OLD = '''
import re


def get_new_command(command):
    broken_cmd = re.findall(r'ERROR: unknown command "([^"]+)"', command.output)[0]
    new_cmd = re.findall(r'maybe you meant "([^"]+)"', command.output)[0]
    return command.script.replace(broken_cmd, new_cmd, 1)
'''
PIP_NEW = PIP_OLD.replace('ERROR: unknown command "([^"]+)"', 'ERROR: unknown command "([a-z]+)"')
PIP_CONFTEST = '''
import pytest


@pytest.fixture
def broken():
    return 'instatl'


@pytest.fixture
def suggested():
    """The fixed spelling."""
    word = 'inst'
    return word + 'all'
'''
PIP_TEST = '''
import pytest

from pkg_tf.rules.pip_unknown_command import get_new_command
from pkg_tf.types import Command


@pytest.fixture
def pip_unknown_cmd(broken, suggested):
    return 'ERROR: unknown command "{}" - maybe you meant "{}"'.format(broken, suggested)


@pytest.fixture
def pip_unknown_cmd_without_recommend():
    yield f'ERROR: unknown command "{"i"}"'


@pytest.mark.parametrize('script, broken, suggested, new_cmd', [
    ('pip un+install thefuck', 'un+install', 'uninstall', 'pip uninstall thefuck'),
    ('pip instatl', 'instatl', 'install', 'pip install')])
def test_get_new_command(script, new_cmd, pip_unknown_cmd):
    assert get_new_command(Command(script, pip_unknown_cmd)) == new_cmd


def test_plain(pip_unknown_cmd, pip_unknown_cmd_without_recommend):
    get_new_command(Command('pip instatl', pip_unknown_cmd))
    get_new_command(Command('pip i', pip_unknown_cmd_without_recommend))
'''
PIP_FILES = {"pkg_tf/__init__.py": "", "pkg_tf/rules/__init__.py": "", "pkg_tf/types.py": TYPES,
             "tests/conftest.py": "", "tests/rules/conftest.py": PIP_CONFTEST,
             "tests/rules/test_pip_unknown_command.py": PIP_TEST}


def test_fixtures_from_conftest_and_format_resolve(tmp_path):
    root = write_tree(tmp_path / "r", PIP_FILES)
    scan = examples._scan_file(str(root), "tests/rules/test_pip_unknown_command.py", {"get_new_command"})
    shown = [repr(c[0][2]) for c in examples._file_calls(scan, "get_new_command", ["command"])]
    # parametrize overrides the conftest fixtures that pip_unknown_cmd depends on
    assert """Command('pip un+install thefuck', 'ERROR: unknown command "un+install" - maybe you meant "uninstall"')""" in shown
    assert """Command('pip instatl', 'ERROR: unknown command "instatl" - maybe you meant "install"')""" in shown
    assert """Command('pip i', 'ERROR: unknown command "i"')""" in shown  # yield fixture, f-string
    assert len(shown) == 4


def test_same_file_fixture_overrides_conftest(tmp_path):
    root = write_tree(tmp_path / "r", {
        "tests/conftest.py": "import pytest\n\n@pytest.fixture\ndef word():\n    return 'outer'\n",
        "tests/sub/conftest.py": "import pytest\n\n@pytest.fixture\ndef word():\n    return 'inner'\n",
        "tests/sub/test_a.py": "def test_a(word):\n    f(word)\n",
        "tests/sub/test_b.py": "import pytest\n\n@pytest.fixture\ndef word():\n    return 'own'\n\ndef test_b(word):\n    f(word)\n",
        "tests/test_c.py": "import pytest\n\n@pytest.fixture(name='word')\ndef _w():\n    return 'named'\n\ndef test_c(word):\n    f(word)\n",
    })
    def values(rel):
        scan = examples._scan_file(str(root), rel, {"f"})
        return [c[0][2] for c in examples._file_calls(scan, "f", ["x"])]
    assert values("tests/sub/test_a.py") == ["inner"]
    assert values("tests/sub/test_b.py") == ["own"]
    assert values("tests/test_c.py") == ["named"]


def test_format_and_fstring_resolution_is_bounded(tmp_path):
    root = write_tree(tmp_path / "r", {"tests/test_f.py": '''
        def test_f():
            a, n = 'x', 3
            f('{}-{}'.format(a, n))
            f('{k}:{0}'.format(a, k='y'))
            f(f'{a}={n:03d}!{a!r}')
            f('{:>99999999}'.format(a))
            f('{0.__class__}'.format(a))
            f(f'{a:>{n}}')
            f('{}'.format(unknown))
    '''})
    scan = examples._scan_file(str(root), "tests/test_f.py", {"f"})
    vals = [c[0][2] for c in examples._file_calls(scan, "f", ["x"])]
    assert vals == ["x-3", "y:x", "x=003!'x'", "  x"]


def test_thefuck_1_format_fixture_edit_detected(tmp_path, make_cfg):
    rel = "pkg_tf/rules/pip_unknown_command.py"
    base = write_tree(tmp_path / "base", {**PIP_FILES, rel: PIP_OLD})
    work = write_tree(tmp_path / "work", {**PIP_FILES, rel: PIP_NEW})
    cfg = make_cfg(work)
    examples._TEST_FILES.clear()
    targets = diff.changed_targets(rel, (base / rel).read_text(), (work / rel).read_text())
    r = next(r for r in diff.run(cfg, str(base), str(work), targets, harvest={}) if r.qualname == "get_new_command")
    assert r.status == diff.DIFFERENT, r.reason
    assert any("pip un+install thefuck" in d["call"] for d in r.differences)


# ---- review fixes: bounded fixture evaluation, no SyntaxWarnings, shared module names ----

def test_fixture_fan_out_is_evaluated_once_per_row(tmp_path):
    import time
    k, levels = 5, 8
    lines = ["import pytest"]
    for lvl in range(levels):
        for i in range(k):
            deps = ", ".join(f"f{lvl + 1}_{j}" for j in range(k)) if lvl < levels - 1 else ""
            lines += ["@pytest.fixture", f"def f{lvl}_{i}({deps}):", "    return 'x'"]
    args = ", ".join(f"f0_{i}" for i in range(k))
    rows = ", ".join(f"({i},)" for i in range(10))
    test = (f"import pytest\n@pytest.mark.parametrize('p', [{rows}])\ndef test_a(p, {args}):\n    g(p, f0_0)\n"
            + "".join(f"def test_{i}({args}):\n    g(1, f0_1)\n" for i in range(20)))
    root = write_tree(tmp_path / "fan", {"tests/conftest.py": "\n".join(lines) + "\n", "tests/test_x.py": test})
    start = time.perf_counter()
    scan = examples._scan_file(str(root), "tests/test_x.py", {"g"})
    assert time.perf_counter() - start < 1.0
    calls = examples._file_calls(scan, "g", ["a", "b"])
    assert len(calls) == 30 and all(c[1][2] == "x" for c in calls)
    # Each fixture is evaluated once for the empty row and once per parametrize value. The deepest level (stack depth 7)
    # is past the depth limit, so 35 of the 40 fixtures are evaluated.
    assert scan._fixture_evals == 35 * 11


def test_fixture_evaluations_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(examples, "MAX_FIXTURE_EVALS", 3)
    root = write_tree(tmp_path / "cap", {"tests/test_x.py": '''
        import pytest

        @pytest.fixture
        def a():
            return 'a'

        @pytest.fixture
        def b(a):
            return a + 'b'

        @pytest.fixture
        def c(b):
            return b + 'c'

        @pytest.fixture
        def d(c):
            return c + 'd'

        def test_x(d):
            g(d)
    '''})
    scan = examples._scan_file(str(root), "tests/test_x.py", {"g"})
    assert scan._fixture_evals == 3 and examples._file_calls(scan, "g", ["v"]) == []


def test_scan_respects_deadline(tmp_path):
    import time
    root = write_tree(tmp_path / "dl", {"tests/test_x.py": "def test_a():\n    g(1)\n"})
    assert examples._file_calls(examples._scan_file(str(root), "tests/test_x.py", {"g"}), "g", ["v"]) == [[("v", "pos", 1)]]
    late = examples._scan_file(str(root), "tests/test_x.py", {"g"}, time.monotonic() - 1)
    assert examples._file_calls(late, "g", ["v"]) == []


def test_old_code_parses_without_syntax_warnings(tmp_path):
    import warnings
    text = "import re\nfrom pkg_tf.rules.php_s import match\n\ndef test_m():\n    match(re.compile('\\.x'))\n"
    root = write_tree(tmp_path / "w", {"tests/test_re.py": text})
    assert "'\\.x'" in (root / "tests/test_re.py").read_text()  # an invalid escape in the source
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert examples._file_imports_own(str(root), "tests/test_re.py", "match", OWN)
        assert examples._scan_file(str(root), "tests/test_re.py", {"match"}) is not None


def test_modules_sharing_a_last_name_part_are_told_apart(tmp_path):
    root = write_tree(tmp_path / "r", {
        "tests/test_a.py": "from pkg_a.rules import match\n",
        "tests/test_b.py": "from pkg_b.rules import match\n",
        "tests/test_b2.py": "import pkg_b.rules\n\ndef test_m():\n    pkg_b.rules.match(1)\n",
    })
    assert examples._file_imports_own(str(root), "tests/test_a.py", "match", "pkg_a/rules.py")
    assert not examples._file_imports_own(str(root), "tests/test_b.py", "match", "pkg_a/rules.py")
    assert not examples._file_imports_own(str(root), "tests/test_b2.py", "match", "pkg_a/rules.py")
    assert examples._file_imports_own(str(root), "tests/test_b2.py", "match", "pkg_b/rules.py")
