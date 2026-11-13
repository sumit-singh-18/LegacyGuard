"""declare_change: intended changes with checked examples (roadmap D1)."""
import pytest

from guardlib import engine, intent, ledger
from runner import diff

from conftest import write_tree

OLD = '''
def parse_range(text: str) -> list:
    """>>> parse_range('2-4')"""
    lo, hi = map(int, text.split("-"))
    return list(range(lo, hi + 1))


def total_span(text: str) -> int:
    return len(parse_range(text))
'''
EXCLUSIVE = OLD.replace("range(lo, hi + 1)", "range(lo, hi)")


def project(tmp_path, make_cfg, new_src):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/r.py": new_src})
    cfg = make_cfg(root, behavioral_diff={"mutants": 0})
    write_tree(cfg.state_dir / "baseline", {"pkg/__init__.py": "", "pkg/r.py": OLD})
    return cfg


def check(cfg):
    """What post_edit does: targets, diff, record. Returns the findings that would wake Claude."""
    with ledger.locked(cfg.state_dir) as data:
        targets = engine.targets_for(cfg, "pkg/r.py", data)
    results = diff.run(cfg, str(cfg.state_dir / "baseline"), cfg.root, targets, harvest={})
    with ledger.locked(cfg.state_dir) as data:
        return engine.record(cfg, data, results), results


def test_parse_example():
    item = intent.parse_example("parse_range", ["text"], {"call": "parse_range('5-8')", "returns": "[5, 6, 7]"})
    assert item["expect"] == {"k": "return", "v": {"t": "list", "v": [{"t": "v", "v": 5}, {"t": "v", "v": 6}, {"t": "v", "v": 7}]}}
    assert intent.parse_example("f", ["x"], {"call": "f(-1)", "raises": "ValueError"})["expect"] == {"k": "raise", "type": "ValueError"}
    for bad in ({"call": "g(1)", "returns": "1"}, {"call": "f(x)", "returns": "1"}, {"call": "f(1)"}):
        with pytest.raises(intent.IntentError):
            intent.parse_example("f", ["x"], bad)


def test_declared_change_stops_the_wake(tmp_path, make_cfg):
    cfg = project(tmp_path, make_cfg, EXCLUSIVE)
    wake, _ = check(cfg)
    assert any(r.qualname == "parse_range" for _, r in wake)  # before declaring: a difference wakes Claude
    text = intent.declare(cfg, "pkg/r.py::parse_range", "the upper bound is now exclusive",
                          [{"call": "parse_range('5-8')", "returns": "[5, 6, 7]"}, {"call": "parse_range('1-2')", "returns": "[1]"}])
    assert "All 2 examples hold" in text and "1 open difference(s) marked intended" in text
    (tmp_path / "proj/pkg/r.py").write_text(EXCLUSIVE.replace("return list(range(lo, hi))", "items = list(range(lo, hi))\n    return items"))
    wake, results = check(cfg)
    assert wake == []  # the function and its caller differ as declared: recorded, no wake
    states = {f["item"]: f["state"] for f in ledger.load(cfg.state_dir)["findings"] if f["kind"] == "behavior_difference"}
    assert states["parse_range"] == "intended" and states.get("total_span") in ("intended", None)


def test_wrong_examples_mark_nothing(tmp_path, make_cfg):
    cfg = project(tmp_path, make_cfg, EXCLUSIVE)
    check(cfg)
    text = intent.declare(cfg, "pkg/r.py::parse_range", "exclusive", [{"call": "parse_range('5-8')", "returns": "[5, 6, 7, 8]"}])
    assert "FAIL parse_range('5-8'): expected returns [5, 6, 7, 8], got returns [5, 6, 7]" in text
    assert all(f["state"] != "intended" for f in ledger.load(cfg.state_dir)["findings"])


def test_breaking_a_declared_example_wakes(tmp_path, make_cfg):
    cfg = project(tmp_path, make_cfg, EXCLUSIVE)
    intent.declare(cfg, "pkg/r.py::parse_range", "exclusive", [{"call": "parse_range('5-8')", "returns": "[5, 6, 7]"}])
    (tmp_path / "proj/pkg/r.py").write_text(EXCLUSIVE.replace("range(lo, hi)", "range(lo + 1, hi)"))
    wake, _ = check(cfg)
    texts = [diff.format_wake(r, f["id"]) for f, r in wake if r.qualname == "parse_range"]
    assert texts and "declared change not holding" in texts[0] and "declared returns [5, 6, 7]" in texts[0]
