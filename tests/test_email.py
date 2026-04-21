"""Tests for the email service.

Integration style: we do not mock Resend. We exercise the env-gated no-op
path (no `RESEND_API_KEY`) and confirm the send path builds the expected
payload without hitting the network.
"""
from __future__ import annotations

import pingback.services.email as email_service


def test_send_email_noop_when_key_missing(monkeypatch, caplog):
    monkeypatch.setattr(email_service, "RESEND_API_KEY", "")
    with caplog.at_level("WARNING", logger="pingback.email"):
        result = email_service.send_email(
            to="user@example.com",
            subject="hello",
            text="body",
        )
    assert result is None
    assert any("RESEND_API_KEY not set" in r.message for r in caplog.records)


def test_send_email_requires_body(monkeypatch):
    monkeypatch.setattr(email_service, "RESEND_API_KEY", "test-key")
    import pytest

    with pytest.raises(ValueError):
        email_service.send_email(to="user@example.com", subject="hello")


def test_send_email_builds_payload_and_returns_id(monkeypatch):
    captured: dict = {}

    class FakeEmails:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "fake-msg-id"}

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "test-key")
    monkeypatch.setattr(email_service.resend, "Emails", FakeEmails)
    monkeypatch.setattr(email_service, "EMAIL_FROM_NOREPLY", "Pingback <noreply@usepingback.com>")

    message_id = email_service.send_email(
        to="user@example.com",
        subject="Verify your email",
        text="Click the link",
        html="<a href='x'>Click</a>",
        headers={"X-Category": "verification"},
    )

    assert message_id == "fake-msg-id"
    assert captured["from"] == "Pingback <noreply@usepingback.com>"
    assert captured["to"] == ["user@example.com"]
    assert captured["subject"] == "Verify your email"
    assert captured["text"] == "Click the link"
    assert captured["html"] == "<a href='x'>Click</a>"
    assert captured["headers"] == {"X-Category": "verification"}


def test_send_email_accepts_list_of_recipients(monkeypatch):
    captured: dict = {}

    class FakeEmails:
        @staticmethod
        def send(params):
            captured.update(params)
            return {"id": "id"}

    monkeypatch.setattr(email_service, "RESEND_API_KEY", "test-key")
    monkeypatch.setattr(email_service.resend, "Emails", FakeEmails)

    email_service.send_email(
        to=["a@example.com", "b@example.com"],
        subject="hi",
        text="body",
        from_email="Ops <ops@example.com>",
    )

    assert captured["to"] == ["a@example.com", "b@example.com"]
    assert captured["from"] == "Ops <ops@example.com>"
