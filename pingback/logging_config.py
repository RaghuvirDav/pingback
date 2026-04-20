"""Structured JSON logging with per-request context.

Root logger emits one JSON record per log call. Request-scoped fields
(request_id, user_id, path, method, status, duration_ms) flow through
``contextvars`` so every log line a handler emits during a request is
auto-tagged, including logs from libraries we don't control.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import jsonlogger


request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
path_var: ContextVar[str | None] = ContextVar("path", default=None)
method_var: ContextVar[str | None] = ContextVar("method", default=None)
status_var: ContextVar[int | None] = ContextVar("status", default=None)
duration_ms_var: ContextVar[float | None] = ContextVar("duration_ms", default=None)


class _ContextJsonFormatter(jsonlogger.JsonFormatter):
    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["timestamp"] = self.formatTime(record, self.datefmt)
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        for name, var in (
            ("request_id", request_id_var),
            ("user_id", user_id_var),
            ("path", path_var),
            ("method", method_var),
            ("status", status_var),
            ("duration_ms", duration_ms_var),
        ):
            if log_record.get(name) is None:
                value = var.get()
                if value is not None:
                    log_record[name] = value
        log_record.pop("taskName", None)


def configure_logging(level: int | str = logging.INFO) -> None:
    """Install the JSON formatter on the root logger and uvicorn loggers."""
    formatter = _ContextJsonFormatter(
        "%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        lg.propagate = True
