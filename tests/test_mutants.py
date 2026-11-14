"""Planted bugs on the changed lines measure how strong a pass is (roadmap B6)."""
import re

import pytest

from runner import diff, mutants

from conftest import call, write_tree

FN = '''def tier(points: int) -> str:
    if points >= 500:
        return "gold"
    return "basic"
'''


def test_generate_only_on_changed_lines():
    plan = mutants.generate(FN, 10, [[11, 11]])  # line 2 of the function (the `if`) is line 11 of the file
    assert plan["name"] == "tier"
    changes = {m["change"] for m in plan["mutants"]}
    assert "GtE -> Gt" in changes and "500 -> 501" in changes
    assert not any("return" in c for c in changes)  # the return lines did not change


def test_generate_skips_decorated_and_unchanged():
    assert mutants.generate("@cache\n" + FN, 1, [[3, 3]]) is None
    assert mutants.generate(FN, 1, []) is None
    assert mutants.generate(FN, 1, [[50, 60]]) is None


SRC = '''
def tier(points: int) -> str:
    if points >= 500:
        return "gold"
    return "basic"


def make_adder(n):
    def add(x: int) -> int:
        return x + n
    return add


def countdown(n: int) -> int:
    n = min(n, 50)
    steps = 0
    while n > 0:
        n -= 1
        steps += 1
    return steps
'''


def run(tmp_path, make_cfg, new_src, **bd):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/m.py": new_src})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/m.py": SRC})
    cfg = make_cfg(root, behavioral_diff=bd) if bd else make_cfg(root)
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_pass_reports_confidence(tmp_path, make_cfg):
    new = SRC.replace("if points >= 500:", "if not points < 500:")
    r = run(tmp_path, make_cfg, new)["tier"]
    assert r.status == diff.DONE
    assert r.mutants_total >= 2 and r.mutants_killed == r.mutants_total  # 500 is in the repo's own values, so the edge is tried
    assert "planted bugs in the changed lines were caught" in diff.format_note([r])


def test_looping_mutant_counts_as_caught(tmp_path, make_cfg):
    new = SRC.replace("        n -= 1\n", "        n = n - 1\n")
    r = run(tmp_path, make_cfg, new)["countdown"]
    assert r.status == diff.DONE and r.mutants_total >= 1 and r.mutants_killed == r.mutants_total


def test_mutants_can_be_switched_off(tmp_path, make_cfg):
    new = SRC.replace("if points >= 500:", "if not points < 500:")
    r = run(tmp_path, make_cfg, new, mutants=0)["tier"]
    assert r.status == diff.DONE and r.mutants_total == 0 and diff.confidence_text(r) == ""


def test_worker_refuses_when_recompiling_changes_behavior(tmp_path, run_worker):
    # A method using zero-argument super() cannot be recompiled on its own: the identity check must notice.
    src = "class A:\n    def f(self, x: int):\n        return x\n\nclass B(A):\n    def f(self, x: int):\n        return super().f(x) + 1\n"
    root = write_tree(tmp_path / "w", {"m.py": src})
    plan = mutants.generate("def f(self, x: int):\n    return super().f(x) + 1\n", 6, [[7, 7]])
    out = run_worker(root, [{"module": "m", "qualname": "B.f", "inputs": [call(1), call(2)], "mutants": plan}])["m:B.f"]
    assert out["mutation"]["applicable"] is False


# ---- strings (mutmut) and regexes (MutRex subset): planted bugs for edits that only touch text ----

STR_FN = '''def label(kind: str, text: str) -> str:
    """Docstring 'stays'."""
    log.info("not mutated %s", kind)
    head = f"{kind}:"
    return head + text.lstrip().lower() + "Tail"
'''


def test_string_operators_on_changed_line_only():
    plan = mutants.generate(STR_FN, 1, [[5, 5]])
    changes = {m["change"] for m in plan["mutants"]}
    assert "string 'Tail' -> 'XXTailXX'" in changes
    assert "string 'Tail' -> ''" in changes
    assert "string 'Tail' -> 'TAIL'" in changes
    assert any(c.startswith("method .l") for c in changes)  # lstrip -> rstrip or lower -> upper
    assert not any("Docstring" in c or "not mutated" in c for c in changes)


