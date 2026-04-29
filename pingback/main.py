from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from pingback.config import DEBUG_BOOM_ENABLED, HOST, PORT, validate_secrets
from pingback.csrf import CSRFCookieMiddleware, register_csrf_globals
from pingback.db.connection import close_database, get_database
from pingback.logging_config import configure_logging
from pingback.middleware import (
    AuditLogMiddleware,
    HTTPSRedirectMiddleware,
    RequestContextMiddleware,
)
from pingback.routes.admin import router as admin_router
from pingback.routes.audit import router as audit_router
from pingback.routes.billing import router as billing_router
from pingback.routes.checks import router as checks_router
from pingback.routes.dashboard import router as dashboard_router
from pingback.routes.debug import router as debug_router
from pingback.routes.digest import router as digest_router
from pingback.routes.health import router as health_router
from pingback.routes.monitors import router as monitors_router
from pingback.routes.users import router as users_router
from pingback.sentry_init import init_sentry
from pingback.services.scheduler import start_scheduler, stop_scheduler

configure_logging()
init_sentry()
logger = logging.getLogger("pingback")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # MAK-167: refuse to boot in production with missing/default secrets so a
    # silent fallback to dev keys can never reach a real deployment.
    validate_secrets()
    await get_database()
    start_scheduler()
    logger.info("Pingback server started on %s:%d", HOST, PORT)
    yield
    stop_scheduler()
    await close_database()
    logger.info("Pingback server shut down")


app = FastAPI(title="Pingback", version="0.1.0", lifespan=lifespan)

app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)

_templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates")
)
register_csrf_globals(_templates)


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: StarletteHTTPException):
    return _templates.TemplateResponse(
        request, "404.html", status_code=404
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "unhandled_exception",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "path": request.url.path,
            "method": request.method,
            "exception_type": exc.__class__.__name__,
        },
    )
    return _templates.TemplateResponse(
        request, "500.html", status_code=500
    )


app.add_middleware(AuditLogMiddleware)
app.add_middleware(CSRFCookieMiddleware)
app.add_middleware(HTTPSRedirectMiddleware)
app.add_middleware(RequestContextMiddleware)
app.include_router(health_router)
app.include_router(monitors_router)
app.include_router(checks_router)
app.include_router(users_router)
app.include_router(audit_router)
app.include_router(digest_router)
app.include_router(billing_router)
app.include_router(dashboard_router)
app.include_router(admin_router)
if DEBUG_BOOM_ENABLED:
    app.include_router(debug_router)


if __name__ == "__main__":
    uvicorn.run(
        "pingback.main:app",
        host=HOST,
        port=PORT,
        reload=True,
        log_config=None,
    )
