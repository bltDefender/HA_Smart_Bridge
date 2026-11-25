from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from smart_bridge.approvals import ApprovalError, ApprovalStore, rule_hash
from smart_bridge.audit import AuditLog
from smart_bridge.authoring import (
    AuthoringError,
    CircuitBreaker,
    Pipeline,
    RuleManager,
    RuleRepository,
)
from smart_bridge.authoring.pipeline import find_cycles
from smart_bridge.cli import main as cli_main
from smart_bridge.mcp_server.service import ToolError
from smart_bridge.mcp_server.writes import WriteService
from smart_bridge.policy import Policy
from smart_bridge.rules import Rule, parse_rules_text

ENTITIES = {"sensor.bat", "sensor.x", "sensor.y", "light.a", "light.b", "lock.front"}


def rule_yaml(
    rid: str = "lamp",
    service: str = "light.turn_on",
    entity: str = "light.a",
    trigger: str = "sensor.x",
    intent: str = "Mach das Licht an",
    extra: str = "",
) -> str:
    return f"""rules:
- id: {rid}
  intent: "{intent}"
  triggers: [{{type: state, entity_id: {trigger}, to: "on"}}]
  actions: [{{type: ha_service, service: {service}, target: {{entity_id: {entity}}}}}]
{extra}"""


ALERT_ONLY = """rules:
- id: batt
  intent: "Warne bei leerer Batterie"
  alert: {target: battery, severity: info}
  condition: {numeric_state: {entity_id: sensor.bat, below: 10}}
"""
LOCK_RULE = rule_yaml("door", "lock.lock", "lock.front", "sensor.y", "Verriegle die Tür")


def parse(text: str) -> Rule:
    rules, errors = parse_rules_text(text)
    assert not errors, errors
    return rules[0]


@dataclass
class Env:
    manager: RuleManager
    approvals: ApprovalStore
    breaker: CircuitBreaker
    audit: AuditLog
    rules_dir: Path
    config_dir: Path
    data_dir: Path
    clock: list[datetime]
    applied: list[Rule] = field(default_factory=list)
    alerts: dict[str, tuple[str, str]] = field(default_factory=dict)
    fires: list[float] = field(default_factory=lambda: [0.0])
    notified: list[str] = field(default_factory=list)

    def status(self, rid: str) -> str:
        return next(r.status for r in self.applied if r.id == rid)

    def audit_rows(self) -> list[tuple[str, str, str | None, str]]:
        db = sqlite3.connect(self.data_dir / "bridge.db")
        return db.execute("SELECT actor, action, subject, result FROM audit_log").fetchall()


def build(tmp_path: Path, policy: Policy | None = None, max_per_hour: int = 2) -> Env:
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    rules_dir = config_dir / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(exist_ok=True)
    clock = [datetime(2026, 1, 1, 12, tzinfo=UTC)]

    def now() -> datetime:
        return clock[0]

    db = data_dir / "bridge.db"
    audit = AuditLog(db, now)
    approvals = ApprovalStore(config_dir / "approvals", db, b"secret", audit, now)
    breaker = CircuitBreaker(db, now, max_per_hour)
    env = Env(None, approvals, breaker, audit, rules_dir, config_dir, data_dir, clock)  # type: ignore[arg-type]

    async def backtest(text: str, days: int) -> dict[str, Any]:
        rules, _ = parse_rules_text(text)
        return {
            "valid": True,
            "days": days,
            "rules": {
                r.id: {"activation_count": 0, "fire_count": 0, "fires_per_day": env.fires[0]}
                for r in rules
            },
        }

    async def suggest(entity_id: str) -> list[str]:
        return ["sensor.x"]

    pipeline = Pipeline(
        policy or Policy(),
        entity_exists=lambda e: e in ENTITIES,
        suggest=suggest,
        backtest=backtest,
    )

    async def apply(rules: list[Rule], errors: dict[str, str]) -> None:
        env.applied = rules

    async def set_alert(key: str, title: str, message: str, severity: str) -> None:
        env.alerts[key] = (title, message)

    async def clear_alert(key: str) -> None:
        env.alerts.pop(key, None)

    async def notify(rule: Rule) -> None:
        env.notified.append(rule.id)

    env.manager = RuleManager(
        RuleRepository(rules_dir), pipeline, approvals, breaker, audit, apply, set_alert,
        clear_alert, notify,
    )  # fmt: skip
    return env


