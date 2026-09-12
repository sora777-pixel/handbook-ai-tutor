from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.db import engine
from app.services.llm.router import ModelRouter
from app.services.ocr import describe_ocr
from app.services.stt import KNOWN_STT_PROVIDERS

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "llm_default_provider": settings.llm_default_provider,
        "llm_provider_text": settings.llm_provider_text or settings.llm_default_provider,
        "llm_provider_audio": settings.llm_provider_audio or settings.llm_default_provider,
        "llm_provider_embed": settings.llm_provider_embed or settings.llm_default_provider,
        "task_backend": settings.task_backend,
        "storage_backend": settings.storage_backend,
        "stt_provider": settings.stt_provider,
        "stt_providers": list(KNOWN_STT_PROVIDERS),
        "rag_provider": settings.rag_provider,
        "storage_location": (
            settings.s3_endpoint if settings.storage_backend == "minio" else settings.local_storage_path
        ),
        "db": "sqlite" if settings.is_sqlite else "postgresql",
    }


@router.get("/health/llm")
async def health_llm() -> dict:
    """Unauthenticated, redacted summary of LLM routing.

    Useful to confirm at a glance that .env keys were picked up: `ready: false`
    plus `error` tells you exactly which provider/key is missing.
    """
    try:
        r = ModelRouter()
        rows = r.describe_routes()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    missing_keys = sorted(
        {
            row["api_key_env"]
            for row in rows
            if row.get("api_key_env") and row.get("api_key_present") is False
        }
    )
    return {
        "ok": True,
        "default_provider": r.default_name,
        "providers_config_path": r.settings.providers_config_path,
        # provider names only — never the key values
        "tasks": {
            row["task"]: {
                "modality": row["modality"],
                "provider": row["provider"],
                "model": row["model"],
                "ready": row["ready"],
                "error": row["error"],
            }
            for row in rows
        },
        "unconfigured_api_key_env": missing_keys,
        "using_mock": all(row["provider"] == "mock" for row in rows) if rows else False,
        # How .pdf / .png / .jpg uploads actually get turned into text. `engine`
        # other than "rapidocr"/"vision" means OCR is unavailable and image or
        # scanned-PDF uploads will fail with `reason`.
        "ocr": describe_ocr(r),
    }


@router.get("/health/schema")
async def health_schema() -> dict:
    """Which tables exist and, for SQLite, which columns they have."""
    from app.core.migrate import describe_schema

    try:
        return {"dialect": engine.dialect.name, "tables": await describe_schema(engine)}
    except Exception as exc:
        return {"dialect": engine.dialect.name, "error": f"{type(exc).__name__}: {exc}"}
