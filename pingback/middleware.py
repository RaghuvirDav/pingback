from __future__ import annotations

import uuid
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from pingback.db.connection import get_database

# Routes that don't need audit logging (health checks, static assets)
_SKIP_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}

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
