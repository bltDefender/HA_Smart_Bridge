from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from smart_bridge.rules import parse_rules_text
from smart_bridge.scheduler import AstralSun, FireTimeStore, Scheduler

from .engine_harness import BERLIN, FakeClock, FakeTimers, Harness, make


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=BERLIN)


class Rig:
    """Scheduler mit Fake-Uhr; Trigger- und Fenster-Aufrufe werden protokolliert."""

    def __init__(self, yaml_text: str, start: datetime, db: Path | str = ":memory:") -> None:
        self.rules, errors = parse_rules_text(yaml_text)
        assert not errors, errors
        self.clock = FakeClock(start.astimezone(UTC))  # absolute Zeitarithmetik
        self.timers = FakeTimers(self.clock)
        self.db = db
        self.fired: list[tuple[str, int, datetime, dict[str, Any]]] = []
        self.windows: list[str] = []
        self.report_errors: dict[str, str] = {}
        self.scheduler = self._build()

    def _build(self) -> Scheduler:
        self.store = FireTimeStore(self.db)

        async def on_trigger(rid: str, idx: int, when: datetime, data: dict[str, Any]) -> None:
            self.fired.append((rid, idx, when, data))

        async def on_window(rid: str) -> None:
            self.windows.append(rid)

        sch = Scheduler(
            now=self.clock,
            timers=self.timers,
            zone=BERLIN,
            store=self.store,
            sun=AstralSun(52.4, 13.1, BERLIN),
            on_trigger=on_trigger,
            on_window=on_window,
        )
        self.report_errors = sch.load(self.rules).errors
        return sch

    def restart(self) -> None:
        """Prozessneustart: neue Timer und neuer Scheduler, gleiche Datenbank."""
        self.scheduler.stop()
        self.store.close()
        self.timers = FakeTimers(self.clock)
        self.scheduler = self._build()


def rule(trigger: str, extra: str = "") -> str:
    return f"""
rules:
  - id: r
    triggers:
      - {trigger}
    actions:
      - {{ type: ha_service, service: light.turn_on, target: {{ entity_id: light.a }} }}
{extra}
"""


async def test_fires_at_time_and_rearms() -> None:
    rig = Rig(rule("{ type: time, at: '22:30' }"), at(2026, 1, 5, 12))
    await rig.scheduler.start()
    await rig.timers.advance(10 * 3600 + 29 * 60)
    assert rig.fired == []
    await rig.timers.advance(120)
    assert [f[2] for f in rig.fired] == [at(2026, 1, 5, 22, 30).astimezone(UTC)]
    await rig.timers.advance(24 * 3600)
    assert len(rig.fired) == 2


async def test_long_waits_are_chunked_not_polled() -> None:
    rig = Rig(rule("{ type: time, at: '22:30' }"), at(2026, 1, 5, 12))
    await rig.scheduler.start()
    (due, _), *rest = rig.timers.items.values()
    assert not rest and (due - rig.clock.now) <= timedelta(hours=1)


async def test_dst_spring_gap_fires_once_shifted() -> None:
    rig = Rig(rule("{ type: time, at: '02:30' }"), at(2026, 3, 28, 12))
    await rig.scheduler.start()
    await rig.timers.advance(2 * 86400)
    times = [f[2].astimezone(BERLIN) for f in rig.fired]
    assert [(t.day, t.hour, t.minute) for t in times] == [(29, 3, 30), (30, 2, 30)]


async def test_dst_fall_back_fires_once() -> None:
    rig = Rig(rule("{ type: time, at: '02:30' }"), at(2026, 10, 24, 12))
    await rig.scheduler.start()
    await rig.timers.advance(3 * 86400)
    assert len(rig.fired) == 3  # 25., 26., 27. – nicht doppelt am 25.


async def test_interval_runs_in_absolute_time() -> None:
    rig = Rig(rule("{ type: interval, every: '01:00:00' }"), at(2026, 3, 29, 0, 30))
    await rig.scheduler.start()
    await rig.timers.advance(4 * 3600)
    assert len(rig.fired) == 4  # Umstellung dazwischen ändert nichts


