"""Sprint 5 Task B: inputs from tests (variables, loops, parametrize, objects) and from the edit itself.

Each end-to-end case mirrors a miss from validation run 1 (docs/validation/2026-11-bugsinpy.md). OLD is the fixed
code, NEW the planted bug, as in the harness."""
import json

import pytest

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


def arg_values(d):
    return [a["value"] for a in d["args"]]


# ---- arguments through variables, loops, parametrize, fixtures ----

def scan_calls(tmp_path, text, name, params):
    root = write_tree(tmp_path / "scan", {"tests/test_x.py": text})
    scan = examples._scan_file(str(root), "tests/test_x.py", {name})
    return [[v for _, _, v in c] for c in examples._file_calls(scan, name, params)]


def test_variable_loop_and_concatenation_are_resolved(tmp_path):
    calls = scan_calls(tmp_path, '''
        from pkg import f

        def test_one():
            inp = ('{"a": '
                   '"x\\\\ny"}')
            assert f(inp)
            inp = 'second'
            assert f(inp, flag=True)
            suffix = 'b'
            assert f('a' + suffix)

        def test_loop():
            for s in ['p', 'q']:
                f(s)
            for a, b in [('m', 1), ('n', 2)]:
                f(a, b)

        def test_unknown(tmp_path):
            f(tmp_path)
    ''', "f", ["text", "flag"])
    assert ['{"a": "x\\ny"}'] in calls  # implicit concatenation; the value holds a backslash and an n
    assert ["second", True] in calls
    assert ["ab"] in calls
    assert ["p"] in calls and ["q"] in calls
    assert ["m", 1] in calls and ["n", 2] in calls
    assert not any(len(c) == 1 and not isinstance(c[0], str) for c in calls)


def test_parametrize_fixtures_and_objects(tmp_path):
    calls = scan_calls(tmp_path, '''
        import pytest
        import pkg.types as t
        from pkg.types import Command as Cmd
        from pkg.rules import match

        @pytest.fixture
        def output():
            return 'ERROR: unknown command'

        @pytest.mark.parametrize('script, expected', [
            ('ls -la', True),
            pytest.param('cd', False, id='cd'),
        ])
        def test_match(script, expected, output):
            assert match(Cmd(script, output)) == expected

        def test_module_attr():
            command = t.Command('git push', '')
            match(command)
    ''', "match", ["command"])
    shown = [repr(c[0]) for c in calls]
    assert "Command('ls -la', 'ERROR: unknown command')" in shown
    assert "Command('cd', 'ERROR: unknown command')" in shown
    assert "Command('git push', '')" in shown
    assert all(c[0].cls == "pkg.types:Command" for c in calls)


def test_construct_encoding_is_built_only_in_the_target(tmp_path):
    enc = lg_codec.encode_input(lg_codec.Construct("pkg.types:Command", ["ls", ""], {"env": {"A": "1"}}))
    assert enc["t"] == "construct" and enc["cls"] == "pkg.types:Command"
    assert lg_codec.display(enc) == "Command('ls', '', env={'A': '1'})"
    with pytest.raises(ValueError):
        lg_codec.decode(enc)  # the guard never imports the project to build one


def test_constructor_that_fails_is_setup_failed(tmp_path, run_worker):
    root = write_tree(tmp_path / "p", {"pkg/__init__.py": "", "pkg/m.py": "class Boom:\n    def __init__(self, x):\n        raise RuntimeError('no')\n\ndef f(o):\n    return 1\n"})
    item = {"args": [{"name": "o", "kind": "pos", "value": lg_codec.encode_input(lg_codec.Construct("pkg.m:Boom", [1]))}]}
    missing = {"args": [{"name": "o", "kind": "pos", "value": lg_codec.encode_input(lg_codec.Construct("pkg.nope:Gone", []))}]}
    res = run_worker(root, [{"module": "pkg.m", "qualname": "f", "inputs": [item, missing]}])
    outcomes = [r["outcome"] for r in res["pkg.m:f"]["results"]]
    assert outcomes == ["setup_failed", "setup_failed"]


# ---- youtube-dl 10: the distinguishing input reaches the call through a variable ----

