from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from smart_bridge.settings import Settings, parse_frame_ancestors
from smart_bridge.ui.static import mount_ui, resolve_dist

from .test_mcp_server import running_bridge


def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>ui</title>")
    (d / "assets" / "app.js").write_text("console.log(1)")
    return d


async def fetch(app: FastAPI, method: str, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, path, follow_redirects=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("self", "'self'"),
        ("'self' http://homeassistant.local:8123", "'self' http://homeassistant.local:8123"),
        ("https://ha.example.org *.lan", "https://ha.example.org *.lan"),
    ],
)
def test_frame_ancestors_accepted(raw: str, expected: str) -> None:
    assert parse_frame_ancestors(raw) == expected


@pytest.mark.parametrize("raw", ["", "self; script-src *", "a,b", "x\r\nSet-Cookie: a=b"])
def test_frame_ancestors_rejects_injection(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_frame_ancestors(raw)


def test_settings_read_ui_env() -> None:
    cfg = Settings.from_env({"UI_FRAME_ANCESTORS": "self http://ha.lan:8123", "UI_DIST_DIR": "/x"})
    assert cfg.ui_frame_ancestors == "'self' http://ha.lan:8123"
    assert cfg.ui_dist_dir == Path("/x")


def test_resolve_dist_needs_index(tmp_path: Path) -> None:
    assert resolve_dist(tmp_path) is None
    assert resolve_dist(dist(tmp_path)) == tmp_path / "dist"


async def test_assets_served_read_only_with_csp(tmp_path: Path) -> None:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    assert mount_ui(app, dist(tmp_path), "self http://ha.lan:8123")
    index = await fetch(app, "GET", "/ui")
    assert index.status_code == 200 and "<title>ui</title>" in index.text
    policy = index.headers["content-security-policy"]
    assert "frame-ancestors 'self' http://ha.lan:8123" in policy
    assert "script-src 'self'" in policy and "connect-src 'self'" in policy
    assert (await fetch(app, "HEAD", "/ui/assets/app.js")).status_code == 200
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        res = await fetch(app, method, "/ui/")
        assert res.status_code == 405, method
    assert "content-security-policy" not in (await fetch(app, "GET", "/healthz")).headers


async def test_missing_build_keeps_api_and_adds_no_route(tmp_path: Path) -> None:
    app = FastAPI()
    assert not mount_ui(app, None, "self")
    assert (await fetch(app, "GET", "/ui/")).status_code == 404


async def test_real_bridge_serves_ui_and_api_with_same_csp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UI_DIST_DIR", str(dist(tmp_path)))
    monkeypatch.setenv("UI_FRAME_ANCESTORS", "self http://ha.lan:8123")
    async with running_bridge(tmp_path, monkeypatch) as (url, _), httpx.AsyncClient() as c:
        page = await c.get(f"{url}/ui/", follow_redirects=True)
        assert page.status_code == 200 and "<title>ui</title>" in page.text
        api = await c.get(f"{url}/ui/api/rules")
        assert api.status_code == 200  # API-Router hat Vorrang vor dem statischen Mount
        for res in (page, api):
            assert (
                "frame-ancestors 'self' http://ha.lan:8123"
                in res.headers["content-security-policy"]
            )
        assert (await c.post(f"{url}/ui/")).status_code == 405
