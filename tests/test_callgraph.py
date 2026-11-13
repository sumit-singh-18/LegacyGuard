"""Callers resolved through imports, not matched by name (roadmap B7)."""
from guardlib import callgraph, engine
from runner import diff

from conftest import write_tree

FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": """
def fee(x):
    return x * 2


class Cart:
    def subtotal(self):
        return 10

    def total(self):
        return self.subtotal() + fee(1)
""",
    "pkg/api.py": "from pkg.core import fee as charge\n\ndef checkout(x):\n    return charge(x)\n",
    "pkg/reports.py": "import pkg.core as c\n\ndef summary():\n    return c.fee(2)\n",
    "pkg/rel.py": "from .core import fee\n\ndef rel_user():\n    return fee(3)\n",
    "pkg/fake.py": "def fee(x):\n    return 0\n\ndef uses_fake():\n    return fee(1)\n",
    "pkg/second.py": "from pkg.api import checkout\n\ndef top():\n    return checkout(5)\n",
    "pkg/nested.py": "from pkg.core import fee\n\ndef outer():\n    def inner():\n        return fee(1)\n    return inner()\n",
    "pkg/klass.py": "from pkg.core import Cart\n\ndef make_total():\n    return Cart.subtotal(Cart())\n",
}


def edges(root, file, qual, hops=1):
    return {(e.file, e.qualname, e.how, e.hops) for e in callgraph.callers(str(root), file, qual, None, hops=hops)}


def test_function_callers_resolved(tmp_path):
    root = write_tree(tmp_path / "p", FILES)
    found = {(f, q) for f, q, how, _ in edges(root, "pkg/core.py", "fee") if how == "resolved"}
    assert found == {("pkg/core.py", "Cart.total"), ("pkg/api.py", "checkout"), ("pkg/reports.py", "summary"),
                     ("pkg/rel.py", "rel_user"), ("pkg/nested.py", "outer")}
    assert not any(f == "pkg/fake.py" for f, _ in found)  # a different function that happens to be called fee


def test_method_callers(tmp_path):
    root = write_tree(tmp_path / "p", FILES)
    found = {(f, q) for f, q, _, _ in edges(root, "pkg/core.py", "Cart.subtotal")}
    assert found == {("pkg/core.py", "Cart.total"), ("pkg/klass.py", "make_total")}


def test_second_hop(tmp_path):
    root = write_tree(tmp_path / "p", FILES)
    found = edges(root, "pkg/core.py", "fee", hops=2)
    assert ("pkg/second.py", "top", "resolved", 2) in found


def test_engine_runs_resolved_callers(tmp_path, make_cfg):
    new_core = FILES["pkg/core.py"].replace("return x * 2", "return x * 3")
    root = write_tree(tmp_path / "proj", {**FILES, "pkg/core.py": new_core})
    cfg = make_cfg(root)
    base = write_tree(cfg.state_dir / "baseline", FILES)
    changed = diff.changed_targets("pkg/core.py", (base / "pkg/core.py").read_text(), (root / "pkg/core.py").read_text())
    deps = engine.dependents(cfg, "pkg/core.py", changed, {t.qualname for t in changed})
    by_name = {(d.file, d.qualname): d for d in deps}
    assert ("pkg/api.py", "checkout") in by_name and ("pkg/fake.py", "uses_fake") not in by_name
    assert by_name[("pkg/api.py", "checkout")].via == "calls fee"
    results = {r.qualname: r for r in diff.run(cfg, str(base), str(root), changed + deps, harvest={})}
    assert results["fee"].status == diff.DIFFERENT
    assert results["summary"].status == diff.DIFFERENT  # summary() takes no input, so only the caller shows c.fee(2) changed