JS_OLD = r'''
import re


def js_to_json(code):
    def fix_kv(m):
        v = m.group(0)
        if v.startswith('"'):
            return v
        return '"%s"' % v
    return re.sub(r'"(?:[^"\\]*(?:\\\\|\\["nu]))*[^"\\]*"|[a-zA-Z_]+', fix_kv, code)
'''
JS_NEW = JS_OLD.replace(r'''r'"(?:[^"\\]*(?:\\\\|\\["nu]))*[^"\\]*"|''', r'''r'"(?:[^"\\]*(?:\\\\|\\")?)*"|''')
JS_TEST = r'''
import unittest

from pkg.utils import js_to_json


class TestJs(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(js_to_json('{abc: "x"}'), '{"abc": "x"}')

    def test_escapes(self):
        inp = '{"duration": "00:01:07", "title": "Line one\\nLine two", "ok": "yes"}'
        self.assertEqual(js_to_json(inp), inp)
'''


def test_js_to_json_variable_input_detected(tmp_path, make_cfg):
    assert JS_NEW != JS_OLD
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg/__init__.py": "", "tests/test_utils.py": JS_TEST},
                                         "pkg/utils.py", JS_OLD, JS_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "js_to_json")
    assert r.status == diff.DIFFERENT
    via_var = [d for d in r.differences if d["source"] == "examples" and "Line one" in d["call"]]
    assert via_var, [d["call"] for d in r.differences][:5]
    assert r.differences[0]["source"] == "examples"


# ---- thefuck 7 and 1: an untyped `command` parameter that tests fill with Command objects ----

TF_TYPES = '''
class Command:
    def __init__(self, script, output):
        self.script = script
        self.output = output

    @property
    def script_parts(self):
        return self.script.split()
'''
PHP_OLD = '''
def match(command):
    return "php -s" in command.script


def get_new_command(command):
    return command.script.replace("-s", "-S")
'''
PHP_NEW = PHP_OLD.replace('"php -s" in command.script', '" -s " in command.script')
PHP_TEST = '''
import pytest

from pkg_tf.rules.php_s import get_new_command, match
from pkg_tf.types import Command


@pytest.mark.parametrize('command', [
    Command('php -s localhost:8000', ''),
    Command('php -t pub -s 0.0.0.0:8080', '')
])
def test_match(command):
    assert match(command)


@pytest.mark.parametrize('command, new_command', [
    (Command('php -s localhost:8000', ''), 'php -S localhost:8000'),
])
def test_get_new_command(command, new_command):
    assert get_new_command(command) == new_command
'''
OTHER_OLD = '''
def get_new_command(command):
    return command.script.split()[0]
'''
OTHER_NEW = OTHER_OLD.replace("[0]", "[-1]")
TF_FILES = {"pkg_tf/__init__.py": "", "pkg_tf/rules/__init__.py": "", "pkg_tf/types.py": TF_TYPES,
            "tests/rules/test_php_s.py": PHP_TEST}


def test_thefuck_match_with_command_objects_detected(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, TF_FILES, "pkg_tf/rules/php_s.py", PHP_OLD, PHP_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "match")
    assert r.status == diff.DIFFERENT, r.reason
    calls = [d["call"] for d in r.differences]
    assert "match(command=Command('php -t pub -s 0.0.0.0:8080', ''))" in calls
    assert r.differences[0]["source"] == "examples"


def test_objects_reach_same_named_parameters_elsewhere(tmp_path, make_cfg):
    # A rule with no tests of its own gets the Command objects other rules' tests build for `command`.
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {**TF_FILES, "pkg_tf/rules/php_s.py": PHP_OLD},
                                         "pkg_tf/rules/other.py", OTHER_OLD, OTHER_NEW)
    objects = examples.param_objects(cfg.root, "pkg_tf/rules/other.py", "get_new_command", targets[0].params, [])
    assert objects and all(o.cls == "pkg_tf.types:Command" for o in objects["command"])
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "get_new_command")
    assert r.status == diff.DIFFERENT, r.reason
    assert any("Command('php -s localhost:8000', '')" in d["call"] for d in r.differences)


# ---- youtube-dl 1: a bool spliced into a dict example ----

