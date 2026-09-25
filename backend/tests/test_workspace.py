"""Per-user workspace folders: creation, layout, isolation, and records archive.

The folder is the user's own copy of what the platform holds: profile (account
metadata), records (study-record snapshots) and resources (uploaded originals).
The password must never appear in it — that is asserted, not assumed.
"""
from __future__ import annotations

import json
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.chunk import DocumentChunk
from app.models.source import Source
from app.models.user import User
from app.services.llm.mock import mock_embed_vectors
from app.services.workspace import SUBDIRS, storage_root, workspace_dir
from tests.conftest import auth_header
from tests.helpers import minimal_pdf_bytes

PASSWORD = "password123"


async def _me(client: AsyncClient, headers: dict) -> dict:
    resp = await client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _seed_ready_source(email: str) -> str:
    """A ready source with one chunk, so quiz generation works offline."""
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        source = Source(
            user_id=user.id,
            filename="notes.pdf",
            content_type="application/pdf",
            kind="pdf",
            storage_key="memory/notes.pdf",
            byte_size=1,
            status="ready",
            title="Notes",
        )
        session.add(source)
        await session.flush()
        text = "Photosynthesis uses chlorophyll to capture light and produce glucose."
        session.add(
            DocumentChunk(
                source_id=source.id,
                user_id=user.id,
                ordinal=0,
                content=text,
                page_number=1,
                locator="p.1",
                embedding=mock_embed_vectors([text], 32)[0],
            )
        )
        source_id = str(source.id)
        await session.commit()
    return source_id


# ------------------------------------------------------------------ creation


@pytest.mark.asyncio
async def test_register_provisions_the_three_folder_workspace(client: AsyncClient) -> None:
    headers = await auth_header(client, email="ws-reg@example.com", password=PASSWORD)
    me = await _me(client, headers)

    root = workspace_dir(UUID(me["id"]))
    assert root.is_dir(), root
    for name in SUBDIRS:
        assert (root / name).is_dir(), f"missing {name}/ under {root}"

    account = json.loads((root / "profile" / "account.json").read_text(encoding="utf-8"))
    assert account["user_id"] == me["id"]
    assert account["email"] == "ws-reg@example.com"
    assert account["schema_version"] >= 1
    assert sorted(account["folders"]) == sorted(SUBDIRS)


@pytest.mark.asyncio
async def test_account_file_never_carries_a_password(client: AsyncClient) -> None:
    """Neither the plaintext nor the bcrypt hash may be copied into the folder."""
    headers = await auth_header(client, email="ws-secret@example.com", password=PASSWORD)
    me = await _me(client, headers)
    root = workspace_dir(UUID(me["id"]))

    async with SessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.email == "ws-secret@example.com"))
        ).scalar_one()
        stored_hash = user.hashed_password
    assert stored_hash.startswith("$2"), stored_hash  # bcrypt, as designed

    blob = "\n".join(
        path.read_text(encoding="utf-8") for path in root.rglob("*.json")
    )
    assert PASSWORD not in blob, "plaintext password leaked into the workspace folder"
    assert stored_hash not in blob, "bcrypt hash copied into the workspace folder"


@pytest.mark.asyncio
async def test_login_recreates_a_hand_deleted_workspace(client: AsyncClient) -> None:
    """The provisioning call is idempotent, so a missing folder heals on login."""
    import shutil

    headers = await auth_header(client, email="ws-heal@example.com", password=PASSWORD)
    me = await _me(client, headers)
    root = workspace_dir(UUID(me["id"]))
    assert root.is_dir()

    shutil.rmtree(root)
    assert not root.exists()

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "ws-heal@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    for name in SUBDIRS:
        assert (root / name).is_dir(), f"{name}/ was not restored"


@pytest.mark.asyncio
async def test_workspace_is_private_to_each_account(client: AsyncClient) -> None:
    a = await auth_header(client, email="ws-a@example.com", password=PASSWORD)
    b = await auth_header(client, email="ws-b@example.com", password=PASSWORD)
    me_a = await _me(client, a)
    me_b = await _me(client, b)

    assert me_a["id"] != me_b["id"]
    dir_a = workspace_dir(UUID(me_a["id"]))
    dir_b = workspace_dir(UUID(me_b["id"]))
    assert dir_a != dir_b
    assert dir_a.is_dir() and dir_b.is_dir()

    # Each account only ever sees its own folder.
    seen_a = (await client.get("/api/v1/system/workspace", headers=a)).json()
    seen_b = (await client.get("/api/v1/system/workspace", headers=b)).json()
    assert seen_a["user_id"] == me_a["id"]
    assert seen_b["user_id"] == me_b["id"]
    assert seen_a["path"] == str(dir_a)
    assert seen_b["path"] == str(dir_b)


# ------------------------------------------------------------------ resources