def git_log(directory: Path) -> list[tuple[str, str]]:
    out = subprocess.run(
        ["git", "log", "--format=%an|%s"], cwd=directory, capture_output=True, text=True, check=True
    ).stdout
    return [tuple(line.split("|", 1)) for line in out.splitlines()]  # type: ignore[misc]


# --- Policy: Stufe nach Inhalt ---------------------------------------------------


@pytest.mark.parametrize(
    ("action", "tier"),
    [
        ("{type: delay, duration: '00:00:05'}", 0),
        ("{type: ha_service, service: light.turn_on, target: {entity_id: light.a}}", 1),
        ("{type: notify, service: notify.mobile, data: {message: hi}}", 1),
        ("{type: ha_service, service: lock.lock, target: {entity_id: lock.front}}", 2),
        ("{type: ha_service, service: light.turn_on, target: {entity_id: light.garage_tor}}", 2),
        ("{type: ha_service, service: switch.turn_on, target: {entity_id: switch.wallbox}}", 2),
        ("{type: ha_service, service: script.turn_on, target: {entity_id: script.x}}", 2),
        ("{type: ha_tool, tool: HassTurnOn, arguments: {name: x}}", 2),
        ("{type: mqtt_publish, topic: a/b, payload: x}", 2),
        ("{type: webhook, url: 'http://x'}", 2),
        (
            "{type: if, condition: {state: {entity_id: sensor.x, is: 'on'}}, "
            "then: [{type: ha_service, service: lock.lock, target: {entity_id: lock.front}}]}",
            2,
        ),
    ],
)
def test_tier_is_derived_from_content(action: str, tier: int) -> None:
    rule = parse(f"rules:\n- id: t\n  triggers: [{{type: startup}}]\n  actions: [{action}]\n")
    assert Policy().tier_of(rule) == tier


def test_alert_only_is_tier_0_and_policy_is_configurable(tmp_path: Path) -> None:
    assert Policy().tier_of(parse(ALERT_ONLY)) == 0
    path = tmp_path / "policy.yaml"
    path.write_text("limits: {max_fires_per_day: 5}\ntier1: {mqtt_topics: ['home/ok/*']}\n")
    policy = Policy.load(path)
    assert policy.max_fires_per_day == 5
    rule = parse("rules:\n- id: t\n  triggers: [{type: startup}]\n  actions: "
                 "[{type: mqtt_publish, topic: home/ok/x, payload: y}]\n")  # fmt: skip
    assert policy.tier_of(rule) == 1


def test_per_entity_template_uses_bound_entities_for_tier() -> None:
    rule = parse(
        "rules:\n- id: t\n  per_entity: [lock.front]\n  triggers: [{type: startup}]\n"
        "  actions: [{type: ha_service, service: light.turn_on, "
        "target: {entity_id: '{{ entity_id }}'}}]\n"
    )
    assert Policy().tier_of(rule) == 2


# --- Pipeline -------------------------------------------------------------------------


async def test_pipeline_rejects_unknown_entity_with_suggestion(tmp_path: Path) -> None:
    env = build(tmp_path)
    report = await env.manager.pipeline.check(rule_yaml(trigger="sensor.nope"), {})
    assert not report.ok
    assert "unknown entity 'sensor.nope'" in report.errors[0] and "sensor.x" in report.errors[0]


async def test_pipeline_rejects_schema_errors_author_status_and_tier(tmp_path: Path) -> None:
    env = build(tmp_path)
    bad = await env.manager.pipeline.check("rules: [{id: x, bogus: 1}]", {})
    assert not bad.ok and bad.errors[0].startswith("schema")
    shadow = await env.manager.pipeline.check(rule_yaml(extra="  status: shadow\n"), {})
    assert "computed by the bridge" in shadow.errors[0]
    tier = await env.manager.pipeline.check(rule_yaml(extra="  tier: 0\n"), {})
    assert not tier.ok


