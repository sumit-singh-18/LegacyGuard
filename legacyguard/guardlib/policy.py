"""Decision policy (ARCHITECTURE §6, D-013, D-018, D-034). Pure logic so the fixture table can test it directly."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Decision:
    action: str          # allow, ask, deny
    prompt_used: bool = False
    over_budget: bool = False
    include_context: bool = True


def contract_decision(mode: str, prompts_used: int, budget: int, waived: bool) -> Decision:
    """A contract break: advisory never blocks; guarded asks within budget; strict denies until waived."""
    if mode == "advisory":
        return Decision("allow")
    if mode == "strict":
        return Decision("allow") if waived else Decision("deny")
    if waived:
        return Decision("allow", include_context=False)
    if prompts_used < budget:
        return Decision("ask", prompt_used=True)
    return Decision("allow", over_budget=True)
