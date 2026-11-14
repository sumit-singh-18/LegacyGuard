"""The validation harness (tools/validate.py, roadmap 3b) on tiny local git repos: no network."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from conftest import TARGET_PY, write_tree

TOOL = Path(__file__).resolve().parent.parent / "tools" / "validate.py"
spec = importlib.util.spec_from_file_location("lg_validate", TOOL)
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)

BUGGY = '''
def parse_range(text: str) -> list:
    """>>> parse_range('2-4')"""
    lo, hi = text.split("-")
    return list(range(int(lo), int(hi)))
'''
FIXED = BUGGY.replace("int(hi))", "int(hi) + 1)")
REFACTOR = FIXED.replace("    return list(range(int(lo), int(hi) + 1))",
                         "    first, last = int(lo), int(hi)\n    return [n for n in range(first, last + 1)]")
CHANGED = REFACTOR.replace('text.split("-")', 'text.split(":")').replace("'2-4'", "'2:4'")
OTHER_V1 = '''
def scale(n: int) -> int:
    """>>> scale(3)"""
    return n * 2
'''
OTHER_V2 = OTHER_V1.replace("n * 2", "n * 3")
LEGACY = '''
def old_helper(n: int) -> int:
    return n
'''
DEP = '''
import lg_missing_dependency_xyz


def total(items: list) -> int:
    """>>> total([1, 2, 3])"""
    return sum(items)
'''


def git(root, *args) -> str:
    return subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def commit(root, files: dict, msg: str, remove=()) -> str:
    for rel, text in files.items():
        (root / rel).write_text(text.lstrip("\n"))
    for rel in remove:
        (root / rel).unlink()
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg)
    return git(root, "rev-parse", "HEAD")


def make_repo(tmp_path) -> dict:
    """buggy -> unrelated change in another module (and a helper deleted) -> fix -> refactor -> behavior change."""
    root = write_tree(tmp_path / "src-repo", {"rng/__init__.py": "", ".gitignore": ".legacyguard/\n"})
    git(root, "init", "-q")
    return {
        "buggy": commit(root, {"rng/core.py": BUGGY, "rng/other.py": OTHER_V1, "rng/legacy.py": LEGACY}, "buggy"),
        "unrelated": commit(root, {"rng/other.py": OTHER_V2}, "unrelated change", remove=["rng/legacy.py"]),
        "fixed": commit(root, {"rng/core.py": FIXED}, "fix off by one"),
        "refactor": commit(root, {"rng/core.py": REFACTOR}, "refactor"),
        "changed": commit(root, {"rng/core.py": CHANGED}, "new separator"),
    }


def make_dep_repo(tmp_path) -> dict:
    """A module that imports a dependency nobody installed: nothing can be compared."""
    root = write_tree(tmp_path / "dep-repo", {"dep/__init__.py": "", ".gitignore": ".legacyguard/\n"})
    git(root, "init", "-q")
    return {"buggy": commit(root, {"dep/core.py": DEP.replace("sum(items)", "sum(items) - 1")}, "buggy"),
            "fixed": commit(root, {"dep/core.py": DEP}, "fix")}


def test_harness_on_local_fixtures(tmp_path, capsys):
    c, d = make_repo(tmp_path), make_dep_repo(tmp_path)
    other_only = tmp_path / "other-only.patch"  # a fix patch that misses the bug's file
    other_only.write_text(git(tmp_path / "src-repo", "diff", c["buggy"], c["fixed"], "--", "rng/other.py") + "\n")
    py = {"python": TARGET_PY}
    cases = [
        {"name": "bug-off-by-one", "repo": "src-repo", "kind": "bug", "fixed": c["fixed"], "buggy": c["buggy"], "files": ["rng/core.py"], **py},
        {"name": "bug-no-files", "repo": "src-repo", "kind": "bug", "fixed": c["fixed"], "buggy": c["buggy"], **py},
        {"name": "bug-credit-only-bug-files", "repo": "src-repo", "kind": "bug", "fixed": c["fixed"], "buggy": c["buggy"],
         "files": ["rng/core.py"], "patch": "other-only.patch", **py},
        {"name": "refactor", "repo": "src-repo", "kind": "change", "base": c["fixed"], "head": c["refactor"], "expect": "quiet", **py},
        {"name": "separator", "repo": "src-repo", "kind": "change", "base": c["refactor"], "head": c["changed"], "expect": "detected", **py},
        {"name": "bad-commit", "repo": "src-repo", "kind": "change", "base": "deadbeef" * 5, "head": c["changed"], "expect": "quiet", **py},
        {"name": "bad-python", "repo": "src-repo", "kind": "change", "base": c["fixed"], "head": c["refactor"], "expect": "quiet",
         "python": str(tmp_path / "no-such-python")},
        {"name": "separator-mislabeled", "repo": "src-repo", "kind": "change", "base": c["refactor"], "head": c["changed"], "expect": "quiet", **py},
        {"name": "missing-dependency", "repo": "dep-repo", "kind": "bug", "fixed": d["fixed"], "buggy": d["buggy"], **py},
    ]
    manifest = tmp_path / "manifest.json"  # relative repo and patch paths resolve against the manifest's folder
    manifest.write_text(json.dumps({"cases": cases}))
    out, work = tmp_path / "out", tmp_path / "work"

    assert validate.main([str(manifest), "--workdir", str(work), "--out", str(out), "--fix-tests", "with"]) == 0
    printed = capsys.readouterr().out

    data = json.loads((out / "results.json").read_text())
    by = {r["name"]: r for r in data["cases"]}
    assert len(list((work / "repos").iterdir())) == 2  # one clone per repo, reused by its cases
    version = subprocess.run([TARGET_PY, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True).stdout.strip()

    bug = by["bug-off-by-one"]  # only the bug's file is planted; the unrelated module stays as fixed
    assert bug["verdict"] == "detected" and bug["match"] and bug["files"] == ["rng/core.py"] and not bug["warnings"]
    assert bug["changed_files"] == ["rng/core.py"] and {f["file"] for f in bug["functions"]} == {"rng/core.py"}
    assert bug["python_version"] == version
    ce = bug["different"][0]["counterexample"]
    assert bug["different"][0]["qualname"] == "parse_range" and "'2-4'" in ce["call"]
    assert "[2, 3, 4]" in str(ce["before"]) and "[2, 3]" in str(ce["after"])  # baseline is the fix

    wide = by["bug-no-files"]  # every differing source file planted, with a warning; a buggy-only file is still seen
    assert wide["verdict"] == "detected" and sorted(wide["files"]) == ["rng/core.py", "rng/legacy.py", "rng/other.py"]
    assert any("no files given and 3 source files differ" in w for w in wide["warnings"])
    assert "rng/legacy.py" in wide["changed_files"]

    narrow = by["bug-credit-only-bug-files"]  # a difference outside the bug's files is recorded but not credited
    assert narrow["verdict"] == "unchecked" and not narrow["match"] and not narrow["different"]
    assert [f["qualname"] for f in narrow["other_different"]] == ["scale"]
    assert any("outside the bug's files" in w for w in narrow["warnings"])

    ref = by["refactor"]
    assert ref["verdict"] == "quiet" and ref["match"]
    assert [f["qualname"] for f in ref["verified"]] == ["parse_range"] and ref["mutants_total"] > 0
    assert ref["mutation_score"] is not None and ref["seconds"] > 0

    assert by["separator"]["verdict"] == "detected" and by["separator"]["match"]
    bad = by["bad-commit"]
    assert bad["verdict"] == "error" and not bad["match"] and "deadbeef" in bad["error"]
    assert by["bad-python"]["verdict"] == "error" and "cannot run" in by["bad-python"]["error"]
    assert by["separator-mislabeled"]["verdict"] == "detected" and not by["separator-mislabeled"]["match"]

    dep = by["missing-dependency"]
    assert dep["verdict"] == "unchecked" and not dep["match"]
    assert dep["untested"] and all(f["inputs_compared"] == 0 for f in dep["functions"])

    m = data["metrics"]
    assert (m["cases"], m["ran"], m["checked"], m["unchecked"], m["errors"], m["matched"]) == (9, 7, 5, 2, 2, 4)
    assert m["unchecked_cases"] == ["bug-credit-only-bug-files", "missing-dependency"]
    assert m["error_cases"] == ["bad-commit", "bad-python"]
    assert m["detection"] == {"detected": 3, "total": 3, "rate": 1.0}  # the unchecked bug cases are not misses
    assert m["false_alarm"] == {"alarms": 1, "total": 2, "rate": 0.5}
    assert m["untested"]["total"] == sum(len(r["functions"]) for r in data["cases"])
    assert m["untested"]["share"] == round(m["untested"]["functions"] / m["untested"]["total"], 3)
    times = [r["seconds"] for r in data["cases"] if r["verdict"] != "error"]
    assert m["seconds"]["median"] == round(sorted(times)[len(times) // 2], 3) and m["seconds"]["p95"] == max(times)

    md = (out / "results.md").read_text()
    assert "Detection rate: 100.0% (3 of 3" in md and "False-alarm rate: 50.0% (1 of 2" in md
    assert "Unchecked (nothing compared, left out of the rates): bug-credit-only-bug-files, missing-dependency" in md
    assert "| bad-commit | change | - | quiet | error | no |" in md and "Error: RuntimeError" in md
    assert data["fix_tests"] == "with" and list(data["metrics_by_mode"]) == ["with"]
    assert bug["fix_tests"] == "with" and bug["synthetic_base"] is None and by["refactor"]["fix_tests"] is None
    assert bug["deciding_input"]["source"] == "examples" and "'2-4'" in bug["deciding_input"]["call"]  # the docstring
    # parse_range: generated inputs differ only by repeating the docstring's '2-4'.
    assert bug["deciding_input"]["independent_sources"] == ["examples"]
    # scale (n * 2 -> n * 3): the docstring's scale(3) and generated small ints both differ. The counterexample
    # shown first is the example; every source is still recorded.
    sc = next(f for f in wide["different"] if f["qualname"] == "scale")
    assert sc["counterexample"]["source"] == "examples" and sc["independent_sources"] == ["examples", "generated"]
    assert any(d["source"] == "generated" and not d["repeats_example"] for d in sc["differing_inputs"])
    assert wide["deciding_input"]["sources"] == ["examples", "generated"]
    assert wide["deciding_input"]["independent_sources"] == ["examples", "generated"]
    ds = data["metrics"]["deciding_sources"]  # true detections only: separator-mislabeled is a false alarm
    assert ds["first_shown"] == {"examples": 3} and ds["cases_by_source"] == {"examples": 3, "generated": 3}
    assert (ds["tests_or_examples_only"], ds["also_other_sources"]) == (1, 2)  # bug-off-by-one; bug-no-files, separator
    assert "Warning: no files given and 3 source files differ" in md
    assert "Different outside the bug's files (not credited): `rng/other.py::scale`" in md
    assert "not a proof" in md
    assert "Detection rate: 100.0%" in printed and "Unchecked (nothing compared" in printed and "warning:" in printed


# lower() and casefold() differ only on a few hundred special code points (German sharp s, ligatures, ...). The fix's
# code has no literal at all, so neither the fix's text nor splicing its literals into examples yields one; the only
# distinguishing input anywhere is the magic value in the fix's regression test.
FIX_LITERAL = "Straße"
WS_BUGGY = '''
def norm_key(text):
    return text.lower()
'''
WS_FIXED = WS_BUGGY.replace("text.lower()", "text.casefold()")
WS_TEST_V1 = '''
from txt.core import norm_key


def test_plain():
    assert norm_key("Hello") == "hello"
'''
WS_TEST_V2 = WS_TEST_V1 + f'''

def test_sharp_s():
    assert norm_key("{FIX_LITERAL}") == "strasse"
'''
WS_TEST_NEW = '''
from txt.core import norm_key


def test_ascii():
    assert norm_key("ABC") == "abc"
'''


def make_fix_test_repo(tmp_path) -> dict:
    """buggy -> fix that also adds a regression test (an edited test file, a new test file and changed test data)."""
    root = write_tree(tmp_path / "ws-repo", {"txt/__init__.py": "", ".gitignore": ".legacyguard/\n"})
    (root / "tests" / "data").mkdir(parents=True)
    git(root, "init", "-q")
    return {
        "buggy": commit(root, {"txt/core.py": WS_BUGGY, "tests/test_core.py": WS_TEST_V1, "tests/data/sample.txt": "old\n"}, "buggy"),
        "fixed": commit(root, {"txt/core.py": WS_FIXED, "tests/test_core.py": WS_TEST_V2, "tests/test_extra.py": WS_TEST_NEW,
                               "tests/data/sample.txt": "new\n"}, "fix inner spaces, with a regression test"),
    }


def test_fix_tests_modes(tmp_path, capsys):
    c = make_fix_test_repo(tmp_path)
    case = {"name": "ws", "repo": "ws-repo", "kind": "bug", "fixed": c["fixed"], "buggy": c["buggy"], "files": ["txt/core.py"],
            "python": TARGET_PY}
    change = {"name": "ws-change", "repo": "ws-repo", "kind": "change", "base": c["buggy"], "head": c["fixed"],
              "expect": "detected", "python": TARGET_PY}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": [case, change]}))
    out, work = tmp_path / "out", tmp_path / "work"

    assert validate.main([str(manifest), "--workdir", str(work), "--out", str(out)]) == 0  # default: both
    printed = capsys.readouterr().out
    data = json.loads((out / "results.json").read_text())
    recs = {(r["name"], r["fix_tests"]): r for r in data["cases"]}
    assert set(recs) == {("ws", "without"), ("ws", "with"), ("ws-change", None)}  # a change case runs once

    with_ = recs[("ws", "with")]
    assert with_["verdict"] == "detected" and with_["synthetic_base"] is None and with_["fix_tests_restored"] == []
    di = with_["deciding_input"]
    # ci.run does not run tests, so the fix test's literal arrives as an example call found in the test file.
    assert di["function"] == "txt/core.py::norm_key" and di["source"] == "examples"
    assert di["call"] == f"norm_key(text='{FIX_LITERAL}')" and di["args"][0]["name"] == "text"
    assert with_["different"][0]["counterexample"]["source"] == di["source"]
    # The grid reuses the example's value, so "generated" differs too, but only by repeating the fix test's call.
    assert di["sources"] == ["examples", "generated"] and di["independent_sources"] == ["examples"]
    assert all(FIX_LITERAL in d["call"] for d in with_["different"][0]["differing_inputs"])

    without = recs[("ws", "without")]
    # Structural: without the fix's test, its literal never reaches the guard. Whatever else is detected (say, a
    # generated string that happens to hold a special code point) must come from an input that is not that literal.
    assert without["verdict"] in ("quiet", "unchecked", "detected")
    differing = [d for f in without["functions"] for d in f.get("differing_inputs", [])]
    assert not any(FIX_LITERAL in (d["call"] or "") for d in differing)
    if without["verdict"] == "detected":
        assert FIX_LITERAL not in without["deciding_input"]["call"]
    else:
        assert not without["different"] and without["deciding_input"] is None
    assert without["fix_tests_restored"] == ["tests/data/sample.txt", "tests/test_core.py", "tests/test_extra.py"]
    assert without["contract_breaks"] == 0 and without["changed_files"] == ["txt/core.py"]  # no test-integrity noise
    assert without["files"] == ["txt/core.py"] and not without["warnings"]

    # The synthetic base: the fixed source with the buggy commit's tests; its parent is the fixed commit.
    clone = next((work / "repos").iterdir())
    synth = without["synthetic_base"]
    assert synth not in (c["fixed"], c["buggy"])
    assert git(clone, "rev-parse", f"{synth}^") == c["fixed"]
    assert git(clone, "diff", "--name-only", c["buggy"], synth, "--", "tests") == ""  # no fix-test changes at all
    assert git(clone, "diff", "--name-only", c["fixed"], synth, "--", "txt") == ""     # the fix itself is kept
    assert git(clone, "show", f"{synth}:tests/test_core.py").strip() == WS_TEST_V1.strip()
    assert synth not in git(clone, "for-each-ref", "--format=%(objectname)")  # no branch or tag points at it

    m = data["metrics_by_mode"]
    assert data["headline_mode"] == "without" and data["metrics"] == m["without"]
    assert m["with"]["detection"] == {"detected": 2, "total": 2, "rate": 1.0}  # the bug plus the change case
    if without["verdict"] == "quiet":  # the expected outcome today; the structural checks above hold either way
        assert m["without"]["detection"] == {"detected": 1, "total": 2, "rate": 0.5}  # only the change case
        assert "| Detection rate | 50.0% (1 of 2) | 100.0% (2 of 2) |" in (out / "results.md").read_text()
    ws_with = m["with"]["deciding_sources"]
    assert ws_with["cases_by_source"] == {"examples": 2, "generated": 2} and ws_with["first_shown"] == {"examples": 2}
    assert (ws_with["tests_or_examples_only"], ws_with["also_other_sources"], ws_with["other_sources_only"]) == (2, 0, 0)
    assert "Detection rate without fix tests (headline):" in printed and "; with fix tests: 100.0% (2 of 2)" in printed
    assert "[without fix tests]" in printed and "differing inputs from examples" in printed

    md = (out / "results.md").read_text()
    assert "| Metric | **without fix tests (headline)** | with fix tests |" in md
    assert (f"| ws | bug | with | detected | detected | yes | examples: `norm_key(text='{FIX_LITERAL}')` "
            "(all: examples, generated; independent: examples) |") in md
    assert "### Without fix tests (headline)" in md and "### With fix tests" in md
    assert "| ws | bug | without |" in md and "| ws | bug | with |" in md
    assert "test files put back to the buggy version: `tests/data/sample.txt`, `tests/test_core.py`, `tests/test_extra.py`" in md

    # One mode only.
    out2 = tmp_path / "out2"
    assert validate.main([str(manifest), "--workdir", str(work), "--out", str(out2), "--fix-tests", "without", "--only", "ws"]) == 0
    one = json.loads((out2 / "results.json").read_text())
    assert [r["fix_tests"] for r in one["cases"]] == ["without"] and list(one["metrics_by_mode"]) == ["without"]


def test_is_fix_test():
    assert validate.is_fix_test("tests/test_a.py", []) and validate.is_fix_test("test/data/x.json", [])
    assert validate.is_fix_test("pkg/conftest.py", []) and validate.is_fix_test("pkg/foo_test.py", [])
    assert not validate.is_fix_test("pkg/core.py", []) and not validate.is_fix_test("pkg/testing_utils.py", [])
    assert not validate.is_fix_test("tests/helpers.py", ["tests/helpers.py"])  # one of the bug's own files
    assert not validate.is_fix_test("numpy/testing/utils.py", [])  # real source under a "testing" package
    assert validate.is_fix_test("numpy/testing/data/a.npy", []) and validate.is_fix_test("pkg/fixtures/x.json", [])
    assert validate.is_fix_test("pkg/testdata/in.txt", []) and not validate.is_fix_test("pkg/fixtures/factory.py", [])
    assert validate.modes_for("both") == ["without", "with"] and validate.modes_for("with") == ["with"]


def test_all_differing_sources_are_recorded():
    """diff sorts differences tests, then examples first; the record must not keep only that first one."""
    from types import SimpleNamespace
    diffs = [{"call": "f(3)", "before": "6", "after": "9", "source": "examples", "args": [{"name": "n", "value": 3}]},
             {"call": "f(1)", "before": "2", "after": "3", "source": "generated", "args": [{"name": "n", "value": 1}]},
             {"call": "f(-1)", "before": "-2", "after": "-3", "source": "generated", "args": []}]
    fn = lambda ds: validate._function(SimpleNamespace(  # noqa: E731
        file="a.py", qualname="f", status="different", reason="", inputs_compared=len(ds), mutants_killed=0,
        mutants_total=0, via=None, via_file=None, differences=ds))
    f = fn(diffs)
    assert f["difference_sources"] == ["examples", "generated"] and len(f["differing_inputs"]) == 3
    assert f["independent_sources"] == ["examples", "generated"]  # f(1) and f(-1) are new values
    di = validate.deciding_input([f])
    assert di["source"] == "examples" and di["call"] == "f(3)" and di["sources"] == ["examples", "generated"]
    # A generated input that only repeats the example's call (the grid reuses example values) is not independent.
    echo = fn([diffs[0], {**diffs[1], "call": "f(3)"}])
    assert echo["difference_sources"] == ["examples", "generated"] and echo["independent_sources"] == ["examples"]
    assert [d["repeats_example"] for d in echo["differing_inputs"]] == [False, True]
    only_gen = fn(diffs[1:])
    assert only_gen["independent_sources"] == ["generated"]
    recs = [{"verdict": "detected", "deciding_input": validate.deciding_input(fs)} for fs in ([f], [echo], [only_gen])]
    ds = validate.deciding_sources(recs + [{"verdict": "quiet", "deciding_input": None}])
    assert ds["cases_by_source"] == {"examples": 2, "generated": 3}
    assert (ds["tests_or_examples_only"], ds["also_other_sources"], ds["other_sources_only"]) == (1, 1, 1)
    assert ds["first_shown"] == {"examples": 2, "generated": 1}  # first-shown alone hides generated in case 1
    assert "generated 3" in validate._sources_text(ds)


def test_classify_credits_only_the_bug_files():
    fn = lambda file, status, via_file=None: {"file": file, "qualname": "f", "status": status,  # noqa: E731
                                              **({"via": "calls g", "via_file": via_file} if via_file else {})}
    got = validate.classify("bug", [fn("a.py", "verified"), fn("b.py", "different")], ["a.py"])
    assert got["verdict"] == "quiet" and got["other_different"] == [fn("b.py", "different")]
    got = validate.classify("bug", [fn("c.py", "different", via_file="a.py")], ["a.py"])  # a caller of the bug's code
    assert got["verdict"] == "detected"
    assert validate.classify("bug", [fn("a.py", "untested")], ["a.py"])["verdict"] == "unchecked"
    assert validate.classify("change", [], [])["verdict"] == "unchecked"
    assert validate.classify("change", [fn("b.py", "broken")], [])["verdict"] == "detected"


def test_preflight_rejects_old_or_broken_interpreters(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    py, version = validate.preflight(root, sys.executable)
    assert py == sys.executable and version.startswith(f"{sys.version_info[0]}.{sys.version_info[1]}.")
    old = tmp_path / "python2"
    old.write_text("#!/bin/sh\necho 2.7.18\n")
    old.chmod(0o755)
    assert validate.preflight(root, str(old))[1] == "2.7.18"  # run_case turns < 3.7 into an error
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    try:
        validate.preflight(root, str(broken))
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as exc:
        assert "cannot run" in str(exc)


def test_metrics_math():
    rec = lambda name, kind, expect, verdict, secs, statuses=(): {  # noqa: E731
        "name": name, "kind": kind, "expect": expect, "verdict": verdict, "match": verdict == expect, "seconds": secs,
        "functions": [{"status": s} for s in statuses], "mutants_killed": 1, "mutants_total": 2}
    records = [rec("a", "bug", "detected", "quiet", 1.0, ["untested", "verified"]),
               rec("b", "bug", "detected", "detected", 2.0, ["different"]),
               rec("c", "change", "quiet", "quiet", 3.0, ["verified"]),
               rec("d", "bug", "detected", "unchecked", 4.0, ["untested"]),
               rec("e", "change", "detected", "error", 99.0)]
    m = validate.metrics(records)
    assert m["detection"] == {"detected": 1, "total": 2, "rate": 0.5} and m["false_alarm"]["rate"] == 0.0
    assert (m["errors"], m["unchecked"], m["checked"]) == (1, 1, 3) and m["unchecked_cases"] == ["d"]
    assert m["untested"] == {"functions": 2, "total": 5, "share": 0.4}
    assert m["seconds"] == {"median": 2.5, "p95": 4.0} and m["mutation"]["score"] == 0.5
    assert validate._p95([float(i) for i in range(1, 21)]) == 19.0
