"""Pydantic-Modell für Regeln (siehe docs/architecture.md)."""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Any, Literal

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Duration = Annotated[str, StringConstraints(pattern=r"^-?\d{1,2}:\d{2}:\d{2}$")]
Clock = Annotated[str, StringConstraints(pattern=r"^\d{1,2}:\d{2}(:\d{2})?$")]
RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _check_clock(value: str | None) -> None:
    if value is None:
        return
    m = _CLOCK_RE.match(value)
    if not m or int(m[1]) > 23 or int(m[2]) > 59 or int(m[3] or 0) > 59:
        raise ValueError(f"invalid time of day: {value!r}")


# --- Conditions -------------------------------------------------------------


class StateCond(_Strict):
    entity_id: str
    is_: str | list[str] = Field(alias="is")


class NumericStateCond(_Strict):
    entity_id: str
    above: float | None = None
    below: float | None = None
    hysteresis: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _bounds(self) -> NumericStateCond:
        if self.above is None and self.below is None:
            raise ValueError("numeric_state needs 'above' and/or 'below'")
        return self


class TemplateCond(_Strict):
    value_template: str


class SunWindow(_Strict):
    after: Literal["sunrise", "sunset"] | None = None
    before: Literal["sunrise", "sunset"] | None = None
    after_offset: Duration | None = None
    before_offset: Duration | None = None


class TimeCond(_Strict):
    after: Clock | None = None
    before: Clock | None = None
    weekdays: list[Weekday] | None = None

    @model_validator(mode="after")
    def _clocks(self) -> TimeCond:
        _check_clock(self.after)
        _check_clock(self.before)
        return self


class Condition(_Strict):
    """Genau ein Schlüssel ist gesetzt (wie bei HA)."""

    state: StateCond | None = None
    numeric_state: NumericStateCond | None = None
    template: TemplateCond | None = None
    time: TimeCond | None = None
    sun: SunWindow | None = None
    workday: bool | None = None
    and_: list[Condition] | None = Field(default=None, alias="and")
    or_: list[Condition] | None = Field(default=None, alias="or")
    not_: Condition | None = Field(default=None, alias="not")

    @model_validator(mode="after")
    def _exactly_one(self) -> Condition:
        set_keys = [k for k, v in self.__dict__.items() if v is not None]
        if len(set_keys) != 1:
            raise ValueError(f"condition needs exactly one key, got {set_keys or 'none'}")
        for group in (self.and_, self.or_):
            if group is not None and not group:
                raise ValueError("and/or must not be empty")
        return self

    def entity_ids(self) -> set[str]:
        ids: set[str] = set()
        for cond in (self.state, self.numeric_state):
            if cond is not None:
                ids.add(cond.entity_id)
        for group in (self.and_, self.or_):
            for child in group or []:
                ids |= child.entity_ids()
        if self.not_ is not None:
            ids |= self.not_.entity_ids()
        return ids


# --- Triggers ---------------------------------------------------------------


class StateTrigger(_Strict):
    type: Literal["state"]
    entity_id: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    for_: Duration | None = Field(default=None, alias="for")


class NumericStateTrigger(_Strict):
    type: Literal["numeric_state"]
    entity_id: str
    above: float | None = None
    below: float | None = None
    hysteresis: float | None = Field(default=None, ge=0)
    for_: Duration | None = Field(default=None, alias="for")

    @model_validator(mode="after")
    def _bounds(self) -> NumericStateTrigger:
        if self.above is None and self.below is None:
            raise ValueError("numeric_state trigger needs 'above' and/or 'below'")
        return self


class EventTrigger(_Strict):
    type: Literal["event"]
    event_type: str
    event_data: dict[str, Any] = Field(default_factory=dict)


class MqttTrigger(_Strict):
    type: Literal["mqtt"]
    topic: str
    payload: str | None = None


class WebhookTrigger(_Strict):
    type: Literal["webhook"]
    webhook_id: str


class StartupTrigger(_Strict):
    type: Literal["startup"]


class _MissedFire(_Strict):
    # none | once | within:"HH:MM:SS" — Letzteres als catch_up_within
    catch_up: Literal["none", "once", "within"] = "none"
    catch_up_within: Duration | None = None

    @model_validator(mode="after")
    def _within(self) -> _MissedFire:
        if (self.catch_up == "within") != (self.catch_up_within is not None):
            raise ValueError("catch_up_within is required exactly when catch_up is 'within'")
        return self


class TimeTrigger(_MissedFire):
    type: Literal["time"]
    at: Clock
    weekdays: list[Weekday] | None = None

    @model_validator(mode="after")
    def _at(self) -> TimeTrigger:
        _check_clock(self.at)
        return self


_PATTERN_RE = re.compile(r"^(\*|\d{1,2}|/\d{1,2}|\*/\d{1,2})$")
_PATTERN_MAX = {"hours": 23, "minutes": 59, "seconds": 59}
_FULL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_DAY = re.compile(r"^\d{2}-\d{2}$")


class TimePatternTrigger(_MissedFire):
    type: Literal["time_pattern"]
    hours: str | None = None
    minutes: str | None = None
    seconds: str | None = None

    @model_validator(mode="after")
    def _fields(self) -> TimePatternTrigger:
        given = {k: getattr(self, k) for k in _PATTERN_MAX if getattr(self, k) is not None}
        if not given:
            raise ValueError("time_pattern needs hours, minutes and/or seconds")
        for name, value in given.items():
            if not _PATTERN_RE.match(value):
                raise ValueError(f"invalid {name} pattern: {value!r}")
            digits = value.lstrip("*/")
            if digits:
                n = int(digits)
                too_big = n > _PATTERN_MAX[name]
                if too_big or ("/" in value and n == 0):
                    raise ValueError(f"{name} out of range: {value!r}")
        return self


