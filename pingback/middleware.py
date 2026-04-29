from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from pingback.config import APP_ENV
from pingback.db.connection import get_database
from pingback.logging_config import (
    duration_ms_var,
    method_var,
    path_var,
    request_id_var,
    status_var,
    user_id_var,
)
from pingback.version import VERSION

_access_log = logging.getLogger("pingback.access")

# Routes that don't need audit logging (health checks, static assets)
_SKIP_PATHS = {"/health", "/healthz", "/docs", "/openapi.json", "/redoc"}

# Map HTTP methods to action verbs
_METHOD_ACTIONS = {
    "GET": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


def _parse_resource(path: str) -> tuple[str | None, str | None]:
    """Extract resource_type and resource_id from an /api/... path."""
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "api":
        return None, None
    resource_type = parts[1]  # e.g. "monitors", "users", "audit-log"
    resource_id = parts[2] if len(parts) >= 3 else None
    return resource_type, resource_id


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Log every authenticated request to the audit_log table."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        response = await call_next(request)

        # Only log requests to the API
        if not request.url.path.startswith("/api"):
            return response

        # Extract user from request state (set by auth dependency)
        user_id = None
        if hasattr(request.state, "audit_user_id"):
            user_id = request.state.audit_user_id

        method = request.method
        action = _METHOD_ACTIONS.get(method, method.lower())
        resource_type, resource_id = _parse_resource(request.url.path)
        ip_address = request.client.host if request.client else None

        detail = f"{method} {request.url.path} -> {response.status_code}"

        try:
            db = await get_database()
            await db.execute(
                """INSERT INTO audit_log (id, user_id, action, resource_type, resource_id, ip_address, detail, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    user_id,
                    action,
                    resource_type,
                    resource_id,
                    ip_address,
                    detail,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
        except Exception:
            pass  # Don't let audit failures break the app

        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind request_id/user_id/path/etc. into log context for the life of the request.

    Honors ``X-Request-Id`` from upstream (nginx) and generates a fresh UUID
    when absent. Emits one JSON access log per request with duration_ms so
    CloudWatch Logs Insights can query latency by path.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        incoming = request.headers.get("x-request-id")
        request_id = incoming if incoming else uuid.uuid4().hex
        request.state.request_id = request_id

        rid_token = request_id_var.set(request_id)
        path_token = path_var.set(request.url.path)
        method_token = method_var.set(request.method)
        uid_token = user_id_var.set(None)
        status_token = status_var.set(None)
        dur_token = duration_ms_var.set(None)

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            response.headers["X-Pingback-Version"] = VERSION
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            status_var.set(status_code)
            duration_ms_var.set(duration_ms)
            user_id = getattr(request.state, "audit_user_id", None)
            if user_id is not None:
                user_id_var.set(user_id)
            if request.url.path not in _SKIP_PATHS:
                _access_log.info(
                    "request_completed",
                    extra={
                        "request_id": request_id,
                        "path": request.url.path,
                        "method": request.method,
                        "status": status_code,
                        "duration_ms": duration_ms,
                        "user_id": user_id,
                    },
                )
            request_id_var.reset(rid_token)
            path_var.reset(path_token)
            method_var.reset(method_token)
            user_id_var.reset(uid_token)
            status_var.reset(status_token)
            duration_ms_var.reset(dur_token)


class HTTPSRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect HTTP requests to HTTPS when APP_ENV is 'production'."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if APP_ENV == "production":
            # Check the scheme — also honour X-Forwarded-Proto from reverse proxies
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            if proto == "http":
                url = request.url.replace(scheme="https")
                return RedirectResponse(url=str(url), status_code=301)
        return await call_next(request)
