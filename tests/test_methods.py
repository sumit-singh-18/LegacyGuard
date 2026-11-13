"""Methods on classes that need constructor arguments (roadmap B3)."""
from runner import diff

from conftest import call, write_tree

MONEY = '''
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Currency(Enum):
    USD = "USD"
    EUR = "EUR"


class Money:
    def __init__(self, amount: Decimal, currency: Currency):
        self.amount = amount
        self.currency = currency

    def doubled(self) -> Decimal:
        return self.amount * 2

    def plus(self, cents: int) -> Decimal:
        return self.amount + Decimal(cents) / 100


@dataclass
class Point:
    x: int
    y: int

    def norm1(self) -> int:
        return abs(self.x) + abs(self.y)


class Account:
    def __init__(self, owner, balance):
        self.owner = owner
        self.balance = balance

    def withdraw(self, amount: int) -> int:
        if amount > self.balance:
            raise ValueError("insufficient funds")
        self.balance -= amount
        return self.balance


class Needy:
    def __init__(self, thing):
        self.thing = thing

    def get(self):
        return self.thing
'''

USES = '''
from bank.money import Account

def test_withdraw():
    acct = Account("ann", 100)
    assert acct.withdraw(30) == 70
'''


def run_methods(tmp_path, make_cfg, old_src, new_src):
    files = {"bank/__init__.py": "", "tests/test_bank.py": USES}
    root = write_tree(tmp_path / "proj", {**files, "bank/money.py": new_src})
    base = write_tree(tmp_path / "base", {**files, "bank/money.py": old_src})
    cfg = make_cfg(root)
    targets = diff.changed_targets("bank/money.py", (base / "bank/money.py").read_text(), (root / "bank/money.py").read_text())
    return {r.qualname: r for r in diff.run(cfg, str(base), str(root), targets, harvest={})}


def test_method_on_class_with_typed_init_is_checked(tmp_path, make_cfg):
    new = MONEY.replace("return self.amount * 2", "return self.amount + self.amount")
    assert run_methods(tmp_path, make_cfg, MONEY, new)["Money.doubled"].status == diff.DONE
    broken = MONEY.replace("Decimal(cents) / 100", "Decimal(cents) / 10")
    r = run_methods(tmp_path / "b", make_cfg, MONEY, broken)["Money.plus"]
    assert r.status == diff.DIFFERENT


def test_dataclass_method_is_checked(tmp_path, make_cfg):
    broken = MONEY.replace("abs(self.x) + abs(self.y)", "abs(self.x + self.y)")
    r = run_methods(tmp_path, make_cfg, MONEY, broken)["Point.norm1"]
    assert r.status == diff.DIFFERENT


def test_untyped_init_uses_constructor_call_from_tests(tmp_path, make_cfg):
    # Account("ann", 100) in the tests is the only way to know what Account takes.
    broken = MONEY.replace("if amount > self.balance:", "if amount >= self.balance:")
    r = run_methods(tmp_path, make_cfg, MONEY, broken)["Account.withdraw"]
    assert r.status == diff.DIFFERENT, r.reason
    assert any("100" in d["call"] or "withdraw(amount=100)" in d["call"] for d in r.differences)


def test_unbuildable_class_says_why(tmp_path, make_cfg):
    new = MONEY.replace("return self.thing", "return self.thing or None")
    r = run_methods(tmp_path, make_cfg, MONEY, new)["Needy.get"]
    assert r.status == diff.UNTESTED and "could not build a Needy" in r.reason


def test_worker_reports_recipe(tmp_path, run_worker):
    root = write_tree(tmp_path / "w", {"m.py": MONEY})
    out = run_worker(root, [{"module": "m", "qualname": "Money.doubled", "inputs": [call()]}])["m:Money.doubled"]
    assert out["self_recipe"] == "values made from the __init__ type hints"
    assert out["results"][0]["value"] == {"t": "Decimal", "v": "20.00"}