class CronTrigger(_MissedFire):
    type: Literal["cron"]
    expression: str

    @model_validator(mode="after")
    def _valid(self) -> CronTrigger:
        if len(self.expression.split()) not in (5, 6) or not croniter.is_valid(self.expression):
            raise ValueError(f"invalid cron expression: {self.expression!r}")
        return self


class IntervalTrigger(_MissedFire):
    type: Literal["interval"]
    every: Duration

    @model_validator(mode="after")
    def _positive(self) -> IntervalTrigger:
        if self.every.startswith("-") or self.every == "00:00:00":
            raise ValueError("interval must be positive")
        return self


class DateTrigger(_MissedFire):
    """Einmalig (`on: YYYY-MM-DD`), jährlich (`yearly`, `on: MM-DD`) oder Bereich (`from`/`to`)."""

    type: Literal["date"]
    on: str | None = None
    yearly: bool = False
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    at: Clock = "00:00"

    @model_validator(mode="after")
    def _shape(self) -> DateTrigger:
        _check_clock(self.at)
        ranged = self.from_ is not None or self.to is not None
        if (self.on is None) == (not ranged):
            raise ValueError("date needs either 'on' or 'from'+'to'")
        if ranged and (self.from_ is None or self.to is None):
            raise ValueError("date range needs both 'from' and 'to'")
        values = [v for v in (self.on, self.from_, self.to) if v is not None]
        for v in values:
            allowed = (_MONTH_DAY, _FULL_DATE) if self.yearly else (_FULL_DATE,)
            if not any(r.match(v) for r in allowed):
                raise ValueError(f"invalid date {v!r} (yearly: MM-DD, otherwise YYYY-MM-DD)")
            probe = v if len(v) == 10 else f"2024-{v}"  # 2024: Schaltjahr
            try:
                datetime.date.fromisoformat(probe)
            except ValueError as exc:
                raise ValueError(f"invalid date {v!r}") from exc
        if ranged and not self.yearly and self.from_ and self.to and self.from_ > self.to:
            raise ValueError("date range: 'from' must not be after 'to'")
        return self


class SunTrigger(_MissedFire):
    type: Literal["sun"]
    event: Literal[
        "sunrise",
        "sunset",
        "civil_dawn",
        "civil_dusk",
        "nautical_dawn",
        "nautical_dusk",
        "astronomical_dawn",
        "astronomical_dusk",
    ]
    offset: Duration = "00:00:00"


Trigger = Annotated[
    StateTrigger
    | NumericStateTrigger
    | EventTrigger
    | MqttTrigger
    | WebhookTrigger
    | StartupTrigger
    | TimeTrigger
    | TimePatternTrigger
    | CronTrigger
    | IntervalTrigger
    | DateTrigger
    | SunTrigger,
    Field(discriminator="type"),
]


# --- Actions ----------------------------------------------------------------


class Action(BaseModel):
    """Typ wird in M1 geprüft, Parameter je Typ folgen mit dem Executor (M6)."""

    model_config = ConfigDict(extra="allow")

    type: Literal[
        "ha_tool",
        "ha_service",
        "mqtt_publish",
        "webhook",
        "notify",
        "delay",
        "wait_for",
        "choose",
        "if",
    ]
    timeout: Duration | None = None
    retries: int | None = Field(default=None, ge=0, le=10)


# --- Rule -------------------------------------------------------------------


class ActiveWindow(_Strict):
    after: Clock | None = None
    before: Clock | None = None
    weekdays: list[Weekday] | None = None
    sun: SunWindow | None = None

    @model_validator(mode="after")
    def _clocks(self) -> ActiveWindow:
        _check_clock(self.after)
        _check_clock(self.before)
        return self


class AlertSpec(_Strict):
    target: str
    severity: Literal["critical", "warning", "info"]
    title: str | None = None
    message: str | None = None
    blink: bool | None = None
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")] | None = None
    zoom: bool | None = None


class Rule(_Strict):
    id: RuleId
    title: str | None = None
    intent: str | None = None
    triggers: list[Trigger] | None = None
    condition: Condition | None = None
    for_: Duration | None = Field(default=None, alias="for")
    active: ActiveWindow | None = None
    per_entity: list[str] | None = None
    mode: Literal["single", "restart", "queued", "parallel"] = "single"
    alert: AlertSpec | None = None
    actions: list[Action] | None = None
    on_activate: list[Action] | None = None
    on_clear: list[Action] | None = None
    repeat_after: Duration | None = None
    on_unavailable: Literal["ignore", "deviation"] = "ignore"
    debounce: Duration | None = None
    max_runs_per_hour: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    created_by: Literal["claude", "manual", "file"] = "manual"
    version: int = Field(default=1, ge=1)
    locked: bool = False
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    status: Literal["active", "shadow", "disabled", "error"] = "active"
    # `tier` wird bewusst nicht modelliert: berechnet, in YAML verboten (extra=forbid).

    @model_validator(mode="after")
    def _shape(self) -> Rule:
        if not (self.alert or self.actions or self.on_activate or self.on_clear):
            raise ValueError("rule needs 'alert' and/or actions")
        if not (self.triggers or self.condition):
            raise ValueError("rule needs 'triggers' and/or 'condition'")
        if self.for_ and not self.condition:
            raise ValueError("'for' requires a condition")
        return self


class RulesFile(_Strict):
    rules: list[Rule]

    @model_validator(mode="after")
    def _unique_ids(self) -> RulesFile:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen.add(rule.id)
        return self
