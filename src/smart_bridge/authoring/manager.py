"""RuleManager: Hot-Reload der YAML-Dateien und MCP-Schreibpfad über dieselbe Pipeline.

- Dateien: Polling-Scan über Datei-Hashes. Gültig → aktiv (`created_by: file`, Audit, Commit als
  `file`). Ungültig → letzte gültige Version bleibt aktiv, Fehler erscheint als Alert.
- MCP: `create/update/set_enabled/delete/rollback`; `locked` gilt nur hier.
- Status wird immer von der Bridge berechnet (Stufe 2 → `shadow`, bis freigegeben).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
from collections.abc import Awaitable, Callable, Mapping
from io import StringIO
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..approvals import ApprovalStore, rule_hash
from ..audit import AuditLog
from ..rules import parse_rules_text
from ..rules.models import Rule
from .breaker import CircuitBreaker
from .pipeline import Pipeline, Report
from .repository import RuleRepository, dump_rules, sha256

log = logging.getLogger(__name__)

Apply = Callable[[list[Rule], dict[str, str]], Awaitable[None]]
SetAlert = Callable[[str, str, str, str], Awaitable[None]]  # key, title, message, severity
ClearAlert = Callable[[str], Awaitable[None]]
NotifyPending = Callable[[Rule], Awaitable[None]]


class AuthoringError(Exception):
    """Schreiben abgelehnt; `report` enthält die Pipeline-Ergebnisse (falls vorhanden)."""

    def __init__(self, message: str, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


def _diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), "before", "after", lineterm="", n=2
    )
    return "\n".join(lines)[:4000]


class RuleManager:
    def __init__(
        self,
        repo: RuleRepository,
        pipeline: Pipeline,
        approvals: ApprovalStore,
        breaker: CircuitBreaker,
        audit: AuditLog,
        apply: Apply,
        set_alert: SetAlert,
        clear_alert: ClearAlert,
        notify_pending: NotifyPending | None = None,
    ) -> None:
        self.repo, self.pipeline, self.approvals = repo, pipeline, approvals
        self.breaker, self._audit, self._apply = breaker, audit, apply
        self._set_alert, self._clear_alert, self._notify = set_alert, clear_alert, notify_pending
        self._files: dict[str, list[Rule]] = {}
        self._seen: dict[str, str] = {}
        self._invalid: set[str] = set()
        self._file_ids: set[str] = set()
        self._approvals_seen = ""
        self._notified: set[tuple[str, str]] = set()
        self._alerted: set[str] = set()
        self.final: dict[str, Rule] = {}
        self.file_hashes: dict[str, str] = {}
        self.lock = asyncio.Lock()

    # --- Lesen ------------------------------------------------------------------

    def raw(self, rule_id: str) -> Rule | None:
        return next((r for rules in self._files.values() for r in rules if r.id == rule_id), None)

    def raw_rules(self) -> dict[str, Rule]:
        return {r.id: r for name in sorted(self._files) for r in self._files[name]}

    def _others(
        self, exclude_name: str | None = None, exclude_ids: set[str] | None = None
    ) -> dict[str, Rule]:
        skip = exclude_ids or set()
        return {
            r.id: r
            for name in sorted(self._files)
            if name != exclude_name
            for r in self._files[name]
            if r.id not in skip
        }

    def _file_of(self, rule_id: str) -> str | None:
        return next(
            (n for n, rules in self._files.items() if any(r.id == rule_id for r in rules)), None
        )

    # --- Start ----------------------------------------------------------------------

    async def startup(self) -> None:
        """Regeln laden (ohne Entity-Prüfung: HA ist noch nicht synchronisiert) und anwenden."""
        self.repo.ensure()
        for path in self.repo.files():
            text = path.read_text(encoding="utf-8")
            digest = sha256(text)
            head = self.repo.head_blob(path.name)
            report = await self.pipeline.check(text, self._others(path.name), full=False)
            if report.ok:
                self._files[path.name] = report.rules
                if head is not None and sha256(head) != digest:
                    await self._accept_file(path.name, report, text)
                elif head is None:
                    await self._accept_file(path.name, report, text)
            else:
                await self._reject_file(path.name, report)
                if head:
                    fallback = await self.pipeline.check(head, self._others(path.name), full=False)
                    if fallback.ok:
                        self._files[path.name] = fallback.rules
            self._seen[path.name] = digest
        self._approvals_seen = self.approvals.digest()
        await self._reload()

    # --- Watcher ----------------------------------------------------------------------

    async def scan(self) -> bool:
        """Ein Durchlauf des Watchers. `True`, wenn sich etwas geändert hat."""
        async with self.lock:
            changed = False
            names = {p.name: p for p in self.repo.files()}
            for name, path in names.items():
                text = path.read_text(encoding="utf-8")
                digest = sha256(text)
                if self._seen.get(name) == digest:
                    continue
                self._seen[name] = digest
                changed = True
                await self._file_changed(name, text)
            for name in set(self._seen) - set(names):
                del self._seen[name]
                self._invalid.discard(name)
                if self._files.pop(name, None) is not None:
                    self.repo.commit(f"file removed: {name}", "file", paths=[name])
                    self._audit.record("file", "rule_file_removed", name)
                await self._clear_alert(f"file_{name}")
                changed = True
            digest = self.approvals.digest()
            if digest != self._approvals_seen:
                self._approvals_seen, changed = digest, True
            if changed:
                await self._reload()
            return changed

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.scan()
            except Exception:
                log.exception("rule scan failed")
            await asyncio.sleep(interval)

    async def _file_changed(self, name: str, text: str) -> None:
        report = await self.pipeline.check(text, self._others(name), full=True)
        if report.ok:
            self._files[name] = report.rules
            await self._accept_file(name, report, text)
        else:
            await self._reject_file(name, report)

    async def _accept_file(self, name: str, report: Report, text: str) -> None:
        self._invalid.discard(name)
        for rule in report.rules:
            self._file_ids.add(rule.id)
            self.breaker.clear(rule.id)
        await self._clear_alert(f"file_{name}")
        intent = next((r.intent for r in report.rules if r.intent), None)
        self.repo.commit(intent or f"file change: {name}", "file", paths=[name])
        self._audit.record(
            "file",
            "rule_file_accepted",
            name,
            {
                "created_by": "file",
                "rules": [r.id for r in report.rules],
                "tier": report.tier,
                "warnings": report.warnings,
                "hash": sha256(text)[:12],
            },
        )

    async def _reject_file(self, name: str, report: Report) -> None:
        self._invalid.add(name)
        self._audit.record(
            "file", "rule_file_rejected", name, {"errors": report.errors}, result="rejected"
        )
        message = "; ".join(report.errors)[:400]
        log.error("rule file %s rejected: %s", name, message)
        await self._set_alert(
            f"file_{name}",
            f"Regeldatei {name} abgelehnt",
            f"{message} (letzte gültige Version bleibt aktiv)",
            "warning",
        )

    # --- Anwenden ------------------------------------------------------------------------

    def _finalize(self, rule: Rule, tripped: set[str]) -> Rule:
        status = "disabled" if rule.status == "disabled" else "active"
        tier = self.pipeline.tier(rule)
        if rule.id in tripped:
            status = "disabled"
        elif tier == 2 and status == "active":
            decision = self.approvals.decision_for(rule)
            status = {"approved": "active", "rejected": "disabled"}.get(decision or "", "shadow")
        update: dict[str, Any] = {"status": status}
        history = self.repo.versions(rule.id)
        if history:
            update["version"] = len(history)
        if rule.id in self._file_ids:
            update["created_by"] = "file"
        return rule.model_copy(update=update)

    async def _reload(self) -> None:
        raw: list[Rule] = []
        errors: dict[str, str] = {}
        seen: set[str] = set()
        for name in sorted(self._files):
            for rule in self._files[name]:
                if rule.id in seen:
                    errors[rule.id] = f"duplicate rule id (also defined before {name})"
                    continue
                seen.add(rule.id)
                raw.append(rule)
                self.file_hashes[rule.id] = self._seen.get(name, "")
        tripped = self.breaker.tripped()
        final = [self._finalize(r, tripped) for r in raw]
        self.final = {r.id: r for r in final}
        await self._apply(final, errors)
        await self._sync_alerts(final, tripped)

    async def _sync_alerts(self, final: list[Rule], tripped: set[str]) -> None:
        wanted: dict[str, tuple[str, str, str]] = {}
        for rule in final:
            label = rule.title or rule.id
            if rule.id in tripped:
                wanted[f"trip_{rule.id}"] = (
                    f"Circuit-Breaker: {label}",
                    "Regel wegen zu vieler Aktivierungen pro Stunde deaktiviert. "
                    "Regel anpassen (update_rule) oder erneut aktivieren.",
                    "warning",
                )
            elif rule.status == "shadow":
                wanted[f"approval_{rule.id}"] = (
                    f"Freigabe nötig: {label}",
                    f"Stufe-2-Regel im Schattenmodus. Freigabe: bridge approve {rule.id}",
                    "info",
                )
        for key, (title, message, severity) in wanted.items():
            await self._set_alert(key, title, message, severity)
        for key in self._alerted - set(wanted):
            await self._clear_alert(key)
        self._alerted = set(wanted)
        for rule in final:
            if rule.status == "shadow" and self._notify:
                token = (rule.id, rule_hash(rule))
                if token not in self._notified:
                    self._notified.add(token)
                    try:
                        await self._notify(rule)
                    except Exception:
                        log.exception("approval notification for %s failed", rule.id)

    async def trip(self, rule_id: str) -> None:
        """Vom Circuit-Breaker: Regel ist soeben getrippt."""
        async with self.lock:
            self._audit.record(
                "bridge", "circuit_breaker", rule_id, {"limit": "max_activations_per_hour"}
            )
            await self._reload()

    # --- MCP-Schreibpfad ----------------------------------------------------------------------

    def _check_locked(self, rule_id: str) -> Rule:
        rule = self.raw(rule_id)
        if rule is None:
            raise AuthoringError(f"rule {rule_id!r} not found")
        if rule.locked:
            raise AuthoringError(
                f"rule {rule_id!r} is locked (locked: true); unlock it in the file"
            )
        name = self._file_of(rule_id)
        if name in self._invalid:
            raise AuthoringError(f"file {name} has pending invalid edits; fix the file first")
        return rule

    async def _write(
        self,
        text: str,
        *,
        rule_id: str | None,
        intent: str | None,
        message: str,
        action: str,
        check: bool = True,
        need_intent: bool = True,
    ) -> dict[str, Any]:
        doc = YAML().load(text)
        items = doc.get("rules") if hasattr(doc, "get") else None
        if not isinstance(items, list) or len(items) != 1:
            raise AuthoringError("rule_yaml must contain exactly one rule under 'rules'")
        item = items[0]
        rid = str(item.get("id", ""))
        if rule_id and rid != rule_id:
            raise AuthoringError(f"rule id in YAML ({rid!r}) must equal {rule_id!r}")
        if intent is not None:
            item["intent"] = intent
        if need_intent and not item.get("intent"):
            raise AuthoringError("intent is required (the user's original wish, verbatim)")
        item["created_by"] = "claude"
        item["version"] = len(self.repo.versions(rid)) + 1
        report = Report()
        final_text = dump_rules([item])
        if check:
            report = await self.pipeline.check(
                final_text, self._others(exclude_ids={rid}), single=True
            )
            if not report.ok:
                raise AuthoringError("rule rejected: " + "; ".join(report.errors), report.to_dict())
        before = self.repo.read_item(rid)
        before_text = dump_rules([before]) if before is not None else ""
        path = self.repo.write_rule(rid, item)
        self.repo.purge_deleted(rid)
        self.repo.commit(message, "claude-mcp", paths=[path.name])
        self._finish_write(path, rid)
        self._audit.record(
            "claude-mcp",
            action,
            rid,
            {"diff": _diff(before_text, final_text), "tier": report.tier},
        )
        await self._reload()
        final = self.final.get(rid)
        status = final.status if final else "unknown"
        return {
            "rule_id": rid,
            "version": final.version if final else None,
            "tier": self.pipeline.tier(raw) if (raw := self.raw(rid)) else report.tier,
            "status": status,
            "needs_approval": status == "shadow",
            "warnings": report.warnings,
            "conflicts": report.conflicts,
            "duplicates": report.duplicates,
            "backtest": _backtest_summary(report.backtest),
        }

    def _finish_write(self, path: Path, rule_id: str) -> None:
        text = path.read_text(encoding="utf-8")
        rules, _ = parse_rules_text(text)
        self._files[path.name] = rules
        self._seen[path.name] = sha256(text)
        self._invalid.discard(path.name)
        self._file_ids.discard(rule_id)
        self.breaker.clear(rule_id)

    async def create(self, rule_yaml: str, intent: str) -> dict[str, Any]:
        async with self.lock:
            rid = _first_id(rule_yaml)
            if rid and (self.raw(rid) or self.repo.read_deleted(rid)):
                raise AuthoringError(
                    f"rule {rid!r} already exists or was deleted; use update_rule/rollback_rule"
                )
            return await self._write(
                rule_yaml, rule_id=None, intent=intent, message=intent, action="rule_create"
            )

    async def update(self, rule_id: str, rule_yaml: str, intent: str) -> dict[str, Any]:
        async with self.lock:
            self._check_locked(rule_id)
            return await self._write(
                rule_yaml, rule_id=rule_id, intent=intent, message=intent, action="rule_update"
            )

    async def set_enabled(self, rule_id: str, enabled: bool) -> dict[str, Any]:
        async with self.lock:
            self._check_locked(rule_id)
            item = self.repo.read_item(rule_id)
            if item is None:
                raise AuthoringError(f"rule {rule_id!r} not found")
            item["enabled"] = enabled
            text = dump_rules([item])
            return await self._write(
                text,
                rule_id=rule_id,
                intent=None,
                message=f"{'enable' if enabled else 'disable'} {rule_id}",
                action="rule_enable" if enabled else "rule_disable",
                check=enabled,
                need_intent=False,
            )

    async def delete(self, rule_id: str) -> dict[str, Any]:
        async with self.lock:
            self._check_locked(rule_id)
            before = self.repo.read_item(rule_id)
            name = self._file_of(rule_id)
            self.repo.soft_delete(rule_id)
            assert name is not None
            self.repo.commit(f"delete {rule_id}", "claude-mcp")
            path = self.repo.dir / name
            if path.exists():
                text = path.read_text(encoding="utf-8")
                self._files[name] = parse_rules_text(text)[0]
                self._seen[name] = sha256(text)
            else:
                self._files.pop(name, None)
                self._seen.pop(name, None)
            self.breaker.clear(rule_id)
            self._audit.record(
                "claude-mcp",
                "rule_delete",
                rule_id,
                {"diff": _diff(dump_rules([before]) if before is not None else "", "")},
            )
            await self._reload()
            return {"rule_id": rule_id, "deleted": True, "restorable": True}

    async def rollback(self, rule_id: str, version: int) -> dict[str, Any]:
        async with self.lock:
            if self.raw(rule_id):
                self._check_locked(rule_id)
            try:
                text = self.repo.version_text(rule_id, version)
            except Exception as exc:
                raise AuthoringError(str(exc)) from exc
            old = YAML().load(text)["rules"][0]
            return await self._write(
                text,
                rule_id=rule_id,
                intent=None,
                message=f"rollback {rule_id} to version {version}: {old.get('intent', '')}",
                action="rule_rollback",
                need_intent=False,
            )


def _first_id(text: str) -> str | None:
    try:
        data = YAML(typ="safe").load(StringIO(text))
        return str(data["rules"][0]["id"])
    except Exception:
        return None


def _backtest_summary(result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not result or not result.get("valid"):
        return None
    return {
        "days": result.get("days"),
        "rules": {
            rid: {
                "activation_count": r["activation_count"],
                "fire_count": r["fire_count"],
                "fires_per_day": r["fires_per_day"],
            }
            for rid, r in result.get("rules", {}).items()
        },
    }