MATCH_OLD = '''
def match_str(filter_str, dct):
    for part in filter_str.split('&'):
        key = part.strip()
        v = dct.get(key)
        ok = (v is True) if isinstance(v, bool) else (v is not None)
        if not ok:
            return False
    return True
'''
MATCH_NEW = MATCH_OLD.replace("ok = (v is True) if isinstance(v, bool) else (v is not None)", "ok = v is not None")
MATCH_TEST = '''
from pkg.filters import match_str


def test_match_str():
    assert match_str('x', {'x': 1200})
    assert not match_str('y', {'x': 1200})
'''


def test_match_str_bool_into_dict_detected(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg/__init__.py": "", "tests/test_filters.py": MATCH_TEST},
                                         "pkg/filters.py", MATCH_OLD, MATCH_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "match_str")
    assert r.status == diff.DIFFERENT, r.reason
    assert all(d["source"] == "edit" for d in r.differences)
    assert "match_str(filter_str='x', dct={'x': False})" in [d["call"] for d in r.differences]


# ---- youtube-dl 3: '&' spliced before an entity ----

UNESCAPE_OLD = r'''
import re


def _transform(name):
    if name.startswith('#'):
        return chr(int(name[1:]))
    return {'amp': '&', 'quot': '"'}.get(name, '&%s;' % name)


def unescapeHTML(s):
    return re.sub(r'&([^&;]+);', lambda m: _transform(m.group(1)), s)
'''
UNESCAPE_NEW = UNESCAPE_OLD.replace("[^&;]+", "[^;]+")
UNESCAPE_TEST = '''
from pkg.html import unescapeHTML


def test_unescape():
    assert unescapeHTML('&#47;') == '/'
    assert unescapeHTML('&quot;') == '"'
'''


def test_unescape_ampersand_spliced_before_entity_detected(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg/__init__.py": "", "tests/test_html.py": UNESCAPE_TEST},
                                         "pkg/html.py", UNESCAPE_OLD, UNESCAPE_NEW)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "unescapeHTML")
    assert r.status == diff.DIFFERENT, r.reason
    spliced = [d for d in r.differences if d["source"] == "edit" and d["call"] == "unescapeHTML(s='&&#47;')"]
    assert spliced and spliced[0]["before"] == "'&/'"


# ---- a regex-only edit with no tests: strings drawn from both patterns ----

WORDS_OLD = r'''
import re


def words(s):
    return re.findall(r'[a-z]+', s)
'''
WORDS_NEW = WORDS_OLD.replace("[a-z]+", "[a-z0-9]+")


def test_regex_only_edit_detected_by_from_regex_sample(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg/__init__.py": ""}, "pkg/words.py", WORDS_OLD, WORDS_NEW)
    edit = examples.edit_info(targets[0].source, targets[0].new_source)
    assert edit["regexes"] == [{"fn": "findall", "old": "[a-z]+", "new": "[a-z0-9]+", "flags_old": 0, "flags_new": 0}]
    samples = inputs._regex_samples(edit["regexes"], cfg.state_dir)
    assert samples and all(__import__("re").findall("[a-z]+", s) != __import__("re").findall("[a-z0-9]+", s) for s in samples)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "words")
    assert r.status == diff.DIFFERENT, r.reason
    sampled = [d for d in r.differences if d["source"] == "edit" and lg_codec.decode(d["args"][0]["value"]) in samples]
    assert sampled


def test_regex_hypothesis_cannot_handle_is_skipped(tmp_path):
    bad = [{"fn": "search", "old": "(", "new": "a", "flags_old": 0, "flags_new": 0},
           {"fn": "search", "old": r"(a)\1", "new": r"(a)a", "flags_old": 0, "flags_new": 0}]
    inputs._regex_samples(bad, tmp_path)  # must not raise


# ---- numbers, budget, determinism ----

def test_numbers_between_and_around_changed_constants():
    edit = examples.edit_info("def f(x):\n    return x > 10\n", "def f(x):\n    return x > 20\n")
    assert edit["numbers"] == [[10, 20]]
    values = inputs._number_values(edit["numbers"])
    assert {10, 20, 15, 9, 11, 19, 21} <= set(values)


