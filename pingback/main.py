from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from pingback.config import HOST, PORT
from pingback.db.connection import close_database, get_database
from pingback.routes.checks import router as checks_router
from pingback.routes.health import router as health_router
from pingback.routes.monitors import router as monitors_router
from pingback.routes.status import router as status_router
from pingback.routes.users import router as users_router
from pingback.services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pingback")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_database()
    start_scheduler()
    logger.info("Pingback server started on %s:%d", HOST, PORT)
    yield
    stop_scheduler()
    await close_database()
    logger.info("Pingback server shut down")


app = FastAPI(title="Pingback", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(monitors_router)
app.include_router(checks_router)
app.include_router(status_router)
app.include_router(users_router)


if __name__ == "__main__":
    uvicorn.run("pingback.main:app", host=HOST, port=PORT, reload=True)
