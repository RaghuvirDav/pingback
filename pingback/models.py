from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, HttpUrl


MonitorStatus = Literal["active", "paused"]
CheckStatus = Literal["up", "down", "error"]


class CreateMonitorInput(BaseModel):
    name: str
    url: HttpUrl
    interval_seconds: int = 300


class Monitor(BaseModel):
    id: str
    user_id: str
    name: str
    url: str
    interval_seconds: int
    status: MonitorStatus
    created_at: str
    updated_at: str


class CheckResult(BaseModel):
    id: str
    monitor_id: str
    status: CheckStatus
    status_code: Optional[int]
    response_time_ms: Optional[int]
    error: Optional[str]
    checked_at: str


class MonitorWithLastCheck(Monitor):
    last_check: Optional[CheckResult] = None
