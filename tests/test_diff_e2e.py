"""differ.run end to end: a baseline copy and a working copy of a small project, three real worker processes."""
import shutil

from runner import diff

from conftest import write_tree

STRUTILS = '''
def parse_int_list(range_string, delim=",", range_delim="-"):
    """Returns a sorted list of positive integers based on *range_string*.

    >>> parse_int_list('1,3,5-8,10-11,15')
    [1, 3, 5, 6, 7, 8, 10, 11, 15]
    """
    output = []
    for x in range_string.strip().split(delim):
        if range_delim in x:
            range_limits = list(map(int, x.split(range_delim)))
            output += list(range(min(range_limits), max(range_limits)+1))
        elif not x:
            continue
        else:
            output.append(int(x))
    return sorted(output)


def dedupe_in_place(items: list[int]) -> int:
    seen = set()
    out = [x for x in items if not (x in seen or seen.add(x))]
    items[:] = out
    return len(out)


def roll(sides: int) -> int:
    import random
    return random.randint(1, sides)


def shout(text: str) -> str:
    return text.upper()
'''


def setup(tmp_path, make_cfg, old_src, new_src):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/strutils.py": new_src})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/strutils.py": old_src})
    cfg = make_cfg(root)
    # Diff the files as written, so line numbers match what the worker traces.
    targets = diff.changed_targets("pkg/strutils.py", (base / "pkg/strutils.py").read_text(), (root / "pkg/strutils.py").read_text())
    return cfg, str(base), str(root), targets


def by_name(results):
    return {r.qualname: r for r in results}


def test_readme_example_is_caught(tmp_path, make_cfg):
    new = STRUTILS.replace("max(range_limits)+1", "max(range_limits)")
    cfg, base, work, targets = setup(tmp_path, make_cfg, STRUTILS, new)
    r = by_name(diff.run(cfg, base, work, targets, harvest={}))["parse_int_list"]
    assert r.status == diff.DIFFERENT
    assert any("range_string=" in d["call"] for d in r.differences)
    assert "LegacyGuard behavior difference [LG-1]" in diff.format_wake(r, "LG-1")


def test_refactor_is_verified(tmp_path, make_cfg):
    new = STRUTILS.replace("return text.upper()", "out = text.upper()\n    return out")
    cfg, base, work, targets = setup(tmp_path, make_cfg, STRUTILS, new)
    r = by_name(diff.run(cfg, base, work, targets, harvest={}))["shout"]
    assert r.status == diff.DONE, r.reason


def test_mutation_only_change_is_caught(tmp_path, make_cfg):
    # Same return value, but the list passed in is no longer deduplicated: only the state comparison sees it.
    new = STRUTILS.replace("    items[:] = out\n", "")
    cfg, base, work, targets = setup(tmp_path, make_cfg, STRUTILS, new)
    r = by_name(diff.run(cfg, base, work, targets, harvest={}))["dedupe_in_place"]
    assert r.status == diff.DIFFERENT
    first = r.differences[0]
    assert "then items=" in first["before"] and "then items=" in first["after"]


def test_random_function_is_checked_not_dropped(tmp_path, make_cfg):
    new = STRUTILS.replace("return random.randint(1, sides)", "return random.randint(1, sides) + 0")
    cfg, base, work, targets = setup(tmp_path, make_cfg, STRUTILS, new)
    r = by_name(diff.run(cfg, base, work, targets, harvest={}))["roll"]
    assert r.status == diff.DONE, r.reason
    broken = STRUTILS.replace("return random.randint(1, sides)", "return random.randint(0, sides)")
    cfg, base, work, targets = setup(tmp_path / "b", make_cfg, STRUTILS, broken)
    assert by_name(diff.run(cfg, base, work, targets, harvest={}))["roll"].status == diff.DIFFERENT


def test_without_determinism_random_is_never_verified(tmp_path, make_cfg):
    new = STRUTILS.replace("return random.randint(1, sides)", "return random.randint(1, sides) + 0")
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/strutils.py": new})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/strutils.py": STRUTILS})
    cfg = make_cfg(root, behavioral_diff={"deterministic": False})
    targets = diff.changed_targets("pkg/strutils.py", (base / "pkg/strutils.py").read_text(), (root / "pkg/strutils.py").read_text())
    r = by_name(diff.run(cfg, str(base), str(root), targets, harvest={}))["roll"]
    # The old approach: inputs where OLD happens to agree with itself survive the noise filter, and NEW then
    # disagrees by chance. A no-op edit is never verified, and is often reported as a false difference.
    assert r.status in (diff.UNTESTED, diff.DIFFERENT)