def test_string_skips_docstrings_fstrings_logs_annotations_and_triple_quotes():
    fn = '''def f(x: "Annot") -> "Ret":
    """doc"""
    print("hello")
    y = f"{x}-part"
    z = """triple"""
    return y + "kept"
'''
    plan = mutants.generate(fn, 1, [[1, 6]])
    changes = " ".join(m["change"] for m in plan["mutants"])
    assert "kept" in changes
    for word in ("Annot", "Ret", "doc", "hello", "part", "triple"):
        assert word not in changes


def test_method_swaps_and_split_needs_maxsplit():
    fn = '''def f(s):
    a = s.split(",")
    b = s.split(",", 1)
    c = s.find("x")
    d = os.path.split(s)
    return a, b, c, d
'''
    sites = mutants._sites(mutants.ast.parse(fn).body[0], 1, [[2, 5]], fn)
    methods = [(s[1], s[2][1]) for s in sites if s[0] == "method"]
    assert [c for _, c in methods] == ["method .split() -> .rsplit()", "method .find() -> .rfind()"]
    assert methods[0][0][0] == ("body", 1)  # the split with maxsplit, not the plain one


def test_case_change_keeps_escapes_and_identical_mutants_are_dropped():
    assert mutants._swap_case("a\\d", upper=True) == "A\\d"
    assert [s for s, _ in mutants._string_mutants("123")] == ["XX123XX", ""]  # no case change for "123"


@pytest.mark.parametrize("pattern,op,expected", [
    (r"[a-z0-9]+", "CCR", r"[0-9]+"),
    (r"[a-z0-9]+", "CCR", r"[a-z]+"),
    (r"[a-z]", "RM", r"[a-y]"),
    (r"[a-z]", "RM", r"[b-z]"),
    (r"\d*x", "QC", r"\d+x"),
    (r"\d+x", "QC", r"\d*x"),
    (r"\d?x", "QC", r"\dx"),
    (r"\d{2}", "QC", r"\d{3}"),
    (r"a.b", "M2C", r"a\.b"),
    (r"[ab]", "CCN", r"[^ab]"),
    (r"[^ab]", "CCN", r"[ab]"),
    (r"\dk", "CA", r"\d[kK]"),
])
def test_regex_operators(pattern, op, expected):
    made = mutants._regex_mutants(pattern)
    assert (expected, op) in {(m, c.split(":")[0].split()[1]) for m, c in made}
    for m, _ in made:
        re.compile(m)  # every mutant is still a regex
        assert m != pattern


def test_regex_leaves_escapes_and_group_syntax_alone():
    made = [m for m, _ in mutants._regex_mutants(r"(?s)(?P<name>\w)\.x")]
    assert made and all("(?s)" in m and "(?P<name>" in m and "\\w" in m and "\\." in m for m in made)
    assert r"(?s)(?P<name>\w)\.[xX]" in made


def test_regex_mutants_that_do_not_compile_are_skipped(monkeypatch):
    real = mutants._compiles
    monkeypatch.setattr(mutants, "_compiles", lambda p, flags=0: real(p, flags) and "[^" not in p)  # no CCN compiles
    made = mutants._regex_mutants(r"[ab]c")
    assert made and not any(m.startswith("[^") for m, _ in made)


RE_FN = '''def js_to_json(code):
    out = re.sub(r'([a-z_]\\w*)\\s*:', r'"\\1":', code)
    out = sub(r'\\bfoo\\b', 'bar', out)
    out = sub('plain', 'x', out)
    back = re.sub(r'(a)\\1', 'b', out) + re.sub(r'x(?=y)', 'b', out)
    pat = r'v[0-9]+'
    return re.findall(pat, out) and back
'''


