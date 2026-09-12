from __future__ import annotations

import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

TEST_ROOT = Path("/tmp/handbook-ai-tutor-tests")
TEST_ROOT.mkdir(parents=True, exist_ok=True)
(TEST_ROOT / "storage").mkdir(exist_ok=True)

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_ROOT}/test.db"
os.environ["DATABASE_URL_SYNC"] = f"sqlite:///{TEST_ROOT}/test.db"
os.environ["TASK_BACKEND"] = "inline"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_PATH"] = str(TEST_ROOT / "storage")
os.environ["LLM_DEFAULT_PROVIDER"] = "mock"
# A developer's real .env often pins LLM_PROVIDER_TEXT/EMBED to deepseek /
# siliconflow. Per-modality vars out-rank LLM_DEFAULT_PROVIDER, so without these
# the suite would hit real APIs (slow, flaky, non-deterministic). Pin them too.
os.environ["LLM_PROVIDER_TEXT"] = "mock"
os.environ["LLM_PROVIDER_EMBED"] = "mock"
os.environ["STT_PROVIDER"] = "mock"
os.environ["RAG_PROVIDER"] = "pgvector"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["EMBEDDING_DIM"] = "32"
os.environ["PROVIDERS_CONFIG_PATH"] = str(
    Path(__file__).resolve().parents[1] / "config" / "providers.yaml"
)

# The launchers (start.bat / run_local_backend.sh) put the bundled ffmpeg on PATH
# before importing the app; pytest is started directly, so it has to do the same
# or `tests/helpers.py::tiny_mp4_bytes` cannot build its fixture clip.
_FFMPEG_BIN = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg" / "bin"
if (_FFMPEG_BIN / "ffmpeg.exe").is_file():
    os.environ["PATH"] = f"{_FFMPEG_BIN}{os.pathsep}{os.environ.get('PATH', '')}"

from app.core.config import get_settings  # noqa: E402
from app.core.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

get_settings.cache_clear()


@pytest.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def auth_header(client: AsyncClient, email: str = "ada@example.com", password: str = "password123") -> dict:
    resp = await client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
