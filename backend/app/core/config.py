from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> backend/app -> backend -> <repo root>
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

# Lowest -> highest priority. Later files win, and real process environment
# variables always beat all of them (docker compose injects them that way).
ENV_FILES: tuple[Path, ...] = (
    Path(".env"),
    REPO_ROOT / ".env",
    BACKEND_DIR / ".env",
)

# Snapshot taken BEFORE .env is merged in. Anything present here came from the
# real process environment (docker compose, a CI export, start.bat), which must
# stay authoritative over UI-editable runtime overrides — see runtime_paths.py.
_EXTERNAL_ENV_KEYS: frozenset[str] = frozenset(os.environ)


def env_var_is_external(key: str) -> bool:
    """True when `key` was set by the real process environment, not by .env.

    Used to decide whether a value may be re-pointed from the settings page:
    a container that pins LOCAL_STORAGE_PATH must not be silently redirected by
    a click in a browser.
    """
    return key in _EXTERNAL_ENV_KEYS


def export_dotenv_to_environ() -> list[str]:
    """Mirror .env values into os.environ.

    Provider implementations read vendor keys with `os.environ[api_key_env]`,
    but pydantic-settings only parses .env into Settings fields — so a key that
    exists in .env used to be invisible to every provider, and every call went
    out unauthenticated (or silently fell back to MockLLM).

    Every *other* variable keeps `setdefault`, so genuine environment variables
    (docker compose, CI shell exports) stay authoritative. API keys are the one
    exception, and deliberately so:

    A vendor key left over from an old `setx`/machine-wide install shadows the
    file and is invisible in the UI — the routing page only ever shows
    ``api_key_present: true``. Observed for real: a dead machine-level
    ``DASHSCOPE_API_KEY`` beat the working DeepSeek key that was actually filled
    in, so every task resolved to dashscope and returned 401, while `.env`
    looked correct. `.env` is where this project documents key setup, so `.env`
    has to win; an *empty* value in `.env` clears the inherited variable
    outright, which is what makes "I left it blank" mean "not configured".

    Set ``LLM_ENV_PRECEDENCE=process`` to restore the old behaviour for
    containers/CI that inject secrets through the real process environment.
    """
    merged: dict[str, str] = {}
    for path in ENV_FILES:
        if not path.exists():
            continue
        for key, value in (dotenv_values(path, encoding="utf-8") or {}).items():
            if value is not None:
                merged[key] = value

    dotenv_wins = os.environ.get("LLM_ENV_PRECEDENCE", "dotenv").strip().lower() != "process"
    for key, value in merged.items():
        if dotenv_wins and "API_KEY" in key:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)
    return sorted(k for k in merged if "API_KEY" in k or k.startswith("LLM_"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=tuple(str(p) for p in ENV_FILES),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI Learning Tutor"
    secret_key: str = "change-me-in-production-use-a-long-random-string"
    debug: bool = False
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"

    database_url: str = "sqlite+aiosqlite:///./tutor.db"
    database_url_sync: str = "sqlite:///./tutor.db"

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint: str = "http://localhost:9000"
    s3_public_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "sources"
    s3_region: str = "us-east-1"
    s3_secure: bool = False

    task_backend: str = "inline"  # arq | inline
    storage_backend: str = "local"  # minio | local
    # Absolute, and deliberately inside the project folder: every account gets
    # <root>/<user_id>/{profile,records,resources} without anyone having to edit
    # a file. Uploaded originals live in that account's resources/ sub-folder.
    # The settings page can re-point it per deployment (see runtime_paths.py).
    local_storage_path: str = str(REPO_ROOT / "data" / "storage")

    rag_provider: str = "llamaindex"  # llamaindex | pgvector
    embedding_dim: int = 1024
    stt_provider: str = "mock"  # mock | faster_whisper | siliconflow
    stt_siliconflow_model: str = "FunAudioLLM/SenseVoiceSmall"
    stt_siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    # Whisper model size for faster-whisper. small 对中文讲课 + 夹杂外语单词的
    # 识别质量明显好于 base，CPU int8 也能跑；需要更快可降到 base。
    stt_whisper_model: str = "small"
    # VAD 过滤静音段，讲课音频里大段停顿/翻页噪声会显著拖慢并干扰识别。
    stt_whisper_vad: bool = True

    # "" = auto: pick the first provider whose API key is present in the
    # environment, else stay on mock. Set it explicitly (including "mock") to
    # pin the behaviour.
    llm_default_provider: str = ""
    # Per-modality routing (text / audio / embedding). Empty = fall back to
    # llm_default_provider. Lets PDF text work on one vendor while video/speech
    # work on another.
    llm_provider_text: str = ""
    llm_provider_audio: str = ""
    llm_provider_embed: str = ""
    llm_provider_vision: str = ""
    # Optional JSON map task -> provider, highest priority, e.g.
    # LLM_TASK_ROUTES={"tutor":"deepseek","summarize":"dashscope"}
    llm_task_routes: str = ""
    providers_config_path: str = str(Path(__file__).resolve().parents[2] / "config" / "providers.yaml")

    @field_validator("local_storage_path")
    @classmethod
    def _resolve_storage_root(cls, value: str) -> str:
        """Always yield a usable absolute path.

        This repo's .env came from a Docker template and carried
        `LOCAL_STORAGE_PATH=/data/storage`. On Windows that is *not* absolute, so
        `mkdir` created a `data/storage` folder relative to whatever the cwd
        happened to be — a different place depending on how you launched the app.
        Blank now means the project default, and a non-absolute value is anchored
        to the project root instead of the cwd.
        """
        raw = (value or "").strip()
        if not raw:
            return str(REPO_ROOT / "data" / "storage")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return str((REPO_ROOT / path.as_posix().lstrip("/\\")).resolve())
        return str(path)

    @field_validator("providers_config_path")
    @classmethod
    def _blank_means_bundled(cls, value: str) -> str:
        """A blank value must mean "use the in-repo file", not ".".

        `.env.example` tells local runs to leave this blank. Passing "" straight
        through produced `Path("")`, which resolves to the current directory —
        that *exists*, so the loader happily tried to open a directory and every
        route died with `PermissionError: [Errno 13] Permission denied: '.'`.
        """
        return value.strip() or str(BACKEND_DIR / "config" / "providers.yaml")

    # --- OCR (images and scanned PDFs) ---
    # auto     -> local OCR engine when installed, else a vision LLM, else error
    # rapidocr -> local offline ONNX OCR (no API key needed)
    # vision   -> send the page image to a vision-capable LLM
    # none     -> refuse image/scanned input with a clear message
    ocr_provider: str = "auto"
    # Language of the recognition model. "auto" tries the bundled Chinese+English
    # model first and re-runs with the Japanese model when confidence is low.
    ocr_lang: str = "auto"
    # Hard cap on how many pages/images we transcribe per source.
    ocr_max_pages: int = 40
    # Longest edge (px) an image is scaled to before being sent to a vision LLM.
    ocr_vision_max_side: int = 1800

    jwt_expire_minutes: int = 60 * 24 * 7
    jwt_algorithm: str = "HS256"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


# Run at import time so every provider sees .env keys via os.environ.
DOTENV_KEYS: list[str] = export_dotenv_to_environ()


@lru_cache
def get_settings() -> Settings:
    return Settings()
