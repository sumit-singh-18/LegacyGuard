"""Bytes-like parameters (Sprint 6.8 task 6.8b): inputs built from bytes, bytearray and memoryview hints, carried
through the codec, compared and shown as Python literals."""
from runner import diff, inputs, lg_codec

from conftest import write_tree


def sym(**hints):
    return {"qualname": "f", "kind": "function",
            "params": [{"name": n, "annotation": a, "default": None} for n, a in hints.items()]}


def values_of(items, name="data"):
    return [lg_codec.decode(a["value"]) for i in items for a in i["args"] if a["name"] == name]


# ---- codec ----

def test_codec_round_trips_bytes_like_values():
    for value in (b"", b"a", b"\xff\xfe", "é".encode(), b"\xef\xbb\xbfhi", b"a\r\nb\n"):
        assert lg_codec.decode(lg_codec.encode_input(value)) == value
        assert type(lg_codec.decode(lg_codec.encode_input(value))) is bytes
    arr = lg_codec.decode(lg_codec.encode_input(bytearray(b"ab")))
    assert type(arr) is bytearray and arr == bytearray(b"ab")
    view = lg_codec.decode(lg_codec.encode_input(memoryview(b"xyz")))
    assert type(view) is memoryview and view.tobytes() == b"xyz"


def test_canon_tells_bytes_like_values_apart():
    assert lg_codec.canon(b"ab") != lg_codec.canon(bytearray(b"ab"))
    assert lg_codec.canon(b"ab") != lg_codec.canon("ab")
    # Two memoryviews with different contents must not collapse to one scrubbed repr.
    assert lg_codec.canon(memoryview(b"a")) != lg_codec.canon(memoryview(b"b"))
    assert lg_codec.canon(memoryview(b"a")) == lg_codec.canon(memoryview(b"a"))
    released = memoryview(b"a")
    released.release()
    assert lg_codec.canon(released)  # never fails


def test_display_shows_python_literals():
    assert lg_codec.display(lg_codec.canon(b"a\r\n\xff")) == repr(b"a\r\n\xff")
    assert lg_codec.display(lg_codec.canon(bytearray(b"ab"))) == "bytearray(b'ab')"
    assert lg_codec.display(lg_codec.encode_input(memoryview(b"ab"))) == "memoryview(b'ab')"


# ---- builder ----

def test_bytes_hints_get_inputs(tmp_path):
    for hint, kind in (("bytes", bytes), ("bytearray", bytearray), ("memoryview", memoryview)):
        items, reason = inputs.build(sym(data=hint), None, 60, tmp_path)
        values = values_of(items)
        assert reason is None and values, hint
        assert all(type(v) is kind for v in values), hint
        raw = [bytes(v) for v in values]
        assert b"" in raw and b"a" in raw and b"\xef\xbb\xbf" in b"".join(raw) and any(b"\r\n" in v for v in raw), hint
        assert any(_invalid_utf8(v) for v in raw) and any(_non_ascii_utf8(v) for v in raw), hint


def _invalid_utf8(v: bytes) -> bool:
    try:
        v.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _non_ascii_utf8(v: bytes) -> bool:
    return not _invalid_utf8(v) and any(b > 127 for b in v)


def test_optional_and_union_hints_with_bytes(tmp_path):
    for hint in ("Optional[bytes]", "bytes | None", "Union[bytes, str]", "typing.Union[str, bytes, None]", "str|bytes"):
        items, reason = inputs.build(sym(data=hint), None, 80, tmp_path)
        kinds = {type(v) for v in values_of(items)}
        assert reason is None and bytes in kinds, hint
    kinds = {type(v) for v in values_of(inputs.build(sym(data="Union[bytes, str]"), None, 80, tmp_path)[0])}
    assert str in kinds


def test_inner_none_in_collection_hints_is_kept(tmp_path):
    # Only a top-level None makes a hint optional: list[int|None] is a list that may hold None.
    for hint in ("list[int|None]", "dict[str,int|None]", "tuple[int|None,str]"):
        values = values_of(inputs.build(sym(data=hint), None, 80, tmp_path)[0])
        inner = [x for v in values if v is not None for x in (v.values() if isinstance(v, dict) else v)]
        assert any(x is None for x in inner), hint


def test_hypothesis_draws_bytes_beyond_the_pool(tmp_path):
    out = inputs._generated(sym(data="bytes"), 40, tmp_path)
    drawn = values_of(out)
    assert drawn and all(type(v) is bytes for v in drawn)
    assert len(set(drawn) - set(inputs.BYTES_POOL)) > 5


def test_runtime_types_fill_bytes_hints():
    s = {"qualname": "f", "kind": "function", "params": [{"name": n, "annotation": None, "default": None}
                                                         for n in ("a", "b", "c")]}
    harvested = {"types": {"a": {"bytes": 3}, "b": {"bytearray": 2, "None": 1}, "c": {"memoryview": 1}}}
    params = {p["name"]: p["annotation"] for p in inputs.with_runtime_types(s, harvested)["params"]}
    assert params == {"a": "bytes", "b": "Optional[bytearray]", "c": "memoryview"}


