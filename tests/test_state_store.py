from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

import pytest

from smart_bridge.state import EntityState, StateChange, StateStore

T1 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)


def _collect(store: StateStore) -> list[StateChange]:
    seen: list[StateChange] = []

    async def listener(c: StateChange) -> None:
        seen.append(c)

    store.subscribe(listener)
    return seen


async def test_update_emits_change_and_keeps_last_changed_on_attribute_only() -> None:
    store = StateStore()
    seen = _collect(store)
    await store.update("light.a", "on", {"b": 1}, T1)
    await store.update("light.a", "on", {"b": 2}, T2)
    assert [c.state_changed for c in seen] == [True, False]
    assert store.get("light.a") is not None
    assert store.get("light.a").last_changed == T1  # type: ignore[union-attr]
    assert await store.update("light.a", "on", {"b": 2}, T2) is None


async def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        await StateStore().update("a.b", "on", None, datetime(2026, 1, 1))


async def test_stale_roundtrip_is_not_a_change() -> None:
    store = StateStore()
    seen = _collect(store)
    await store.update("a.b", "on", None, T1)
    store.mark_all_stale()
    assert store.get("a.b").stale  # type: ignore[union-attr]
    changes = await store.replace_all([EntityState("a.b", "on", {}, T2)])
    assert changes == [] and len(seen) == 1
    st = store.get("a.b")
    assert st is not None and not st.stale and st.last_changed == T1


async def test_replace_all_reports_diffs_and_removes_missing() -> None:
    store = StateStore()
    await store.update("a.keep", "on", None, T1)
    await store.update("a.gone", "on", None, T1)
    changes = await store.replace_all(
        [EntityState("a.keep", "off", {}, T2), EntityState("a.new", "x", {}, T2)]
    )
    assert {c.entity_id for c in changes} == {"a.keep", "a.new"}
    assert store.get("a.gone") is None


async def test_snapshot_survives_restart_as_stale(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    first = StateStore(db)
    await first.update("a.b", "on", {"x": [1]}, T1)
    first.close()
    second = StateStore(db)
    st = second.get("a.b")
    assert st is not None and st.stale and st.attributes == {"x": [1]} and st.last_changed == T1


async def test_bulk_sync_load_keeps_state_and_stale_invariant() -> None:
    store = StateStore()
    seen = _collect(store)
    states = [EntityState(f"sensor.load_{i}", str(i), {"index": i}, T1) for i in range(2_000)]
    started = perf_counter()
    changes = await store.replace_all(states)
    elapsed = perf_counter() - started

    assert len(changes) == len(seen) == 2_000
    assert len(store.all()) == 2_000
    store.mark_all_stale()
    assert sum(state.stale for state in store.all().values()) == 2_000
    assert elapsed < 10  # großzügiges CI-Limit für einen lokalen SQLite-Vollsync