async def test_pipeline_backtest_limit_rejects_noisy_rules(tmp_path: Path) -> None:
    env = build(tmp_path)
    env.fires[0] = 25
    report = await env.manager.pipeline.check(rule_yaml(), {})
    assert not report.ok and "25 times per day" in report.errors[0]
    env.fires[0] = 3
    assert (await env.manager.pipeline.check(rule_yaml(), {})).ok
    # Alert-only rules need no backtest
    env.fires[0] = 99
    assert (await env.manager.pipeline.check(ALERT_ONLY, {})).ok


async def test_pipeline_conflicts_warn_on_tier1_and_block_on_tier2(tmp_path: Path) -> None:
    env = build(tmp_path)
    on = parse(rule_yaml("on", "light.turn_on", "light.a"))
    tier1 = await env.manager.pipeline.check(
        rule_yaml("off", "light.turn_off", "light.a", "sensor.y"), {"on": on}
    )
    assert tier1.ok and tier1.conflicts[0]["rule_id"] == "on"
    unlock = parse(rule_yaml("unlock", "lock.unlock", "lock.front"))
    tier2 = await env.manager.pipeline.check(LOCK_RULE, {"unlock": unlock})
    assert not tier2.ok and "opposite actions" in tier2.errors[0]


async def test_pipeline_rejects_cycles_but_not_self_loops(tmp_path: Path) -> None:
    env = build(tmp_path)
    a = parse(rule_yaml("a", "light.turn_on", "light.b", "light.a"))
    b = rule_yaml("b", "light.turn_on", "light.a", "light.b")
    report = await env.manager.pipeline.check(b, {"a": a})
    assert not report.ok and "action loop" in report.errors[0]
    assert find_cycles({"s": parse(rule_yaml("s", "light.turn_on", "light.a", "light.a"))}) == []


async def test_pipeline_startup_mode_skips_entity_and_backtest(tmp_path: Path) -> None:
    env = build(tmp_path)
    report = await env.manager.pipeline.check(rule_yaml(trigger="sensor.nope"), {}, full=False)
    assert report.ok and report.tier == 1 and report.backtest is None


# --- Manager: MCP-Schreibpfad, Versionierung, Rollback ----------------------------------


