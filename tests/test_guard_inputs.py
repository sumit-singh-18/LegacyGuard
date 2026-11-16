"""Sprint 6 tasks 6.3 and 6.4: edit literals spliced into constructed objects' string arguments; inputs aimed at the
conditions that guard a changed statement; a pool of common date formats for date-like functions.

Each end-to-end case mirrors a miss from validation run 2 (docs/validation/2026-11-run2.md, misses 2-4), without the
fix's tests. OLD is the fixed code, NEW the planted bug, as in the harness."""
import json

from runner import diff, examples, inputs, lg_codec

from conftest import write_tree


def two_trees(tmp_path, make_cfg, files: dict, module: str, old: str, new: str):
    base = write_tree(tmp_path / "base", {**files, module: old})
    work = write_tree(tmp_path / "work", {**files, module: new})
    cfg = make_cfg(work)
    targets = diff.changed_targets(module, (base / module).read_text(), (work / module).read_text())
    return cfg, str(base), str(work), targets


def result_for(results, name):
    return next(r for r in results if r.qualname == name)


# ---- 6.3, thefuck 7 without fix tests: the edit's fragment spliced inside Command('php -s ...', '') ----

TF_TYPES = '''
class Command:
    def __init__(self, script, output):
        self.script = script
        self.output = output
'''
PHP_OLD = '''
def match(command):
    return "php -s" in command.script


def get_new_command(command):
    return command.script.replace("-s", "-S")
'''
PHP_NEW = PHP_OLD.replace('"php -s" in command.script', '" -s " in command.script')
# Only the pre-fix test: every script starts with 'php -s', which both versions accept.
PHP_TEST = '''
import pytest

from pkg_tf.rules.php_s import get_new_command, match
from pkg_tf.types import Command


@pytest.mark.parametrize('command', [
    Command('php -s localhost:8000', ''),
])
def test_match(command):
    assert match(command)
'''
TF_FILES = {"pkg_tf/__init__.py": "", "pkg_tf/rules/__init__.py": "", "pkg_tf/types.py": TF_TYPES,
            "tests/rules/test_php_s.py": PHP_TEST}


def test_construct_string_arguments_get_edit_splices():
    sym = {"qualname": "match", "kind": "function", "params": [{"name": "command", "annotation": None, "default": None}]}
    base = {"args": [{"name": "command", "kind": "pos",
                      "value": lg_codec.encode_input(lg_codec.Construct("pkg_tf.types:Command", ["php -s localhost:8000", ""]))}],
            "has_self": False, "source": "examples"}
    edit = examples.edit_info(PHP_OLD, PHP_NEW)
    items = inputs._edit_items(sym, [base], edit, None)
    built = [i for i in items if i["args"][0]["value"].get("t") == "construct"]
    assert built and all(i["source"] == "edit" and i["weak"] for i in built)
    scripts = [i["args"][0]["value"]["args"][0]["v"] for i in built]
    outputs = {i["args"][0]["value"]["args"][1]["v"] for i in built if i["args"][0]["value"]["args"][0]["v"] != "php -s localhost:8000"}
    assert all(i["args"][0]["value"]["cls"] == "pkg_tf.types:Command" for i in built)
    # One argument at a time: the script arguments vary while the output stays ''.
    assert "" in outputs
    assert any(" -s " in s and "php -s" not in s for s in scripts), scripts
    assert any(s.startswith(" -s ") for s in scripts)              # prefix
    assert any(s.startswith("php  -s  ") for s in scripts)          # after the first word
    assert any(s == "php  -s  localhost:8000" for s in scripts)      # replacing a word
    assert items == inputs._edit_items(sym, [base], edit, None)     # deterministic


def test_thefuck7_without_fix_tests_detected_by_spliced_construct(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, TF_FILES, "pkg_tf/rules/php_s.py", PHP_OLD, PHP_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "match")
    assert r.status == diff.DIFFERENT, r.reason
    spliced = [d for d in r.differences if d["source"] == "edit" and d["call"].startswith("match(command=Command(")]
    assert spliced, [d["call"] for d in r.differences]


# ---- 6.4, tqdm 4: the changed statement sits under `if unit_scale and unit_scale not in (True, 1)` ----

TQDM_OLD = '''
def format_meter(n, total, elapsed, unit='it', unit_scale=False):
    if unit_scale and unit_scale not in (True, 1):
        if total:
            total *= unit_scale
        n *= unit_scale
        unit_scale = False
    if total is None:
        return '{0}{1} [{2}s]'.format(n, unit, elapsed)
    return '{0}/{1}{2} [{3}s]'.format(n, total, unit, elapsed)
'''
TQDM_NEW = TQDM_OLD.replace("        if total:\n            total *= unit_scale\n", "        total *= unit_scale\n")
TQDM_TEST = '''
from pkg_tq.std import format_meter


def test_format_meter():
    assert format_meter(0, 1000, 13) == '0/1000it [13s]'
    assert format_meter(231, 1000, 392) == '231/1000it [392s]'
    assert format_meter(10, 100, 1, unit_scale=True) == '10/100it [1s]'
'''


