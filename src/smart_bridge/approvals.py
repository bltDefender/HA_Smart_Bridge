"""Freigaben für Stufe-2-Regeln. Nur Bridge, CLI und signierter Webhook schreiben hier.

Eine Freigabe gilt für einen Inhalts-Hash der Regel (Trigger, Bedingung, Aktionen, Alert, …).
Jede inhaltliche Änderung macht sie ungültig; Metadaten (Titel, Tags, Version, …) nicht.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from ruamel.yaml import YAML

from .audit import AuditLog
from .clock import Now, utc_now
from .rules.models import Rule

Decision = Literal["approved", "rejected"]
_META = {"status", "version", "created_by", "locked", "tags", "intent", "title", "enabled"}

_SCHEMA = "CREATE TABLE IF NOT EXISTS approval_tokens (nonce TEXT PRIMARY KEY, used_at TEXT)"


class ApprovalError(Exception):
    pass


def rule_hash(rule: Rule) -> str:
    data = rule.model_dump(by_alias=True, exclude_none=True, mode="json", exclude=_META)
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_secret(configured: str | None, data_dir: Path) -> bytes:
    """Signierschlüssel: Secret aus der Umgebung, sonst `data/approval.key` (0600, automatisch)."""
    if configured:
        return configured.encode()
    path = data_dir / "approval.key"
    if not path.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(32))
    return path.read_text().strip().encode()


@dataclass(frozen=True)
class Approval:
    rule_id: str
    decision: Decision
    rule_hash: str
    actor: str
    at: str


class ApprovalStore:
    def __init__(
        self,
        directory: Path,
        db_path: Path | str,
        secret: bytes,
        audit: AuditLog | None = None,
        now: Now = utc_now,
        ttl: timedelta = timedelta(hours=24),
    ) -> None:
        self._dir, self._secret, self._audit, self._now, self._ttl = (
            directory,
            secret,
            audit,
            now,
            ttl,
        )
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- Dateien ------------------------------------------------------------

    def _path(self, rule_id: str) -> Path:
        return self._dir / f"{rule_id}.yaml"

    def get(self, rule_id: str) -> Approval | None:
        path = self._path(rule_id)
        if not path.is_file():
            return None
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or {}
        try:
            return Approval(
                rule_id, data["decision"], data["rule_hash"], str(data["actor"]), str(data["at"])
            )
        except (KeyError, TypeError):
            return None

    def decision_for(self, rule: Rule) -> Decision | None:
        """Entscheidung, die für genau diesen Regelinhalt gilt (sonst `None` = ausstehend)."""
        found = self.get(rule.id)
        if (
            found
            and found.rule_hash == rule_hash(rule)
            and found.decision
            in (
                "approved",
                "rejected",
            )
        ):
            return found.decision
        return None

    def decide(self, rule: Rule, decision: Decision, actor: str) -> None:
        self._write(rule.id, rule_hash(rule), decision, actor)

    def _write(self, rule_id: str, digest: str, decision: Decision, actor: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        data = {
            "rule_id": rule_id,
            "decision": decision,
            "rule_hash": digest,
            "actor": actor,
            "at": self._now().isoformat(),
        }
        yaml = YAML()
        path = self._path(rule_id)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.dump(data, fh)
        tmp.replace(path)
        if self._audit:
            self._audit.record(
                actor, f"rule_{decision}", rule_id, {"rule_hash": digest[:12]}, result="ok"
            )

    def digest(self) -> str:
        """Fingerabdruck des Freigabeverzeichnisses, damit der Watcher Änderungen bemerkt."""
        h = hashlib.sha256()
        for f in sorted(self._dir.glob("*.yaml")) if self._dir.is_dir() else []:
            h.update(f.name.encode() + f.read_bytes())
        return h.hexdigest()

    # --- Signierte Einmal-Tokens ---------------------------------------------

    def issue(self, rule: Rule) -> dict[Decision, str]:
        """Zwei Tokens (freigeben/ablehnen) mit gemeinsamem Nonce: eines davon ist nutzbar."""
        nonce = secrets.token_urlsafe(12)
        exp = int((self._now() + self._ttl).timestamp())
        return {d: self._sign(rule.id, rule_hash(rule), d, nonce, exp) for d in _DECISIONS}

    def _sign(self, rule_id: str, digest: str, decision: str, nonce: str, exp: int) -> str:
        body = json.dumps([rule_id, digest, decision, nonce, exp], separators=(",", ":"))
        payload = base64.urlsafe_b64encode(body.encode()).rstrip(b"=")
        sig = hmac.new(self._secret, payload, hashlib.sha256).hexdigest()
        return f"{payload.decode()}.{sig}"

    def inspect(self, token: str) -> dict[str, Any]:
        """Prüft Signatur und Ablauf, verbraucht den Token nicht."""
        try:
            payload, sig = token.rsplit(".", 1)
            expected = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise ApprovalError("invalid token")
            pad = "=" * (-len(payload) % 4)
            rule_id, digest, decision, nonce, exp = json.loads(
                base64.urlsafe_b64decode(payload + pad)
            )
        except (ValueError, TypeError) as exc:
            raise ApprovalError("invalid token") from exc
        if self._now().timestamp() > exp:
            raise ApprovalError("token expired")
        used = self._db.execute(
            "SELECT 1 FROM approval_tokens WHERE nonce = ?", (nonce,)
        ).fetchone()
        if used:
            raise ApprovalError("token already used")
        return {"rule_id": rule_id, "rule_hash": digest, "decision": decision, "nonce": nonce}

    def redeem(self, token: str, rule: Rule | None, actor: str = "webhook") -> Decision:
        """Verbraucht den Token atomar, nur wenn die Regel noch dem signierten Inhalt entspricht."""
        info = self.inspect(token)
        if rule is None or rule.id != info["rule_id"] or rule_hash(rule) != info["rule_hash"]:
            raise ApprovalError("rule changed or missing, approval is stale")
        try:
            self._db.execute(
                "INSERT INTO approval_tokens VALUES (?, ?)",
                (info["nonce"], self._now().isoformat()),
            )
            self._db.commit()
        except sqlite3.IntegrityError as exc:
            raise ApprovalError("token already used") from exc
        decision: Decision = info["decision"]
        self._write(rule.id, info["rule_hash"], decision, actor)
        return decision


_DECISIONS: tuple[Decision, ...] = ("approved", "rejected")