def test_regex_sites_are_patterns_of_re_calls():
    fn = mutants.ast.parse(RE_FN).body[0]
    patterns, skipped, _ = mutants._text_roles(fn, RE_FN, {})
    values = {n.value for n in mutants.ast.walk(fn) if id(n) in patterns}
    # A bare sub('plain') is not clearly a regex. Patterns with a backreference or lookaround are still patterns
    # (only the parts outside those constructs get mutated).
    assert values == {r'([a-z_]\w*)\s*:', r'\bfoo\b', r'v[0-9]+', r'(a)\1', r'x(?=y)'}
    assert not any(id(n) in skipped for n in mutants.ast.walk(fn) if id(n) in patterns)
    aliases = mutants.re_aliases("from re import sub as s\nimport re as rx\n")
    assert aliases["funcs"] == {"s": "sub"} and "rx" in aliases["modules"]
    fn2 = mutants.ast.parse("def g(t):\n    return s('plain', '', t)\n").body[0]
    assert len(mutants._text_roles(fn2, "", aliases)[0]) == 1  # an imported alias counts without the heuristic


def test_picks_mix_operator_kinds():
    plan = mutants.generate(RE_FN, 1, [[1, 7]])
    ops = [m["change"].split(":")[0] for m in plan["mutants"]]
    assert len(ops) == mutants.MAX_MUTANTS
    assert len({o for o in ops if o.startswith("regex")}) >= 2  # several MutRex operators, not 8 of one
    assert any(o.startswith("string") for o in ops) and any(not o.startswith(("regex", "string")) for o in ops)


JS_OLD = '''import re


def js_to_json(code: str) -> str:
    return re.sub(r'([a-zA-Z_][a-zA-Z_0-9]*)\\s*:', r'"\\1":', code)


def slug(name: str) -> str:
    return name.strip().lower()
'''
JS_TESTS = '''from pkg.m import js_to_json, slug


def test_it():
    assert js_to_json("{abc: 1, Bcd :2}") == '{"abc": 1, "Bcd": 2}'
    assert js_to_json("{x9: 'y', _k: [1]}")
    assert js_to_json("no keys here")
    assert slug("  Hello World ") == "hello world"
    assert slug("ABC")
'''


def run_text(tmp_path, make_cfg, new_src):
    files = {"pkg/__init__.py": "", "tests/test_m.py": JS_TESTS}
    root = write_tree(tmp_path / "proj", {**files, "pkg/m.py": new_src})
    base = write_tree(tmp_path / "base", {**files, "pkg/m.py": JS_OLD})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    return {r.qualname: r for r in diff.run(make_cfg(root), str(base), str(root), targets, harvest={})}


def test_regex_only_edit_gets_planted_bugs(tmp_path, make_cfg):
    # A js_to_json-style edit that only rewrites the pattern (same language). Before the string and regex operators
    # this verified with 0 planted bugs, which said nothing about the inputs.
    new = JS_OLD.replace("[a-zA-Z_][a-zA-Z_0-9]*", "[A-Za-z_][A-Za-z0-9_]*")
    r = run_text(tmp_path, make_cfg, new)["js_to_json"]
    assert r.status == diff.DONE
    assert r.mutants_total > 0 and 0 < r.mutants_killed <= r.mutants_total
    assert len(r.mutants_survived) == min(5, r.mutants_total - r.mutants_killed)
    assert all(s.split()[0] in ("regex", "string", "returns") for s in r.mutants_survived)
    assert "planted bugs in the changed lines were caught" in diff.confidence_text(r)


def test_verbose_patterns_skip_whitespace_and_comments():
    for fn in ('def f(s):\n    return re.findall(r"\\d+  # digits only", s, re.X)\n',
               'def f(s):\n    return re.findall(r"\\d+  # digits only", s, flags=re.VERBOSE)\n',
               'def f(s):\n    return re.findall(r"(?x)\\d+  # digits only", s)\n'):
        plan = mutants.generate(fn, 1, [[2, 2]])
        regex = [m["change"] for m in plan["mutants"] if m["change"].startswith("regex")]
        assert regex and all(c.startswith("regex QC") for c in regex), regex  # no CA on the comment's letters
    assert mutants._regex_mutants("a b", verbose=True) == [("[aA] b", "regex CA: 'a b' -> '[aA] b'"),
                                                           ("a [bB]", "regex CA: 'a b' -> 'a [bB]'")]


