from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.migrate import ensure_sqlite_schema, recover_interrupted_tasks
from app.core.runtime_paths import apply_overrides
from app.models import Base
from app.services.storage import get_storage

# Before anything reads settings: fold the paths saved from the settings page
# (storage root, providers.yaml) back in. Genuinely-set process environment
# variables still win over these.
apply_overrides()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.is_sqlite:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # Bring pre-existing SQLite databases up to the current schema without
        # dropping anything: create_all never alters an existing table.
        applied = await ensure_sqlite_schema(engine)
        if applied:
            print(f"[migrate] sqlite schema upgraded: {', '.join(applied)}")
        # A task owned by a process that is gone can never finish; fail it now so
        # the UI stops showing a progress bar that will never move.
        await recover_interrupted_tasks(engine)
    try:
        await get_storage().ensure_ready()
    except Exception:
        pass
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(api_router)
    return app


app = create_app()
