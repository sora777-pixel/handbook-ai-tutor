from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.chunk import DocumentChunk
from app.models.quiz import Quiz
from app.models.source import Source
from app.models.user import User
from app.services.llm.mock import mock_embed_vectors
from tests.conftest import auth_header


@pytest.mark.asyncio
async def test_quiz_generate_and_attempt_mock(client: AsyncClient) -> None:
    headers = await auth_header(client, email="quiz@example.com")
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == "quiz@example.com"))).scalar_one()
        source = Source(
            user_id=user.id,
            filename="notes.pdf",
            content_type="application/pdf",
            kind="pdf",
            storage_key="memory/quiz.pdf",
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

    gen = await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)
    assert gen.status_code == 200, gen.text
    quiz = gen.json()
    assert quiz["questions"]
    # answers are hidden until submit
    assert "correct_index" not in quiz["questions"][0]

    answers = [{"question_id": q["id"], "selected_index": 0} for q in quiz["questions"]]
    attempt = await client.post(f"/api/v1/quizzes/{quiz['id']}/attempt", headers=headers, json={"answers": answers})
    assert attempt.status_code == 200, attempt.text
    body = attempt.json()
    assert 0 <= body["score"] <= 1
    assert body["results"]
    assert "correct_index" in body["results"][0]
    assert "explanation" in body["results"][0]


@pytest.mark.asyncio
async def test_quiz_regenerate_keeps_prior_history(client: AsyncClient) -> None:
    headers = await auth_header(client, email="quiz-hist@example.com")
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == "quiz-hist@example.com"))).scalar_one()
        source = Source(
            user_id=user.id,
            filename="notes.pdf",
            content_type="application/pdf",
            kind="pdf",
            storage_key="memory/quiz-hist.pdf",
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

    first = (await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)).json()
    answers = [{"question_id": q["id"], "selected_index": 0} for q in first["questions"]]
    attempt = await client.post(
        f"/api/v1/quizzes/{first['id']}/attempt", headers=headers, json={"answers": answers}
    )
    assert attempt.status_code == 200, attempt.text

    second = await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["id"] != first["id"]

    listed = await client.get(f"/api/v1/sources/{source_id}/quizzes", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = [q["id"] for q in listed.json()["quizzes"]]
    assert first["id"] in ids
    assert second.json()["id"] in ids
    assert listed.json()["quizzes"][0]["id"] == second.json()["id"]

    latest = await client.get(f"/api/v1/sources/{source_id}/quiz", headers=headers)
    assert latest.json()["id"] == second.json()["id"]

    old = await client.get(f"/api/v1/sources/{source_id}/quiz?quiz_id={first['id']}", headers=headers)
    assert old.status_code == 200
    assert old.json()["id"] == first["id"]

    records = await client.get(f"/api/v1/sources/{source_id}/quiz/records", headers=headers)
    assert records.status_code == 200
    assert any(r["quiz_id"] == first["id"] for r in records.json()["records"])


@pytest.mark.asyncio
async def test_wrong_question_retry_grades_only_failed_items(client: AsyncClient) -> None:
    headers = await auth_header(client, email="quiz-retry@example.com")
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == "quiz-retry@example.com"))).scalar_one()
        source = Source(
            user_id=user.id,
            filename="notes.pdf",
            content_type="application/pdf",
            kind="pdf",
            storage_key="memory/quiz-retry.pdf",
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

    quiz = (await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)).json()
    choice = [q for q in quiz["questions"] if q["question_type"] == "choice"]
    assert choice, quiz["questions"]
    # First attempt: pick the wrong option on every choice question.
    answers = [
        {"question_id": q["id"], "selected_index": 1}
        if q["question_type"] == "choice"
        else {"question_id": q["id"], "text_answer": "typed"}
        for q in quiz["questions"]
    ]
    first = await client.post(
        f"/api/v1/quizzes/{quiz['id']}/attempt", headers=headers, json={"answers": answers}
    )
    assert first.status_code == 200, first.text
    failed = [r for r in first.json()["results"] if not r["correct"]]
    assert failed, first.json()["results"]
    retry_ids = [r["question_id"] for r in failed if r["question_type"] == "choice"]
    assert retry_ids

    retry = await client.post(
        f"/api/v1/quizzes/{quiz['id']}/attempt",
        headers=headers,
        json={
            "question_ids": retry_ids,
            "answers": [{"question_id": qid, "selected_index": 0} for qid in retry_ids],
        },
    )
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert len(body["results"]) == len(retry_ids)
    assert all(r["correct"] for r in body["results"])
    assert body["score"] == 1.0
    assert body["id"] != first.json()["id"]

    records = await client.get(f"/api/v1/sources/{source_id}/quiz/records", headers=headers)
    assert len(records.json()["records"]) == 2


async def _seed_quiz_source(
    client: AsyncClient, email: str, storage_key: str
) -> tuple[dict, str]:
    """Register a learner and give them one ready source with a single chunk."""
    headers = await auth_header(client, email=email)
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        source = Source(
            user_id=user.id,
            filename="notes.pdf",
            content_type="application/pdf",
            kind="pdf",
            storage_key=storage_key,
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
    return headers, source_id


async def _generate_with_attempt(client: AsyncClient, headers: dict, source_id: str) -> dict:
    quiz = (await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)).json()
    answers = [{"question_id": q["id"], "selected_index": 0} for q in quiz["questions"]]
    resp = await client.post(
        f"/api/v1/quizzes/{quiz['id']}/attempt", headers=headers, json={"answers": answers}
    )
    assert resp.status_code == 200, resp.text
    return quiz