def test_ignorecase_patterns_get_no_case_mutants():
    for fn in ('def f(s):\n    return re.search("abc", s, re.I)\n',
               'def f(s):\n    return re.search("abc", s, flags=re.IGNORECASE)\n',
               'def f(s):\n    return re.search("(?i)abc", s)\n'):
        plan = mutants.generate(fn, 1, [[2, 2]])
        assert not any("CA" in m["change"] for m in (plan or {"mutants": []})["mutants"]), fn
    assert any(c.startswith("regex CA") for _, c in mutants._regex_mutants("abc"))


def test_lookarounds_and_backreferences_are_left_alone():
    p = r"(\w+)-(?!x[a-z]+)\1:(?P<n>q)(?P=n)k[0-9]"
    made = mutants._regex_mutants(p)
    assert made
    for m, change in made:
        re.compile(m)
        assert m.startswith(r"(\w+)-(?!x[a-z]+)\1:(?P<n>q)(?P=n)"), change  # only k[0-9] at the end is mutated
    assert {c.split(":")[0] for _, c in made} >= {"regex CA", "regex CCN", "regex RM"}
    fn = 'def f(s):\n    return re.sub(r"(a)b\\1(?=c)d", "", s)\n'
    assert mutants.generate(fn, 1, [[2, 2]])  # such a pattern is no longer skipped as a whole


def test_conditional_group_does_not_shift_group_numbers():
    p = r"(a)?(?(1)b|c)(d)\2"
    spans = mutants._protected(p, verbose=False)
    assert (p.index("(d)"), p.index("(d)") + 3) in spans and (0, 3) in spans
    assert (6, 9) not in spans  # the condition (1) is not a group
    assert not any("[dD]" in m for m, _ in mutants._regex_mutants(p))


def test_scoped_flag_groups_are_left_alone():
    for p in ("x(?i:ab)y", "x(?-i:ab)y", "x(?x: a b # c d\n)y", "x(?im:ab)y"):
        re.compile(p)
        made = [m for m, _ in mutants._regex_mutants(p)]
        assert made, p
        inner = p[p.index("("):p.rindex(")") + 1]
        assert all(inner in m for m in made), p  # only x and y outside the group are mutated


def test_codec_names_and_stream_writes():
    fn = '''def f(b):
    sys.stdout.write("Progress")
    sys.stderr.write("Oops")
    return b.decode("UTF-8") + codecs.encode(b, "Rot13")
'''
    changes = [m["change"] for m in mutants.generate(fn, 1, [[1, 4]], limit=20)["mutants"]]
    assert not any("Progress" in c or "Oops" in c for c in changes)
    assert "string 'UTF-8' -> ''" in changes and not any("'utf-8'" in c or "'ROT13'" in c for c in changes)


def test_case_change_uses_backslash_parity():
    assert mutants._swap_case("\\\\a", upper=True) == "\\\\A"   # \\ then a: the a is not escaped
    assert mutants._swap_case("\\\\\\d", upper=True) == "\\\\\\d"  # \\ then \d: the d is


def test_module_source_warnings_stay_quiet():
    import warnings
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        aliases = mutants.re_aliases("from re import sub\nx = '\\d'\n")
        mutants.generate("def f():\n    return '\\d'\n", 1, [[2, 2]])
    assert aliases["funcs"] == {"sub": "sub"} and not seen


