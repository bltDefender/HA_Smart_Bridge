"""Trigger-Index: entity_id | event | topic | webhook -> Einträge. Lookup statt Vollauswertung."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from ..rules.models import (
    EventTrigger,
    MqttTrigger,
    NumericStateTrigger,
    Rule,
    StartupTrigger,
    StateTrigger,
    WebhookTrigger,
)
from .triggers import has_wildcard, topic_matches

Role = Literal["trigger", "derived", "level"]


@dataclass(frozen=True)
class Entry:
    rule_id: str
    role: Role
    index: int | None = None  # Position in rule.triggers (nur bei role="trigger")


def has_level(rule: Rule) -> bool:
    return bool(rule.alert or rule.on_activate or rule.on_clear)


class TriggerIndex:
    def __init__(self) -> None:
        self.entity: dict[str, list[Entry]] = defaultdict(list)
        self.event: dict[str, list[Entry]] = defaultdict(list)
        self.webhook: dict[str, list[Entry]] = defaultdict(list)
        self.startup: list[Entry] = []
        self._mqtt_exact: dict[str, list[Entry]] = defaultdict(list)
        self._mqtt_wild: list[tuple[str, Entry]] = []

    def mqtt(self, topic: str) -> list[tuple[str, Entry]]:
        """Passende (Filter, Eintrag)-Paare; exakte Topics per O(1)-Lookup."""
        hits = [(topic, e) for e in self._mqtt_exact.get(topic, [])]
        hits += [(f, e) for f, e in self._mqtt_wild if topic_matches(f, topic)]
        return hits

    def mqtt_filters(self) -> list[str]:
        return sorted({*self._mqtt_exact, *(f for f, _ in self._mqtt_wild)})

    def add(self, rule: Rule) -> None:
        cond_entities = sorted(rule.condition.entity_ids()) if rule.condition else []
        if not rule.triggers:
            for eid in cond_entities:
                self.entity[eid].append(Entry(rule.id, "derived"))
            return
        for i, trig in enumerate(rule.triggers):
            entry = Entry(rule.id, "trigger", i)
            if isinstance(trig, StateTrigger | NumericStateTrigger):
                self.entity[trig.entity_id].append(entry)
            elif isinstance(trig, EventTrigger):
                self.event[trig.event_type].append(entry)
            elif isinstance(trig, MqttTrigger):
                if has_wildcard(trig.topic):
                    self._mqtt_wild.append((trig.topic, entry))
                else:
                    self._mqtt_exact[trig.topic].append(entry)
            elif isinstance(trig, WebhookTrigger):
                self.webhook[trig.webhook_id].append(entry)
            elif isinstance(trig, StartupTrigger):
                self.startup.append(entry)
            # Zeit-Trigger gehören dem Scheduler (M4)
        if has_level(rule):
            for eid in cond_entities:
                self.entity[eid].append(Entry(rule.id, "level"))

    def rules_for_entity(self, entity_id: str) -> set[str]:
        return {e.rule_id for e in self.entity.get(entity_id, [])}


def build_index(rules: list[Rule]) -> TriggerIndex:
    idx = TriggerIndex()
    for rule in rules:
        idx.add(rule)
    return idx
