"""Compare requirements between two versions of a regulation (pure functions)."""

from regpilot.core.models import Requirement


def _limits_key(r: Requirement) -> dict[tuple, str]:
    return {tuple(lr.conditions): lr.raw_value for lr in r.limits}


def describe_change(old: Requirement, new: Requirement) -> list[str]:
    """Human-readable list of what changed in one requirement."""
    notes = []
    o, n = _limits_key(old), _limits_key(new)
    for cond in n.keys() - o.keys():
        notes.append(f"limit row added: {' / '.join(cond) or '(all)'} {new.comparator} {n[cond]}")
    for cond in o.keys() - n.keys():
        notes.append(f"limit row removed: {' / '.join(cond) or '(all)'} {old.comparator} {o[cond]}")
    for cond in o.keys() & n.keys():
        if o[cond] != n[cond]:
            notes.append(f"limit changed: {' / '.join(cond) or '(all)'} {o[cond]} -> {n[cond]}")
    oa = {(a.delta, a.unit, a.applies_to) for a in old.adjustments}
    na = {(a.delta, a.unit, a.applies_to) for a in new.adjustments}
    if oa != na:
        notes.append(f"adjustments changed: {sorted(oa)} -> {sorted(na)}")
    od = {(str(a.date), a.scope) for a in old.applicability}
    nd = {(str(a.date), a.scope) for a in new.applicability}
    if od != nd:
        notes.append(f"applicability dates changed: {sorted(od)} -> {sorted(nd)}")
    if old.conditions != new.conditions:
        notes.append(f"clause conditions changed: {old.conditions} -> {new.conditions}")
    return notes or ["content fingerprint changed"]


def diff(old: list[Requirement], new: list[Requirement]) -> dict:
    o = {r.id: r for r in old}
    n = {r.id: r for r in new}
    changed = {i: describe_change(o[i], n[i]) for i in sorted(o.keys() & n.keys())
               if o[i].fingerprint() != n[i].fingerprint()}
    return {
        "added": sorted(n.keys() - o.keys()),
        "removed": sorted(o.keys() - n.keys()),
        "changed": changed,
        "unchanged": sorted(i for i in o.keys() & n.keys() if i not in changed),
    }