def test_harvest_names_bytes_like_types():
    import sys
    from conftest import PLUGIN
    sys.path.insert(0, str(PLUGIN / "runner"))
    from legacyguard_harvest import type_name
    assert type_name(b"x") == "bytes" and type_name(bytearray(b"x")) == "bytearray"
    assert type_name(memoryview(b"x")) == "memoryview" and type_name([b"a", b"b"]) == "list[bytes]"


def test_edit_splices_into_bytes_arguments(tmp_path):
    base = {"args": [{"name": "data", "kind": "pos", "value": lg_codec.encode_input(b"abc")}], "has_self": False}
    edit = {"literals": [b"\x00", "end"], "fragments": [], "numbers": [], "regexes": [], "guards": []}
    out = inputs._edit_items(sym(data="bytes"), [base], edit, tmp_path)
    got = values_of(out)
    assert b"\x00abc" in got and b"abcend" in got and all(type(v) is bytes for v in got)


def test_guards_on_bytes_constants(tmp_path):
    guards = [{"name": "data", "consts": [b"GIF"]}]
    base = {"args": [{"name": "data", "kind": "pos", "value": lg_codec.encode_input(b"x")}], "has_self": False}
    got = values_of(inputs._guard_items(sym(data="bytes"), [base], guards))
    assert b"GIF" in got and b"" in got and all(isinstance(v, bytes) for v in got)


# ---- end to end ----

DECODER = '''
BOM = b"\\xef\\xbb\\xbf"


def classify(data: bytes) -> str:
    if data.startswith(BOM):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    if "\\r\\n" in text:
        return "crlf"
    return text.strip() or "empty"


def checksum(buf: bytearray) -> int:
    buf.append(sum(buf) % 256)
    return len(buf)


def first_byte(view: memoryview) -> int:
    return view[0] if len(view) else -1


def encode(text: str) -> bytes:
    return text.encode("utf-8")


def same_buffer(buf: bytearray) -> bytearray:
    return buf


def same_view(view: memoryview) -> memoryview:
    return view
'''


def setup(tmp_path, make_cfg, old_src, new_src):
    root = write_tree(tmp_path / "proj", {"pkg/__init__.py": "", "pkg/codec.py": new_src})
    base = write_tree(tmp_path / "base", {"pkg/__init__.py": "", "pkg/codec.py": old_src})
    cfg = make_cfg(root)
    targets = diff.changed_targets("pkg/codec.py", (base / "pkg/codec.py").read_text(), (root / "pkg/codec.py").read_text())
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_branch_change_for_some_bytes_is_caught(tmp_path, make_cfg):
    new = DECODER.replace('return "crlf"', 'return "lf"')
    r = setup(tmp_path, make_cfg, DECODER, new)["classify"]
    assert r.status == diff.DIFFERENT, r.reason
    first = r.differences[0]
    assert "classify(data=b'" in first["call"] and "\\r\\n" in first["call"]
    assert first["before"] == "'crlf'" and first["after"] == "'lf'"


def test_bytearray_state_and_bytes_return_are_compared(tmp_path, make_cfg):
    new = DECODER.replace("% 256", "% 128").replace('text.encode("utf-8")', 'text.upper().encode("utf-8")')
    results = setup(tmp_path, make_cfg, DECODER, new)
    r = results["checksum"]
    assert r.status == diff.DIFFERENT, r.reason
    assert any("then buf=bytearray(b'" in d["before"] for d in r.differences)
    r = results["encode"]
    assert r.status == diff.DIFFERENT, r.reason
    assert any(d["before"].startswith("b'") and d["after"].startswith("b'") for d in r.differences)


def test_memoryview_parameter_is_checked(tmp_path, make_cfg):
    new = DECODER.replace("view[0] if len(view) else -1", "view[-1] if len(view) else -1")
    r = setup(tmp_path, make_cfg, DECODER, new)["first_byte"]
    assert r.status == diff.DIFFERENT, r.reason
    assert "first_byte(view=memoryview(b'" in r.differences[0]["call"]


def test_behaviour_preserving_bytes_edit_is_verified(tmp_path, make_cfg):
    new = DECODER.replace("data = data[3:]", "data = data[len(BOM):]").replace(
        'return text.strip() or "empty"', 'stripped = text.strip()\n    return stripped if stripped else "empty"')
    r = setup(tmp_path, make_cfg, DECODER, new)["classify"]
    assert r.status == diff.DONE, r.reason


def test_edit_info_keeps_bytes_literals_and_guards():
    from runner import examples
    old = 'def kind(data: bytes) -> str:\n    if data[:3] == b"GIF":\n        return "gif"\n    return "other"\n'
    new = old.replace('return "gif"', 'return b"GIF8".decode()')
    info = examples.edit_info(old, new)
    assert b"GIF8" in info["literals"]
    old2 = 'def kind(data: bytes) -> str:\n    if data == b"GIF":\n        return "gif"\n    return "other"\n'
    info = examples.edit_info(old2, old2.replace('return "gif"', 'return "GIF"'))
    assert {"name": "data", "consts": [b"GIF"]} in info["guards"]


def test_bytearray_and_memoryview_pass_through_is_verified(tmp_path, make_cfg):
    new = DECODER.replace("    return buf\n", "    out = buf\n    return out\n").replace(
        "    return view\n", "    out = view\n    return out\n")
    results = setup(tmp_path, make_cfg, DECODER, new)
    for name in ("same_buffer", "same_view"):
        assert results[name].status == diff.DONE, (name, results[name].reason)