def test_numeric_edit_reaches_untyped_param(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "x", "annotation": None, "default": None}]}
    edit = examples.edit_info("def f(x):\n    return x > 10\n", "def f(x):\n    return x > 20\n")
    items, _ = inputs.build(sym, None, 60, tmp_path, examples={"pool": [], "calls": [], "edit": edit})
    edit_vals = [lg_codec.decode(i["args"][0]["value"]) for i in items if i["source"] == "edit"]
    assert 15 in edit_vals and 10 in edit_vals


def test_seeded_inputs_leave_a_third_for_generic(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "s", "annotation": "str", "default": None}]}
    harvested = {"inputs": [[{"name": "s", "kind": "pos", "value": lg_codec.encode_input(f"t{i}")}] for i in range(100)]}
    edit = {"literals": ["&"], "fragments": [], "numbers": [], "regexes": []}
    items, _ = inputs.build(sym, harvested, 30, tmp_path, examples={"pool": [], "calls": [], "edit": edit})
    sources = [i["source"] for i in items]
    assert len(items) == 30
    assert sources.count("generated") >= 10
    assert sources.count("edit") >= 1 and sources.count("tests") >= 10


def test_seeded_inputs_fill_the_budget_when_generation_cannot(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "o", "annotation": "Widget", "default": None}]}
    harvested = {"inputs": [[{"name": "o", "kind": "pos", "value": lg_codec.encode_input(i)}] for i in range(40)]}
    items, _ = inputs.build(sym, harvested, 30, tmp_path)
    assert len(items) == 30 and all(i["source"] == "tests" for i in items)


def test_inputs_are_deterministic(tmp_path, make_cfg):
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg/__init__.py": "", "tests/test_html.py": UNESCAPE_TEST},
                                         "pkg/html.py", UNESCAPE_OLD, UNESCAPE_NEW)
    t = targets[0]
    def once(sub):
        ex = diff._examples(cfg, None, t)
        items, _ = inputs.build({"qualname": t.qualname, "kind": t.kind, "params": t.params}, None, 200, tmp_path / sub, examples=ex)
        return json.dumps(items, sort_keys=True)
    first = once("a")
    assert first == once("b")
    assert '"edit"' in first


# ---- time bound: youtube-dl js_to_json, whose verbose nested pattern makes Hypothesis's from_regex run for minutes ----

@pytest.mark.parametrize("variant", ["real fix", "same-language rewrite"])
def test_real_js_to_json_edit_builds_inputs_quickly(tmp_path, make_cfg, variant):
    import time
    from test_mutants import YT_NEW, YT_OLD, YT_TESTS  # youtube-dl 85d586617 / d305dd73a, copied verbatim there
    old = "import re\n\n\n" + YT_OLD
    if variant == "real fix":
        new = "import re\n\n\n" + YT_NEW
    else:
        new = old.replace('[^"\\\\]*(?:', '[^\\\\"]*(?:').replace("[^'\\\\]*(?:", "[^\\\\']*(?:")
    assert new != old
    files = {"pkg/__init__.py": "", "tests/test_m.py": YT_TESTS}
    work = write_tree(tmp_path / "work", {**files, "pkg/m.py": new})
    base = write_tree(tmp_path / "base", {**files, "pkg/m.py": old})
    cfg = make_cfg(work)
    t = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (work / "pkg/m.py").read_text())[0]
    started = time.monotonic()
    ex = diff._examples(cfg, None, t)
    items, _ = inputs.build({"qualname": t.qualname, "kind": t.kind, "params": t.params}, None, 500, cfg.state_dir, examples=ex)
    took = time.monotonic() - started
    assert ex["edit"]["regexes"], "the pattern change must be seen"
    assert took < inputs.SEED_BUDGET_S + 3, took
    assert items and len(items) <= 500
    # No edit input makes the edited pattern backtrack: an unterminated quote spliced into the tests' long strings would
    # time out in both versions and use up the worker's budget.
    import re as stdlib_re
    patterns = [stdlib_re.compile(r[k]) for r in ex["edit"]["regexes"] for k in ("old", "new")]
    for item in items:
        if item["source"] != "edit":
            continue
        text = lg_codec.decode(item["args"][0]["value"])
        for rx in patterns:
            begun = time.monotonic()
            try:
                with inputs._time_limit(0.5):
                    rx.sub("", text)
            except inputs._RegexSlow:
                pass
            assert time.monotonic() - begun < 0.4, repr(text)[:80]


