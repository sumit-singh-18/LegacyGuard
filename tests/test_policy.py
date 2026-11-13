from guardlib.policy import contract_decision


def test_advisory_never_blocks():
    assert contract_decision("advisory", 0, 3, False).action == "allow"


def test_guarded_asks_within_budget_then_allows():
    d = contract_decision("guarded", 0, 3, False)
    assert d.action == "ask" and d.prompt_used
    over = contract_decision("guarded", 3, 3, False)
    assert over.action == "allow" and over.over_budget


def test_guarded_waived_is_silent():
    d = contract_decision("guarded", 0, 3, True)
    assert d.action == "allow" and not d.include_context


def test_strict_denies_until_waived():
    assert contract_decision("strict", 0, 3, False).action == "deny"
    assert contract_decision("strict", 0, 3, True).action == "allow"