async def test_state_only_rules_get_no_jobs() -> None:
    yaml_text = """
rules:
  - id: r
    alert: { target: x, severity: info }
    condition: { state: { entity_id: light.a, is: "on" } }
"""
    rig = Rig(yaml_text, at(2026, 1, 5, 12))
    await rig.scheduler.start()
    assert rig.timers.items == {}


async def test_window_boundaries_trigger_reevaluation() -> None:
    yaml_text = """
rules:
  - id: night
    alert: { target: x, severity: info }
    active: { after: "22:30", before: "06:00" }
    condition: { state: { entity_id: light.a, is: "on" } }
"""
    rig = Rig(yaml_text, at(2026, 1, 5, 12))
    await rig.scheduler.start()
    await rig.timers.advance(11 * 3600)  # → 23:00
    assert rig.windows == ["night"]
    await rig.timers.advance(7 * 3600)  # → 06:00
    assert rig.windows == ["night", "night"]
    assert rig.fired == []


async def test_workday_condition_reevaluates_at_midnight() -> None:
    yaml_text = """
rules:
  - id: wd
    alert: { target: x, severity: info }
    condition: { workday: true }
"""
    rig = Rig(yaml_text, at(2026, 1, 5, 12))
    await rig.scheduler.start()
    await rig.timers.advance(13 * 3600)
    assert rig.windows == ["wd"]


async def test_sun_window_boundaries_are_scheduled() -> None:
    yaml_text = """
rules:
  - id: dark
    alert: { target: x, severity: info }
    active: { sun: { after: sunset, before: sunrise } }
    condition: { state: { entity_id: light.a, is: "on" } }
"""
    rig = Rig(yaml_text, at(2026, 6, 21, 12))
    await rig.scheduler.start()
    await rig.timers.advance(86400)
    assert rig.windows == ["dark", "dark"]


# --- Missed-Fire & Persistenz ------------------------------------------------


async def _run_missed(tmp_path: Path, policy: str, offset: timedelta) -> Rig:
    rig = Rig(
        rule("{ type: time, at: '10:00'" + policy + " }"), at(2026, 1, 5, 9), tmp_path / "f.db"
    )
    await rig.scheduler.start()
    rig.scheduler.stop()  # Bridge fährt vor 10:00 herunter
    rig.store.close()
    rig.clock.now = (at(2026, 1, 5, 10) + offset).astimezone(UTC)
    rig.timers = FakeTimers(rig.clock)
    rig.scheduler = rig._build()
    await rig.scheduler.start()
    return rig


async def test_missed_fire_none_skips(tmp_path: Path) -> None:
    rig = await _run_missed(tmp_path, "", timedelta(hours=2))
    assert rig.fired == []
    await rig.timers.advance(86400)
    assert len(rig.fired) == 1  # regulär am nächsten Tag


async def test_missed_fire_once_catches_up(tmp_path: Path) -> None:
    rig = await _run_missed(tmp_path, ", catch_up: once", timedelta(hours=20))
    assert len(rig.fired) == 1
    _, _, scheduled, data = rig.fired[0]
    assert scheduled == at(2026, 1, 5, 10).astimezone(UTC)
    assert data["catch_up"] is True
    await rig.timers.advance(86400)
    assert len(rig.fired) == 2  # Nachholen verdoppelt den nächsten Lauf nicht


async def test_missed_fire_within_window(tmp_path: Path) -> None:
    policy = ", catch_up: within, catch_up_within: '00:30:00'"
    assert len((await _run_missed(tmp_path, policy, timedelta(minutes=20))).fired) == 1
    other = tmp_path / "late"
    other.mkdir()
    assert (await _run_missed(other, policy, timedelta(minutes=45))).fired == []


async def test_first_run_has_nothing_to_catch_up() -> None:
    rig = Rig(rule("{ type: time, at: '10:00', catch_up: once }"), at(2026, 1, 5, 12))
    await rig.scheduler.start()
    assert rig.fired == []


