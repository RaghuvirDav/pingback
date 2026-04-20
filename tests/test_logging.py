"""Structured-logging and request-id integration tests (MAK-57)."""
from __future__ import annotations

import io
import json
import logging
import re
import uuid

import pytest
from fastapi import APIRouter


UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _capture_logs(app_ctx) -> io.StringIO:
    """Attach a StringIO handler using the same JSON formatter as the app."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    handler.setFormatter(root.handlers[0].formatter)
    root.addHandler(handler)
    return buf


def _json_lines(buf: io.StringIO) -> list[dict]:
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    parsed = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            pytest.fail(f"Non-JSON log line emitted: {line!r}")
    return parsed


def test_log_output_is_json(app_ctx, client):
    buf = _capture_logs(app_ctx)
    r = client.get("/health")
    assert r.status_code == 200

    records = _json_lines(buf)
    assert records, "expected at least one log record"
    for rec in records:
        assert "timestamp" in rec
        assert "level" in rec
        assert "logger" in rec


def test_request_id_roundtrips_on_header(app_ctx, client):
    incoming = "req-test-" + uuid.uuid4().hex[:8]
    r = client.get("/api/privacy-policy", headers={"X-Request-Id": incoming})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == incoming


def test_request_id_generated_when_missing(app_ctx, client):
    r = client.get("/api/privacy-policy")
    assert r.status_code == 200
    rid = r.headers["x-request-id"]
    assert UUID_HEX_RE.match(rid), f"expected 32-char hex request id, got {rid!r}"


def test_access_log_contains_request_context(app_ctx, client):
    buf = _capture_logs(app_ctx)
    rid = "rid-" + uuid.uuid4().hex[:8]
    r = client.get("/api/privacy-policy", headers={"X-Request-Id": rid})
    assert r.status_code == 200

    records = _json_lines(buf)
    completed = [
        rec for rec in records if rec.get("message") == "request_completed"
    ]
    assert completed, f"no request_completed record in {records}"
    rec = completed[-1]
    assert rec["request_id"] == rid
    assert rec["path"] == "/api/privacy-policy"
    assert rec["method"] == "GET"
    assert rec["status"] == 200
    assert isinstance(rec["duration_ms"], (int, float))
    assert rec["duration_ms"] >= 0


def test_unhandled_exception_logs_traceback_with_context(app_ctx):
    """A 500 surfaces as a JSON log record with request_id, path, and a traceback."""
    from starlette.testclient import TestClient

    router = APIRouter()

    @router.get("/__boom")
    async def boom():
        raise RuntimeError("structured-logging-sentinel-boom")

    app_ctx.app.include_router(router)

    buf = _capture_logs(app_ctx)
    rid = "boom-" + uuid.uuid4().hex[:8]
    with TestClient(app_ctx.app, raise_server_exceptions=False) as c:
        r = c.get("/__boom", headers={"X-Request-Id": rid})
    assert r.status_code == 500

    records = _json_lines(buf)
    errs = [rec for rec in records if rec.get("message") == "unhandled_exception"]
    assert errs, f"no unhandled_exception record in {records}"
    rec = errs[-1]
    assert rec["level"] == "ERROR"
    assert rec["path"] == "/__boom"
    assert rec["method"] == "GET"
    assert rec["request_id"] == rid
    assert rec["exception_type"] == "RuntimeError"
    assert "structured-logging-sentinel-boom" in rec.get("exc_info", "")
    assert "Traceback" in rec.get("exc_info", "")