async def test_create_commits_with_intent_and_authoring_metadata(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    result = await env.manager.create(ALERT_ONLY, "Warne bei leerer Batterie")
    assert result["tier"] == 0 and result["status"] == "active" and result["version"] == 1
    assert git_log(env.rules_dir)[0] == ("claude-mcp", "Warne bei leerer Batterie")
    stored = parse((env.rules_dir / "batt.yaml").read_text())
    assert stored.created_by == "claude" and stored.intent == "Warne bei leerer Batterie"
    assert "claude-mcp" in {a for a, *_ in env.audit_rows()}
    with pytest.raises(AuthoringError, match="already exists"):
        await env.manager.create(ALERT_ONLY, "nochmal")
    with pytest.raises(AuthoringError, match="exactly one rule"):
        await env.manager.create(rule_yaml("p") + rule_yaml("q").split("rules:\n")[1], "zwei")


async def test_invalid_write_changes_nothing(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    before = git_log(env.rules_dir)
    with pytest.raises(AuthoringError, match="unknown entity"):
        await env.manager.create(rule_yaml(trigger="sensor.nope"), "x")
    assert git_log(env.rules_dir) == before
    assert not (env.rules_dir / "lamp.yaml").exists() and env.applied == []


async def test_update_versions_and_rollback(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    await env.manager.create(rule_yaml(), "v1 wunsch")
    await env.manager.update("lamp", rule_yaml(service="light.turn_off"), "v2 wunsch")
    versions = env.manager.repo.versions("lamp")
    assert [v.version for v in versions] == [1, 2]
    assert [v.message for v in versions] == ["v1 wunsch", "v2 wunsch"]
    assert env.applied[0].version == 2

    result = await env.manager.rollback("lamp", 1)
    assert result["version"] == 3
    current = env.manager.raw("lamp")
    assert current is not None and current.actions is not None
    assert (current.actions[0].model_extra or {})["service"] == "light.turn_on"
    assert current.intent == "v1 wunsch"
    assert git_log(env.rules_dir)[0][1].startswith("rollback lamp to version 1")
    with pytest.raises(AuthoringError, match="no version 9"):
        await env.manager.rollback("lamp", 9)


async def test_soft_delete_and_restore_via_rollback(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    await env.manager.create(ALERT_ONLY, "Batterie")
    await env.manager.delete("batt")
    assert env.applied == [] and not (env.rules_dir / "batt.yaml").exists()
    assert (env.rules_dir / ".deleted" / "batt.yaml").exists()
    with pytest.raises(AuthoringError, match="deleted"):
        await env.manager.create(ALERT_ONLY, "neu")
    await env.manager.rollback("batt", 1)
    assert [r.id for r in env.applied] == ["batt"]
    assert not (env.rules_dir / ".deleted" / "batt.yaml").exists()


async def test_locked_rules_are_protected_only_for_mcp(tmp_path: Path) -> None:
    env = build(tmp_path)
    (env.rules_dir / "batt.yaml").write_text(
        ALERT_ONLY.replace("  alert:", "  locked: true\n  alert:")
    )
    await env.manager.startup()
    for call in (
        env.manager.update("batt", ALERT_ONLY, "x"),
        env.manager.set_enabled("batt", False),
        env.manager.delete("batt"),
        env.manager.rollback("batt", 1),
    ):
        with pytest.raises(AuthoringError, match="locked"):
            await call
    # Dateizugriff unterliegt der Policy, nicht dem Lock
    (env.rules_dir / "batt.yaml").write_text(ALERT_ONLY.replace("below: 10", "below: 15"))
    assert await env.manager.scan()
    assert env.manager.raw("batt") is not None


async def test_disable_and_enable(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    await env.manager.create(ALERT_ONLY, "Batterie")
    await env.manager.set_enabled("batt", False)
    assert env.applied[0].enabled is False
    await env.manager.set_enabled("batt", True)
    assert env.applied[0].enabled is True


# --- Stufe 2: Shadow und Freigabe ---------------------------------------------------------


async def test_tier2_starts_as_shadow_until_approved_outside_mcp(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    result = await env.manager.create(LOCK_RULE, "Verriegle die Tür")
    assert result["tier"] == 2 and result["status"] == "shadow" and result["needs_approval"]
    assert env.notified == ["door"] and "approval_door" in env.alerts
    assert env.manager.final["door"].status == "shadow"

    # Claude kann nicht freigeben: weder Status im YAML noch ein Tool
    shadow = await env.manager.pipeline.check(rule_yaml(extra="  status: active\n  tier: 2\n"), {})
    assert not shadow.ok

    env.approvals.decide(env.manager.raw("door"), "approved", "cli")  # type: ignore[arg-type]
    assert await env.manager.scan()
    assert env.status("door") == "active" and "approval_door" not in env.alerts


async def test_approval_is_invalidated_by_content_change_not_metadata(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    await env.manager.create(LOCK_RULE, "Verriegle die Tür")
    env.approvals.decide(env.manager.raw("door"), "approved", "cli")  # type: ignore[arg-type]
    await env.manager.scan()
    assert env.status("door") == "active"

    await env.manager.update("door", LOCK_RULE.replace("Verriegle", "Sperre"), "Sperre die Tür")
    assert env.status("door") == "active"  # nur intent geändert

    changed = rule_yaml("door", "lock.unlock", "lock.front", "sensor.y", "Entriegle")
    await env.manager.update("door", changed, "Entriegle")
    assert env.status("door") == "shadow"


async def test_rejected_tier2_rule_is_disabled(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    await env.manager.create(LOCK_RULE, "Verriegle")
    env.approvals.decide(env.manager.raw("door"), "rejected", "cli")  # type: ignore[arg-type]
    await env.manager.scan()
    assert env.status("door") == "disabled"


async def test_tier2_rule_written_as_file_is_also_shadow(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    (env.rules_dir / "door.yaml").write_text(LOCK_RULE)  # Pfad egal, Inhalt zählt
    await env.manager.scan()
    assert env.status("door") == "shadow"


# --- Datei-Watcher -------------------------------------------------------------------------------


async def test_file_change_is_accepted_audited_and_committed_as_file(tmp_path: Path) -> None:
    env = build(tmp_path)
    (env.rules_dir / "a.yaml").write_text(ALERT_ONLY)
    await env.manager.startup()
    assert git_log(env.rules_dir)[-1] == ("file", "initial import")
    (env.rules_dir / "a.yaml").write_text(ALERT_ONLY.replace("below: 10", "below: 20"))
    assert await env.manager.scan() and not await env.manager.scan()
    assert env.applied[0].created_by == "file"
    assert git_log(env.rules_dir)[0] == ("file", "Warne bei leerer Batterie")
    assert ("file", "rule_file_accepted", "a.yaml", "ok") in env.audit_rows()


async def test_invalid_file_keeps_last_valid_version_and_raises_alert(tmp_path: Path) -> None:
    env = build(tmp_path)
    (env.rules_dir / "a.yaml").write_text(ALERT_ONLY)
    await env.manager.startup()
    (env.rules_dir / "a.yaml").write_text("rules: [{id: batt, nonsense: true}]")
    await env.manager.scan()
    assert [r.id for r in env.applied] == ["batt"]
    assert env.applied[0].condition is not None
    assert "file_a.yaml" in env.alerts
    assert ("file", "rule_file_rejected", "a.yaml", "rejected") in env.audit_rows()
    # MCP-Schreibzugriff auf eine Regel in der defekten Datei wird verweigert
    with pytest.raises(AuthoringError, match="invalid edits"):
        await env.manager.update("batt", ALERT_ONLY, "x")
    # ungültige Edits landen nie im Verlauf
    assert len(env.manager.repo.versions("batt")) == 1

    (env.rules_dir / "a.yaml").write_text(ALERT_ONLY.replace("below: 10", "below: 5"))
    await env.manager.scan()
    assert "file_a.yaml" not in env.alerts


async def test_restart_falls_back_to_last_committed_version(tmp_path: Path) -> None:
    env = build(tmp_path)
    (env.rules_dir / "a.yaml").write_text(ALERT_ONLY)
    await env.manager.startup()
    (env.rules_dir / "a.yaml").write_text("not: [valid")
    second = build(tmp_path)
    await second.manager.startup()
    assert [r.id for r in second.applied] == ["batt"] and "file_a.yaml" in second.alerts


async def test_removed_file_removes_rules(tmp_path: Path) -> None:
    env = build(tmp_path)
    (env.rules_dir / "a.yaml").write_text(ALERT_ONLY)
    await env.manager.startup()
    (env.rules_dir / "a.yaml").unlink()
    await env.manager.scan()
    assert env.applied == []


# --- Circuit-Breaker ----------------------------------------------------------------------------------


async def test_circuit_breaker_disables_rule_until_updated(tmp_path: Path) -> None:
    env = build(tmp_path, max_per_hour=2)
    await env.manager.startup()
    await env.manager.create(rule_yaml(), "Licht")
    assert [env.breaker.record("lamp") for _ in range(3)] == [False, False, True]
    await env.manager.trip("lamp")
    assert env.status("lamp") == "disabled" and "trip_lamp" in env.alerts
    assert ("bridge", "circuit_breaker", "lamp", "ok") in env.audit_rows()
    await env.manager.update("lamp", rule_yaml(), "Licht, enger gefasst")
    assert env.status("lamp") == "active" and "trip_lamp" not in env.alerts


def test_breaker_window_expires(tmp_path: Path) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    breaker = CircuitBreaker(tmp_path / "b.db", lambda: clock[0], 2)
    breaker.record("r")
    breaker.record("r")
    clock[0] += timedelta(hours=2)
    assert breaker.record("r") is False and not breaker.is_tripped("r")


# --- Freigabe-Tokens -------------------------------------------------------------------------------------


def test_token_is_single_use_signed_and_expires(tmp_path: Path) -> None:
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    store = ApprovalStore(tmp_path / "ap", tmp_path / "t.db", b"k", now=lambda: clock[0])
    rule = parse(LOCK_RULE)
    tokens = store.issue(rule)
    assert store.redeem(tokens["approved"], rule) == "approved"
    assert store.decision_for(rule) == "approved"
    for token in tokens.values():  # beide Tokens teilen einen Nonce
        with pytest.raises(ApprovalError, match="already used"):
            store.redeem(token, rule)

    fresh = store.issue(rule)
    head, sig = fresh["rejected"].rsplit(".", 1)
    with pytest.raises(ApprovalError, match="invalid token"):
        store.inspect(f"{head}.{'0' * len(sig)}")
    other = ApprovalStore(tmp_path / "ap", tmp_path / "t.db", b"other")
    with pytest.raises(ApprovalError, match="invalid token"):
        other.inspect(fresh["rejected"])
    clock[0] += timedelta(days=2)
    with pytest.raises(ApprovalError, match="expired"):
        store.inspect(fresh["approved"])


def test_token_is_stale_after_rule_changed(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "ap", tmp_path / "t.db", b"k")
    tokens = store.issue(parse(LOCK_RULE))
    changed = parse(LOCK_RULE.replace("lock.lock", "lock.unlock"))
    assert rule_hash(changed) != rule_hash(parse(LOCK_RULE))
    with pytest.raises(ApprovalError, match="stale"):
        store.redeem(tokens["approved"], changed)
    assert store.decision_for(changed) is None


# --- CLI -----------------------------------------------------------------------------------------------------


def test_cli_approve_reject_and_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, data = tmp_path / "config", tmp_path / "data"
    (config / "rules").mkdir(parents=True)
    data.mkdir()
    (config / "rules" / "door.yaml").write_text(LOCK_RULE)
    (config / "rules" / "lamp.yaml").write_text(rule_yaml())
    paths = ["--config-dir", str(config), "--data-dir", str(data)]
    assert cli_main(["pending", *paths]) == 0
    assert capsys.readouterr().out.startswith("door")
    assert cli_main(["approve", "lamp", *paths]) == 1  # Stufe 1 braucht keine Freigabe
    assert cli_main(["approve", "door", *paths]) == 0
    store = ApprovalStore(config / "approvals", data / "bridge.db", b"x")
    assert store.decision_for(parse(LOCK_RULE)) == "approved"
    assert cli_main(["reject", "door", *paths]) == 0
    assert store.decision_for(parse(LOCK_RULE)) == "rejected"
    rows = sqlite3.connect(data / "bridge.db").execute("SELECT actor, action FROM audit_log")
    assert rows.fetchall() == [("cli", "rule_approved"), ("cli", "rule_rejected")]
    assert cli_main(["pending", *paths]) == 0
    assert "no rules waiting" in capsys.readouterr().out


# --- Schreib-Tools: Rate-Limit, Snooze, Beschreibung -----------------------------------------------------------


class _Alerts:
    def __init__(self) -> None:
        self.snoozed: list[str] = []

    def alerts(self) -> list[Any]:
        return [type("A", (), {"id": "a1"})()]

    async def snooze(self, alert_id: str, until: datetime) -> None:
        self.snoozed.append(alert_id)


async def test_write_service_rate_limit_and_validation(tmp_path: Path) -> None:
    env = build(tmp_path)
    await env.manager.startup()
    alerts = _Alerts()
    clock = env.clock
    writes = WriteService(env.manager, alerts, None, lambda: clock[0], writes_per_hour=1)  # type: ignore[arg-type]
    assert (await writes.acknowledge_alert("a1", 30))["id"] == "a1"
    with pytest.raises(ToolError, match="unknown"):
        await writes.acknowledge_alert("zzz", 30)
    with pytest.raises(ToolError, match="catalog"):
        await writes.set_entity_description("light.a", "x")
    assert (await writes.create_rule(ALERT_ONLY, "x"))["status"] == "active"
    with pytest.raises(ToolError, match="rate limit"):
        await writes.delete_rule("batt")
    clock[0] += timedelta(hours=2)
    assert (await writes.delete_rule("batt"))["deleted"] is True