@pytest.mark.asyncio
async def test_uploaded_original_lands_in_the_owners_resources_folder(client: AsyncClient) -> None:
    headers = await auth_header(client, email="ws-up@example.com", password=PASSWORD)
    me = await _me(client, headers)

    files = {"file": ("lesson.pdf", minimal_pdf_bytes(), "application/pdf")}
    resp = await client.post("/api/v1/sources/upload", headers=headers, files=files)
    assert resp.status_code == 200, resp.text
    source_id = resp.json()["id"]

    stored = workspace_dir(UUID(me["id"])) / "resources" / source_id / "lesson.pdf"
    assert stored.is_file(), f"original not under resources/: {stored}"
    assert stored.stat().st_size > 0
    # And it really is inside the account's own folder, not a shared directory.
    assert workspace_dir(UUID(me["id"])) in stored.parents


@pytest.mark.asyncio
async def test_deleting_the_last_source_keeps_the_three_folders(client: AsyncClient) -> None:
    """An empty-directory prune must not leave the account without its folder."""
    headers = await auth_header(client, email="ws-del@example.com", password=PASSWORD)
    me = await _me(client, headers)

    files = {"file": ("lesson.pdf", minimal_pdf_bytes(), "application/pdf")}
    resp = await client.post("/api/v1/sources/upload", headers=headers, files=files)
    source_id = resp.json()["id"]

    dele = await client.delete(f"/api/v1/sources/{source_id}?delete_files=true", headers=headers)
    assert dele.status_code == 200, dele.text

    root = workspace_dir(UUID(me["id"]))
    for name in SUBDIRS:
        assert (root / name).is_dir(), f"{name}/ disappeared after the last delete"


# ------------------------------------------------------------------ records


@pytest.mark.asyncio
async def test_quiz_attempt_is_archived_into_records(client: AsyncClient) -> None:
    email = "ws-quiz@example.com"
    headers = await auth_header(client, email=email, password=PASSWORD)
    me = await _me(client, headers)
    source_id = await _seed_ready_source(email)

    quiz = (
        await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)
    ).json()
    answers = [{"question_id": q["id"], "selected_index": 0} for q in quiz["questions"]]
    attempt = await client.post(
        f"/api/v1/quizzes/{quiz['id']}/attempt", headers=headers, json={"answers": answers}
    )
    assert attempt.status_code == 200, attempt.text

    records = workspace_dir(UUID(me["id"])) / "records"
    quiz_file = records / "quiz_records.json"
    assert quiz_file.is_file(), f"成绩未归档: {sorted(p.name for p in records.glob('*'))}"

    payload = json.loads(quiz_file.read_text(encoding="utf-8"))
    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["quiz_id"] == quiz["id"]
    assert payload["attempts"][0]["results"], "作答明细应随成绩一并存档"
    assert len(payload["quizzes"]) == 1

    overview = json.loads((records / "overview.json").read_text(encoding="utf-8"))
    assert overview["counts"]["attempts"] == 1
    assert overview["counts"]["quizzes"] == 1


@pytest.mark.asyncio
async def test_export_endpoint_writes_every_record_snapshot(client: AsyncClient) -> None:
    headers = await auth_header(client, email="ws-export@example.com", password=PASSWORD)
    me = await _me(client, headers)

    resp = await client.post("/api/v1/system/workspace/export", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["exported"]) >= {"sources", "notes", "quizzes", "attempts", "token_usages"}
    assert body["workspace"]["exists"] is True

    records = workspace_dir(UUID(me["id"])) / "records"
    for name in ("study_notes.json", "quiz_records.json", "tutor_qa.json", "usage.json", "overview.json"):
        assert (records / name).is_file(), f"missing records/{name}"


@pytest.mark.asyncio
async def test_workspace_report_describes_the_three_folders(client: AsyncClient) -> None:
    headers = await auth_header(client, email="ws-report@example.com", password=PASSWORD)
    me = await _me(client, headers)

    files = {"file": ("lesson.pdf", minimal_pdf_bytes(), "application/pdf")}
    await client.post("/api/v1/sources/upload", headers=headers, files=files)

    report = (await client.get("/api/v1/system/workspace", headers=headers)).json()
    assert report["user_id"] == me["id"]
    assert report["exists"] is True
    for name in SUBDIRS:
        assert report["subdirs"][name]["exists"] is True
    assert report["subdirs"]["resources"]["files"] >= 1
    assert report["subdirs"]["profile"]["files"] >= 1
    assert report["total_bytes"] > 0
    # The root that holds every account is the configured storage root.
    assert str(storage_root()) in report["path"]


@pytest.mark.asyncio
async def test_workspace_endpoints_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/system/workspace")).status_code in (401, 403)
    assert (await client.post("/api/v1/system/workspace/export")).status_code in (401, 403)