# youtube-dl-10 (BugsInPy): js_to_json before and after commit d305dd73a, copied verbatim. The pattern is a
# triple-quoted verbose regex, which the first version of the string operators skipped as a triple-quoted string.
YT_OLD = r"""def js_to_json(code):
    def fix_kv(m):
        v = m.group(0)
        if v in ('true', 'false', 'null'):
            return v
        if v.startswith('"'):
            return v
        if v.startswith("'"):
            v = v[1:-1]
            v = re.sub(r"\\\\|\\'|\"", lambda m: {
                '\\\\': '\\\\',
                "\\'": "'",
                '"': '\\"',
            }[m.group(0)], v)
        return '"%s"' % v

    res = re.sub(r'''(?x)
        "(?:[^"\\]*(?:\\\\|\\")?)*"|
        '(?:[^'\\]*(?:\\\\|\\')?)*'|
        [a-zA-Z_][.a-zA-Z_0-9]*
        ''', fix_kv, code)
    res = re.sub(r',(\s*\])', lambda m: m.group(1), res)
    return res
"""
YT_NEW = YT_OLD.replace(r'''"(?:[^"\\]*(?:\\\\|\\")?)*"|''', r'''"(?:[^"\\]*(?:\\\\|\\['"nu]))*[^"\\]*"|''') \
               .replace(r"""'(?:[^'\\]*(?:\\\\|\\')?)*'|""", r"""'(?:[^'\\]*(?:\\\\|\\['"nu]))*[^'\\]*'|""")


def test_youtube_dl_js_to_json_pattern_gets_regex_mutants():
    assert YT_NEW != YT_OLD and r"\\['\"nu]" not in YT_OLD
    for gate in ([[17, 21]], [[18, 19]]):  # the changed statement, and only the two changed pattern lines
        plan = mutants.generate(YT_OLD, 1, gate, module_source="import re\n")
        regex = [m for m in plan["mutants"] if m["change"].startswith("regex")]
        assert len(regex) >= 3, gate
        for m in regex:
            mutants.ast.parse(m["source"])
    only_lines = mutants.generate(YT_OLD, 1, [[18, 19]])
    assert not any("a-zA-Z" in m["change"] for m in only_lines["mutants"])  # line 20 of the pattern did not change


YT_TESTS = r'''from pkg.m import js_to_json


def test_js_to_json():
    assert js_to_json("""{
            'clip':{'provider':'pseudo'}
        }""")
    assert js_to_json("{abc_def:'1\\'\\\\2\\\\\\'3\"4'}")
    assert js_to_json('{"abc": true}')
    assert js_to_json("""{
            "x": 1,
            y: "a",
            z: some.code
        }""")
'''


def test_youtube_dl_js_to_json_edit_gets_planted_bugs(tmp_path, make_cfg):
    # The real function with an edit to the same two pattern lines that keeps the language ([^"\\] -> [^\\"]), so
    # the diff verifies and the planted bugs run. (The real fix changes behaviour and is reported as different.)
    old = "import re\n\n\n" + YT_OLD
    new = old.replace('[^"\\\\]*(?:', '[^\\\\"]*(?:').replace("[^'\\\\]*(?:", "[^\\\\']*(?:")
    assert new != old
    files = {"pkg/__init__.py": "", "tests/test_m.py": YT_TESTS}
    root = write_tree(tmp_path / "proj", {**files, "pkg/m.py": new})
    base = write_tree(tmp_path / "base", {**files, "pkg/m.py": old})
    targets = diff.changed_targets("pkg/m.py", (base / "pkg/m.py").read_text(), (root / "pkg/m.py").read_text())
    r = {x.qualname: x for x in diff.run(make_cfg(root), str(base), str(root), targets, harvest={})}["js_to_json"]
    assert r.status == diff.DONE, r.reason
    assert r.mutants_total > 0 and r.mutants_killed <= r.mutants_total
    assert all(s.startswith(("regex", "string")) for s in r.mutants_survived)


def test_string_method_edit_gets_planted_bugs(tmp_path, make_cfg):
    new = JS_OLD.replace("return name.strip().lower()", "return str.lower(name.strip())")
    r = run_text(tmp_path, make_cfg, new)["slug"]
    assert r.status == diff.DONE and r.mutants_total > 0 and r.mutants_killed > 0