def test_edit_info_collects_guarding_conditions():
    edit = examples.edit_info(TQDM_OLD, TQDM_NEW)
    guards = {g["name"]: g["consts"] for g in edit["guards"]}
    assert guards["unit_scale"] == [True, 1]
    assert "total" in guards  # the old version's `if total:` guards `total *= unit_scale`
    assert edit["dates"] is False


def test_guard_values_satisfy_and_violate_comparisons():
    values = inputs._guard_values([True, 1])
    for v in (True, False, 1, 2, 10, 0.5, 0, None, ""):
        assert any(type(v) is type(w) and v == w for w in values), (v, values)
    assert len(values) <= inputs.MAX_GUARD_VALUES


def test_tqdm4_without_fix_tests_detected_by_guard_condition_input(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg_tq/__init__.py": "", "tests/test_std.py": TQDM_TEST},
                                         "pkg_tq/std.py", TQDM_OLD, TQDM_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "format_meter")
    assert r.status == diff.DIFFERENT, r.reason
    guard = [d for d in r.differences if d["source"] == "edit" and "total=None" in d["call"]
             and "unit_scale=True" not in d["call"] and "unit_scale=False" not in d["call"]]
    assert guard, [d["call"] for d in r.differences]


# ---- 6.4, youtube-dl 5: the buggy return runs only when no strptime format matches ----

DATE_OLD = r'''
import calendar
import datetime
import email.utils
import re

DATE_FORMATS = ('%B %d %Y', '%b %d %Y', '%Y/%m/%d %H:%M:%S', '%d/%m/%Y %H:%M:%S', '%b %d %Y %H:%M', '%b %dth %Y %H:%M')


def unified_timestamp(date_str):
    if date_str is None:
        return None
    date_str = date_str.replace(',', ' ')
    pm_delta = 12 if re.search(r'(?i)PM', date_str) else 0
    date_str = re.sub(r'(?i)\s*(?:AM|PM)', '', date_str)
    date_str = re.sub(r'\s+', ' ', date_str).strip()
    for expression in DATE_FORMATS:
        try:
            dt = datetime.datetime.strptime(date_str, expression) + datetime.timedelta(hours=pm_delta)
            return calendar.timegm(dt.timetuple())
        except ValueError:
            pass
    timetuple = email.utils.parsedate_tz(date_str)
    if timetuple:
        return calendar.timegm(timetuple) + pm_delta * 3600
'''
DATE_NEW = DATE_OLD.replace("calendar.timegm(timetuple) + pm_delta", "calendar.timegm(timetuple.timetuple()) + pm_delta")
DATE_TEST = '''
from pkg_yt.utils import unified_timestamp


def test_unified_timestamp():
    assert unified_timestamp('December 21, 2010') == 1292889600
    assert unified_timestamp('2/2/2015 6:47:40 PM') == 1422902860
    assert unified_timestamp('Feb 14th 2016 5:45PM') == 1455471900
    assert unified_timestamp('2012/10/11 01:56:38') == 1349920598
'''


def test_date_pool_only_for_date_like_functions():
    assert examples.edit_info(DATE_OLD, DATE_NEW)["dates"] is True
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "s", "annotation": None, "default": None}]}
    base = {"args": [{"name": "s", "kind": "pos", "value": lg_codec.encode_input("abc")}], "has_self": False}
    plain = {"literals": ["x"], "fragments": [], "numbers": [], "regexes": [], "guards": [], "dates": False}
    texts = lambda items: {i["args"][0]["value"].get("v") for i in items}  # noqa: E731
    assert not texts(inputs._edit_items(sym, [base], plain, None)) & set(inputs.DATE_POOL)
    assert set(inputs.DATE_POOL) <= texts(inputs._edit_items(sym, [base], {**plain, "dates": True}, None))
    # Examples that look like dates also turn it on.
    dated = {**base, "args": [{**base["args"][0], "value": lg_codec.encode_input("December 21, 2010")}]}
    assert set(inputs.DATE_POOL) <= texts(inputs._edit_items(sym, [dated], plain, None))


def test_youtube_dl5_without_fix_tests_detected_by_date_pool(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg_yt/__init__.py": "", "tests/test_utils.py": DATE_TEST},
                                         "pkg_yt/utils.py", DATE_OLD, DATE_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "unified_timestamp")
    assert r.status == diff.DIFFERENT, r.reason
    pooled = [d for d in r.differences if d["source"] == "edit" and any(repr(s) in d["call"] for s in inputs.DATE_POOL)]
    assert pooled, [d["call"] for d in r.differences]


