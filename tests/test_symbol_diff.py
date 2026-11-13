from guardlib import symbol_diff

BEFORE = '''
def total(xs):
    return sum(xs)

def _helper(a):
    return a * 2

class Cart:
    def add(self, x):
        self.items.append(x)
'''


def kinds(before, after):
    return {(c.kind, c.qualname) for c in symbol_diff.diff(before, after)}


def test_body_change_detected():
    after = BEFORE.replace("return sum(xs)", "return sum(xs) + 1")
    assert kinds(BEFORE, after) == {("body_changed", "total")}


def test_signature_change_detected():
    after = BEFORE.replace("def total(xs):", "def total(xs, start=0):")
    assert ("signature_changed", "total") in kinds(BEFORE, after)


def test_rename_and_visibility():
    after = BEFORE.replace("def total(", "def grand_total(").replace("def _helper(", "def helper(")
    found = kinds(BEFORE, after)
    assert ("renamed", "total") in found
    assert ("visibility_changed", "_helper") in found


def test_method_qualname():
    after = BEFORE.replace("self.items.append(x)", "self.items.insert(0, x)")
    # The enclosing class counts as changed too; changed_targets skips classes and runs the method.
    assert kinds(BEFORE, after) == {("body_changed", "Cart.add"), ("body_changed", "Cart")}


def test_no_change_no_diff():
    assert symbol_diff.diff(BEFORE, BEFORE) == []