def test_regex_sampling_child_is_killed_at_the_budget(tmp_path):
    import time
    from test_mutants import YT_NEW, YT_OLD
    edit = examples.edit_info(YT_OLD, YT_NEW)
    started = time.monotonic()
    slow, samples = inputs._regex_child(edit["regexes"], ['"' + "a" * 40, "plain"], tmp_path, 0.5)
    assert time.monotonic() - started < 2.5
    assert isinstance(samples, list) and slow in (None, ['"' + "a" * 40])  # screened, or dropped for lack of time
    assert inputs._regex_child(edit["regexes"], ["x"], tmp_path, 0.0) == (None, [])  # no budget left: nothing


# ---- review fixes: edit inputs are guesses, failures fall back, one walk per check ----

PORT_OLD = '''
def port(addr):
    host, p = addr.split(':')
    return int(p)
'''
PORT_NEW = '''
def port(addr):
    host, sep, p = addr.partition(':')
    if ':' in p:
        raise ValueError('bad address')
    return int(p)
'''
PORT_TEST = '''
from pkg.net import port


def test_port():
    assert port('localhost:80') == 80
'''


def test_edit_splices_rejected_by_both_versions_are_not_a_difference(tmp_path, make_cfg):
    # Same results on every valid address; a ':' spliced into the example makes both raise, with different messages.
    cfg, base, work, targets = two_trees(tmp_path, make_cfg, {"pkg/__init__.py": "", "tests/test_net.py": PORT_TEST},
                                         "pkg/net.py", PORT_OLD, PORT_NEW)
    ex = diff._examples(cfg, None, targets[0])
    items, _ = inputs.build({"qualname": "port", "kind": "function", "params": targets[0].params}, None, 500, cfg.state_dir, examples=ex)
    edit = [i for i in items if i["source"] == "edit"]
    assert edit and all(i["weak"] for i in edit)
    assert any(lg_codec.decode(i["args"][0]["value"]) == ":localhost:80" for i in edit)
    r = result_for(diff.run(cfg, base, work, targets, harvest={}), "port")
    assert r.status in (diff.DONE, diff.UNTESTED), [d["call"] for d in r.differences][:3]
    assert not r.differences and r.dropped_invalid > 0


def test_malformed_values_fall_back_to_previous_inputs(tmp_path):
    sym = {"qualname": "f", "kind": "function", "params": [{"name": "s", "annotation": "str", "default": None}]}
    harvested = {"inputs": [[{"name": "s", "kind": "pos", "value": "not an encoded value"}]]}
    edit = {"literals": ["&"], "fragments": [], "numbers": [], "regexes": []}
    objects = {"s": ["not a Construct"]}
    items, reason = inputs.build(sym, harvested, 30, tmp_path, examples={"pool": [], "calls": [], "edit": edit, "objects": objects})
    assert reason is None
    sources = [i["source"] for i in items]
    assert sources[0] == "tests" and "generated" in sources and "edit" not in sources


def test_test_files_come_from_one_pruned_walk(tmp_path, monkeypatch):
    import os
    root = write_tree(tmp_path / "w", {"tests/test_a.py": "command = 1\nmatch(command)\n", "tests/test_b.py": "x = 1\n",
                                       ".venv/lib/test_v.py": "command match", "node_modules/m/test_n.py": "command match",
                                       "build/test_b.py": "command match", "pkg/m.py": "command match"})
    walks = []
    real = os.walk
    def counting(top, *a, **k):
        walks.append(top)
        return real(top, *a, **k)
    monkeypatch.setattr(os, "walk", counting)
    examples._TEST_FILES.clear()
    hits = examples._test_files_with(str(root), {"command": ["match"], "x": ["x"]})
    hits2 = examples._test_files_with(str(root), {"command": ["match"]})
    assert hits == {"command": ["tests/test_a.py"], "x": ["tests/test_b.py"]} and hits2 == {"command": ["tests/test_a.py"]}
    assert len(walks) == 1
