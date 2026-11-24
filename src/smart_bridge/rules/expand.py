"""`per_entity`: eine konkrete Regel je Entity (`id` mit Suffix), danach wie jede andere Regel."""

from __future__ import annotations

import re

from ..templating import bind_entity
from .models import Rule

_NON_ID = re.compile(r"[^a-z0-9_]")


def expand_per_entity(rule: Rule) -> list[Rule]:
    if not rule.per_entity:
        return [rule]
    raw = rule.model_dump(by_alias=True, exclude_unset=True, mode="json")
    raw.pop("per_entity")
    expanded: list[Rule] = []
    seen: set[str] = set()
    for entity_id in rule.per_entity:
        suffix = _NON_ID.sub("_", entity_id.lower())
        if suffix in seen:
            continue
        seen.add(suffix)
        data = bind_entity(raw, entity_id)
        data["id"] = f"{rule.id}__{suffix}"
        child = Rule.model_validate(data)
        child._bindings = {"entity_id": entity_id}  # noqa: SLF001
        child._parent = rule.id  # noqa: SLF001
        expanded.append(child)
    return expanded
