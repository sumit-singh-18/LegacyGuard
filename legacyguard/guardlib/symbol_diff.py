"""Compare definitions before and after an edit (FR-12 step 3)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from guardlib import symbols


@dataclass
class Change:
    kind: str  # added, deleted, renamed, visibility_changed, signature_changed, body_changed
    qualname: str
    new_qualname: str | None = None
    before: symbols.Symbol | None = None
    after: symbols.Symbol | None = None


def _without_name(sym: symbols.Symbol) -> str:
    return re.sub(rf"\b{re.escape(sym.name)}\b", "§", sym.node_text, count=1)


def diff(before: str | None, after: str | None) -> list[Change]:
    a = {s.qualname: s for s in symbols.extract(before or "")[0]}
    b = {s.qualname: s for s in symbols.extract(after or "")[0]}
    changes: list[Change] = []
    removed = {q: s for q, s in a.items() if q not in b}
    added = {q: s for q, s in b.items() if q not in a}
    # Same name apart from underscores first: that is a visibility change, not a coincidence of bodies.
    for q, s in list(removed.items()):
        match = next((t for t in added.values() if t.kind == s.kind and t.name.lstrip("_") == s.name.lstrip("_")
                      and _without_name(t) == _without_name(s)), None)
        if match is not None:
            changes.append(Change("visibility_changed", q, match.qualname, s, match))
            del removed[q], added[match.qualname]
    # A rename only when exactly one removed and one added definition share the body; otherwise it is ambiguous.
    for q, s in list(removed.items()):
        candidates = [t for t in added.values() if t.kind == s.kind and _without_name(t) == _without_name(s)]
        rivals = [r for r in removed.values() if r.kind == s.kind and _without_name(r) == _without_name(s)]
        if len(candidates) == 1 and len(rivals) == 1:
            changes.append(Change("renamed", q, candidates[0].qualname, s, candidates[0]))
            del removed[q], added[candidates[0].qualname]
    changes += [Change("deleted", q, before=s) for q, s in removed.items()]
    changes += [Change("added", q, after=s) for q, s in added.items()]
    for q in a.keys() & b.keys():
        s, t = a[q], b[q]
        if s.signature_hash != t.signature_hash:
            changes.append(Change("signature_changed", q, before=s, after=t))
        elif s.body_hash != t.body_hash:
            changes.append(Change("body_changed", q, before=s, after=t))
    return sorted(changes, key=lambda c: c.qualname)
