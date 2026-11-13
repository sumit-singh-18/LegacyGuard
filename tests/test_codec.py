import datetime as dt
from decimal import Decimal

from runner import lg_codec

VALUES = [None, True, 3, 2**60, 1.5, "x", Decimal("2.675"), dt.date(2024, 1, 2), dt.datetime(2024, 1, 2, 3, 4),
          b"\x00\x01", [1, [2, 3]], (1, "a"), {"k": [1]}, {1, 2}, frozenset({3})]


def test_roundtrip():
    for v in VALUES:
        assert lg_codec.decode(lg_codec.encode_input(v)) == v


def test_canon_is_order_independent():
    assert lg_codec.canon({"a": 1, "b": 2}) == lg_codec.canon({"b": 2, "a": 1})
    assert lg_codec.canon({3, 1, 2}) == lg_codec.canon({1, 2, 3})


def test_canon_scrubs_addresses():
    class Thing:
        pass
    a, b = repr(object()), repr(object())
    assert lg_codec.canon(a) == lg_codec.canon(b)


def test_canon_handles_cycles():
    xs = [1]
    xs.append(xs)
    assert lg_codec.canon(xs)["v"][1] == {"t": "cycle"}


def test_display():
    assert lg_codec.display(lg_codec.canon([5, 6, 7])) == "[5, 6, 7]"
