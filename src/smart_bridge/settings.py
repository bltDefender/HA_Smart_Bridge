"""Konfiguration aus Umgebung; Secrets kommen aus Docker Secrets (`*_FILE`)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _secret(env: Mapping[str, str], name: str) -> str | None:
    file_var = env.get(f"{name}_FILE")
    if file_var:
        return Path(file_var).read_text(encoding="utf-8").strip()
    return env.get(name) or None


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    ha_url: str | None
    ha_token: str | None = None
    ha_mcp_url: str | None = None
    mqtt_host: str | None = None
    mqtt_port: int = 1883
    mqtt_user: str | None = None
    mqtt_password: str | None = None
    qdrant_url: str | None = None
    embed_model: str = "intfloat/multilingual-e5-small"
    embed_cache_dir: Path | None = None
    state_history_days: int = 14
    mcp_server_enabled: bool = False
    mcp_server_token: str | None = None
    latitude: float = 52.4
    longitude: float = 13.1
    tz: str = "Europe/Berlin"
    holiday_country: str = "DE"
    holiday_subdiv: str | None = "BB"
    log_level: str = "INFO"
    dry_run: bool = False
    executor: str = "mcp"
    config_dir: Path = Path("config")
    data_dir: Path = Path("data")
    approval_notify_service: str | None = None
    bridge_public_url: str | None = None
    approval_secret: str | None = None
    approval_ttl_hours: int = 24
    rule_scan_seconds: float = 2.0

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        executor = e.get("EXECUTOR", "mcp")
        if executor not in {"mcp", "ws", "auto"}:
            raise ValueError(f"EXECUTOR must be mcp|ws|auto, got {executor!r}")
        tz = e.get("TZ", "Europe/Berlin")
        ZoneInfo(tz)  # früh scheitern bei ungültiger Zeitzone
        return cls(
            ha_url=e.get("HA_URL") or None,
            ha_token=_secret(e, "HA_TOKEN"),
            ha_mcp_url=e.get("HA_MCP_URL") or None,
            mqtt_host=e.get("MQTT_HOST") or None,
            mqtt_port=int(e.get("MQTT_PORT", "1883")),
            mqtt_user=e.get("MQTT_USER") or None,
            mqtt_password=_secret(e, "MQTT_PASSWORD"),
            qdrant_url=e.get("QDRANT_URL") or None,
            embed_model=e.get("EMBED_MODEL", "intfloat/multilingual-e5-small"),
            embed_cache_dir=Path(e["EMBED_CACHE_DIR"]) if e.get("EMBED_CACHE_DIR") else None,
            state_history_days=int(e.get("STATE_HISTORY_DAYS", "14")),
            mcp_server_enabled=_bool(e.get("MCP_SERVER_ENABLED"), False),
            mcp_server_token=_secret(e, "MCP_SERVER_TOKEN"),
            latitude=float(e.get("LATITUDE", "52.4")),
            longitude=float(e.get("LONGITUDE", "13.1")),
            tz=tz,
            holiday_country=e.get("HOLIDAY_COUNTRY", "DE"),
            holiday_subdiv=e.get("HOLIDAY_SUBDIV", "BB") or None,
            log_level=e.get("LOG_LEVEL", "INFO").upper(),
            dry_run=_bool(e.get("DRY_RUN"), False),
            executor=executor,
            config_dir=Path(e.get("CONFIG_DIR", "config")),
            data_dir=Path(e.get("DATA_DIR", "data")),
            approval_notify_service=e.get("APPROVAL_NOTIFY_SERVICE") or None,
            bridge_public_url=(e.get("BRIDGE_PUBLIC_URL") or "").rstrip("/") or None,
            approval_secret=_secret(e, "APPROVAL_SECRET"),
            approval_ttl_hours=int(e.get("APPROVAL_TTL_HOURS", "24")),
            rule_scan_seconds=float(e.get("RULE_SCAN_SECONDS", "2")),
        )