def test_guard_and_date_inputs_are_deterministic_and_bounded():
    sym = {"qualname": "format_meter", "kind": "function",
           "params": [{"name": "n", "annotation": None, "default": None}, {"name": "total", "annotation": None, "default": None},
                      {"name": "elapsed", "annotation": None, "default": None}, {"name": "unit", "annotation": None, "default": "'it'"},
                      {"name": "unit_scale", "annotation": None, "default": "False"}]}
    bases = [{"args": [{"name": "n", "kind": "pos", "value": lg_codec.encode_input(n)},
                       {"name": "total", "kind": "pos", "value": lg_codec.encode_input(1000)},
                       {"name": "elapsed", "kind": "pos", "value": lg_codec.encode_input(13)}], "has_self": False}
             for n in range(8)]
    edit = {**examples.edit_info(TQDM_OLD, TQDM_NEW), "dates": True}
    a = inputs._edit_items(sym, bases, edit, None)
    assert a == inputs._edit_items(sym, bases, edit, None)
    assert 0 < len(a) <= inputs.MAX_EDIT_ITEMS
    assert all(i["weak"] and i["source"] == "edit" for i in a)
    assert len({json.dumps(i["args"], sort_keys=True) for i in a}) == len(a)


def test_guard_values_respect_type_hints():
    # A typed parameter gets only values its hint allows: '' or None for an `x: int` would differ wherever the edit
    # touched arithmetic and say nothing about valid calls (tests/test_gates.py's partly-reached case).
    sym = {"qualname": "g", "kind": "function", "params": [{"name": "x", "annotation": "int", "default": None},
                                                           {"name": "y", "annotation": "Optional[str]", "default": None}]}
    base = {"args": [{"name": "x", "kind": "pos", "value": lg_codec.encode_input(5)},
                     {"name": "y", "kind": "pos", "value": lg_codec.encode_input("a")}], "has_self": False}
    items = inputs._guard_items(sym, [base], [{"name": "x", "consts": [400]}, {"name": "y", "consts": ["b"]}])
    xs = [lg_codec.decode(i["args"][0]["value"]) for i in items]
    ys = [lg_codec.decode(i["args"][1]["value"]) for i in items]
    assert xs and all(type(v) is int for v in xs) and 401 in xs
    assert None in ys and "" in ys and all(v is None or isinstance(v, str) for v in ys)


# ---- review follow-ups: an untyped parameter's default bounds its guard values; DATE_LIKE; useful guard names ----

LABEL_OLD = '''
def label(n, prefix=''):
    out = str(n)
    if prefix:
        out = prefix + ': ' + out
    return out
'''
LABEL_NEW = LABEL_OLD.replace("out = prefix + ': ' + out", 'out = f"{prefix}: {out}"')
LABEL_TEST = '''
from pkg_lb.fmt import label


def test_label():
    assert label(5) == '5'
    assert label(5, prefix='n') == 'n: 5'
'''


def test_untyped_guard_values_follow_the_default_and_constants():
    def kept(default, consts):
        p = {"name": "p", "annotation": None, "default": default}
        return [v for v in inputs._guard_values(consts) if inputs._fits_guard(v, p, consts)]
    assert kept("''", []) == ["", "x"]
    scale = kept("False", [True, 1])
    assert 10 in scale and 0.5 in scale and None not in scale and "" not in scale
    assert None in kept("None", [])
    assert None in kept("0", [None]) and "" not in kept("0", [None])
    assert kept(None, []) == []  # untyped, required, no constants: nothing is known to be valid


def test_untyped_parameter_with_string_default_is_not_given_numbers(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg_lb/__init__.py": "", "tests/test_fmt.py": LABEL_TEST},
                                         "pkg_lb/fmt.py", LABEL_OLD, LABEL_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "label")
    assert r.status == diff.DONE, (r.reason, [d["call"] for d in r.differences])


def test_date_like_needs_a_date_word_not_update_or_time():
    plain = "def f(x):\n    return x\n"
    for text in ("import time\n\n\ndef update(x):\n    time.sleep(0)\n    return validate(x)\n",
                 "def f(candidate, timeout):\n    return candidate\n"):
        assert examples.edit_info(plain, text)["dates"] is False, text
    for text in ("def f(date_str):\n    return date_str\n", "def f(s):\n    return strptime(s, '%Y')\n",
                 "def f(s):\n    return parse_date(s)\n", "def f(s):\n    return s.timestamp()\n"):
        assert examples.edit_info(plain, text)["dates"] is True, text


def test_guard_names_skip_builtins_self_and_calls():
    old = '''
def f(self, a, b, c, d, e, g, h):
    if isinstance(a, int) and len(b) > 0 and self.x and str(c) and repr(d) and type(e) and id(g) and abs(h) and cls:
        return 1
    return 0
'''
    new = old.replace("return 1", "return 2")
    guards = {g["name"]: g["consts"] for g in examples.edit_info(old, new)["guards"]}
    assert "isinstance" not in guards and "len" not in guards and "self" not in guards and "cls" not in guards
    assert guards.get("b") == []  # 0 is compared to len(b), not to b
    assert set("abcdeg") <= set(guards)  # the cap counts only useful names


def test_date_like_word_edges():
    assert not examples.DATE_LIKE.search("update validate candidate UPDATE import time time.sleep timeout")
    assert all(examples.DATE_LIKE.search(t) for t in ("datetime", "parse_date", "toDate", "Date", "date_str"))