@pytest.mark.asyncio
async def test_quiz_delete_keeps_the_answer_archive(client: AsyncClient) -> None:
    headers, source_id = await _seed_quiz_source(
        client, "quiz-del@example.com", "memory/quiz-del.pdf"
    )

    first = await _generate_with_attempt(client, headers, source_id)
    second = (await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)).json()
    assert second["id"] != first["id"]

    resp = await client.delete(f"/api/v1/quizzes/{first['id']}?with_attempts=false", headers=headers)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["quiz_id"] == first["id"]
    assert report["deleted_questions"] == len(first["questions"])
    assert report["deleted_attempts"] == 0
    assert report["with_attempts"] is False

    # Gone from the picker and no longer reachable by id…
    listed = (await client.get(f"/api/v1/sources/{source_id}/quizzes", headers=headers)).json()
    assert [q["id"] for q in listed["quizzes"]] == [second["id"]]
    by_id = await client.get(f"/api/v1/sources/{source_id}/quiz?quiz_id={first['id']}", headers=headers)
    assert by_id.status_code == 404
    # …while "latest" falls back to the surviving version.
    latest = (await client.get(f"/api/v1/sources/{source_id}/quiz", headers=headers)).json()
    assert latest["id"] == second["id"]

    # The row survives as a tombstone: that is what keeps quiz_attempts' NOT NULL
    # foreign key valid on PostgreSQL and the archived title readable.
    async with SessionLocal() as session:
        row = (await session.execute(select(Quiz).where(Quiz.id == UUID(first["id"])))).scalar_one()
        assert row.deleted_at is not None

    records = (
        await client.get(f"/api/v1/sources/{source_id}/quiz/records", headers=headers)
    ).json()["records"]
    kept = [r for r in records if r["quiz_id"] == first["id"]]
    assert len(kept) == 1
    assert kept[0]["quiz_title"] == first["title"]
    detail = await client.get(f"/api/v1/quiz-attempts/{kept[0]['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert len(detail.json()["results"]) == len(first["questions"])

    # The survivor is still fully usable, and regenerating is unaffected.
    answers = [{"question_id": q["id"], "selected_index": 0} for q in second["questions"]]
    again = await client.post(
        f"/api/v1/quizzes/{second['id']}/attempt", headers=headers, json={"answers": answers}
    )
    assert again.status_code == 200, again.text
    third = await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)
    assert third.status_code == 200, third.text
    assert third.json()["id"] not in (first["id"], second["id"])


@pytest.mark.asyncio
async def test_quiz_delete_with_attempts_erases_the_version(client: AsyncClient) -> None:
    headers, source_id = await _seed_quiz_source(
        client, "quiz-del-all@example.com", "memory/quiz-del-all.pdf"
    )
    quiz = await _generate_with_attempt(client, headers, source_id)

    resp = await client.delete(f"/api/v1/quizzes/{quiz['id']}?with_attempts=true", headers=headers)
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["deleted_attempts"] == 1
    assert report["deleted_questions"] == len(quiz["questions"])

    assert (await client.get(f"/api/v1/sources/{source_id}/quizzes", headers=headers)).json()[
        "quizzes"
    ] == []
    assert (await client.get(f"/api/v1/sources/{source_id}/quiz", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/sources/{source_id}/quiz/records", headers=headers)).json()[
        "records"
    ] == []

    async with SessionLocal() as session:
        assert (
            await session.execute(select(Quiz).where(Quiz.id == UUID(quiz["id"])))
        ).scalar_one_or_none() is None

    # The source itself is untouched, so a fresh set still generates.
    fresh = await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=headers)
    assert fresh.status_code == 200, fresh.text


@pytest.mark.asyncio
async def test_quiz_delete_is_scoped_to_the_owner(client: AsyncClient) -> None:
    owner, source_id = await _seed_quiz_source(
        client, "quiz-del-owner@example.com", "memory/quiz-del-owner.pdf"
    )
    quiz = (await client.post(f"/api/v1/sources/{source_id}/quiz/generate", headers=owner)).json()
    intruder = await auth_header(client, email="quiz-del-intruder@example.com")

    assert (await client.delete(f"/api/v1/quizzes/{quiz['id']}", headers=intruder)).status_code == 404
    assert (await client.get(f"/api/v1/sources/{source_id}/quiz", headers=owner)).status_code == 200

    # Tombstone first, then a record-purging delete finally drops the row.
    assert (await client.delete(f"/api/v1/quizzes/{quiz['id']}", headers=owner)).status_code == 200
    assert (
        await client.delete(f"/api/v1/quizzes/{quiz['id']}?with_attempts=true", headers=owner)
    ).status_code == 200
    assert (
        await client.delete(f"/api/v1/quizzes/{quiz['id']}?with_attempts=true", headers=owner)
    ).status_code == 404
