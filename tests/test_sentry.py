"""Sentry init + PII scrubbing + /debug/boom gating (MAK-58)."""
from __future__ import annotations

import importlib
import sys

import pytest


def _reimport_pingback():
    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]
    return importlib.import_module("pingback.main")


def test_sentry_disabled_when_dsn_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    from pingback.sentry_init import init_sentry

    assert init_sentry() is False


def test_sentry_enabled_when_dsn_set(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv(
        "SENTRY_DSN",
        "https://public@o0.ingest.sentry.io/0",
    )

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]

    captured: dict = {}

    import sentry_sdk

    original_init = sentry_sdk.init

    def fake_init(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(sentry_sdk, "init", fake_init)

    from pingback.sentry_init import init_sentry

    try:
        assert init_sentry() is True
    finally:
        sentry_sdk.init = original_init

    assert captured["dsn"] == "https://public@o0.ingest.sentry.io/0"
    assert captured["send_default_pii"] is False
    assert captured["traces_sample_rate"] == 0.1
    assert callable(captured["before_send"])


def test_before_send_scrubs_pii(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("APP_ENV", "development")

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]

    from pingback.sentry_init import _scrub_event

    event = {
        "user": {
            "email": "alice@example.com",
            "ip_address": "10.0.0.1",
            "username": "alice",
            "id": "user-123",
        },
        "request": {
            "cookies": {"session": "secret"},
            "headers": {
                "Authorization": "Bearer s3cret",
                "Cookie": "session=secret",
                "User-Agent": "pytest",
                "X-Api-Key": "abc",
            },
            "env": {"REMOTE_ADDR": "10.0.0.1", "SERVER_NAME": "pingback"},
        },
    }

    scrubbed = _scrub_event(event, {})

    assert scrubbed is not None
    assert "email" not in scrubbed["user"]
    assert "ip_address" not in scrubbed["user"]
    assert "username" not in scrubbed["user"]
    assert scrubbed["user"]["id"] == "user-123"
    assert "cookies" not in scrubbed["request"]
    assert "Authorization" not in scrubbed["request"]["headers"]
    assert "Cookie" not in scrubbed["request"]["headers"]
    assert "X-Api-Key" not in scrubbed["request"]["headers"]
    assert scrubbed["request"]["headers"]["User-Agent"] == "pytest"
    assert "REMOTE_ADDR" not in scrubbed["request"]["env"]
    assert scrubbed["request"]["env"]["SERVER_NAME"] == "pingback"


def test_before_send_tags_request_id(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))

    for mod in list(sys.modules):
        if mod == "pingback" or mod.startswith("pingback."):
            del sys.modules[mod]

    from pingback.logging_config import request_id_var
    from pingback.sentry_init import _scrub_event

    token = request_id_var.set("rid-abc")
    try:
        scrubbed = _scrub_event({}, {})
    finally:
        request_id_var.reset(token)

    assert scrubbed["tags"]["request_id"] == "rid-abc"


def test_boom_route_not_mounted_by_default(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("DEBUG_BOOM_ENABLED", raising=False)

    main = _reimport_pingback()
    with TestClient(main.app) as c:
        r = c.get("/debug/boom")
    assert r.status_code == 404


def test_boom_route_enabled_raises_500(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DB_PATH", str(tmp_path / "p.db"))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("DEBUG_BOOM_ENABLED", "1")

    main = _reimport_pingback()
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.get("/debug/boom")
    assert r.status_code == 500