async def test_changed_trigger_discards_persisted_time(tmp_path: Path) -> None:
    rig = await _run_missed(tmp_path, "", timedelta(hours=0))
    rig.scheduler.stop()
    rig.rules, _ = parse_rules_text(rule("{ type: time, at: '11:00', catch_up: once }"))
    rig.clock.now = at(2026, 1, 5, 12).astimezone(UTC)
    rig.scheduler = rig._build()
    await rig.scheduler.start()
    assert rig.fired == []  # neue Definition → keine Nachholung der alten Zeit


async def test_persisted_fire_times_survive_restart(tmp_path: Path) -> None:
    rig = Rig(rule("{ type: time, at: '22:30' }"), at(2026, 1, 5, 12), tmp_path / "f.db")
    await rig.scheduler.start()
    expected = rig.scheduler.next_fires[("r", 0)]
    rig.restart()
    await rig.scheduler.start()
    assert rig.scheduler.next_fires[("r", 0)] == expected
    rec = rig.store.get("r", 0)
    assert rec is not None and rec.next_fire == expected


async def test_interval_phase_survives_restart(tmp_path: Path) -> None:
    rig = Rig(
        rule("{ type: interval, every: '00:10:00' }"), at(2026, 1, 5, 12, 0), tmp_path / "f.db"
    )
    await rig.scheduler.start()  # nächster Takt 12:10
    rig.scheduler.stop()
    rig.store.close()
    rig.clock.now = at(2026, 1, 5, 12, 4).astimezone(UTC)
    rig.timers = FakeTimers(rig.clock)
    rig.scheduler = rig._build()
    await rig.scheduler.start()
    assert rig.scheduler.next_fires[("r", 0)] == at(2026, 1, 5, 12, 10).astimezone(UTC)


async def test_removed_rule_is_pruned(tmp_path: Path) -> None:
    rig = Rig(rule("{ type: time, at: '22:30' }"), at(2026, 1, 5, 12), tmp_path / "f.db")
    await rig.scheduler.start()
    rig.rules = []
    rig.restart()
    assert rig.store.get("r", 0) is None


async def test_unschedulable_rule_is_reported() -> None:
    rig = Rig(rule("{ type: time, at: '22:30' }"), at(2026, 1, 5, 12))
    assert rig.report_errors == {}
    sch = Scheduler(
        now=rig.clock,
        timers=rig.timers,
        zone=BERLIN,
        store=FireTimeStore(),
        on_trigger=lambda *_: _noop(),
        on_window=lambda _: _noop(),
    )
    report = sch.load(parse_rules_text(rule("{ type: sun, event: sunset }"))[0])
    assert report.errors.keys() == {"r"}


async def _noop() -> None:
    return None


async def test_handler_errors_do_not_stop_rescheduling() -> None:
    rig = Rig(rule("{ type: interval, every: '00:10:00' }"), at(2026, 1, 5, 12))
    calls = 0

    async def boom(*_: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("x")

    rig.scheduler._on_trigger = boom  # noqa: SLF001
    await rig.scheduler.start()
    await rig.timers.advance(3600)
    assert calls == 6


# --- Engine-Integration -------------------------------------------------


async def test_engine_runs_actions_for_time_trigger() -> None:
    yaml_text = """
rules:
  - id: nachts
    triggers: [ { type: time, at: "22:30" } ]
    condition: { state: { entity_id: lock.x, is: unlocked } }
    actions:
      - { type: ha_service, service: lock.lock, target: { entity_id: lock.x } }
"""
    h: Harness = await make(yaml_text, {"lock.x": "unlocked"}, start=at(2026, 1, 5, 12))
    await h.engine.on_time_trigger("nachts", 0, h.clock.now)
    assert h.kinds() == ["ActionsRequested:actions:nachts"]
    h.out.clear()
    await h.set("lock.x", "locked")
    await h.engine.on_time_trigger("nachts", 0, h.clock.now)
    assert h.out == []  # Condition falsch → keine Aktion


async def test_engine_ignores_time_trigger_before_start() -> None:
    yaml_text = """
rules:
  - id: r
    triggers: [ { type: time, at: "22:30" } ]
    actions:
      - { type: ha_service, service: light.turn_on, target: { entity_id: light.a } }
"""
    h = await make(yaml_text, start_engine=False)
    await h.engine.on_time_trigger("r", 0, h.clock.now)
    assert h.out == []
