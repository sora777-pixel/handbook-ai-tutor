"""Per-user workspace folders.

Every account owns one directory tree under the storage root::

    <storage_root>/<user_id>/
        profile/account.json        账号元数据（用户 ID / 邮箱 / 注册时间）
        records/*.json              学习记录存档（笔记、摘要、知识点、成绩、问答、用量）
        resources/<source_id>/...   导入的学习资源原件（文档 / 视频 / 音频）

The database stays the source of truth for the running application; ``profile/``
and ``records/`` are an on-disk mirror so a learner can see and back up exactly
what the platform holds for them, and support can map a folder back to an
account without opening the database.

Directory naming: the folder is the user's primary key (a UUID), which is
guaranteed unique and stable. The human-readable identity (email) is recorded
inside ``profile/account.json``, where it cannot break path handling.

The password is deliberately absent from this tree. It exists only as a one-way
bcrypt hash in ``users.hashed_password``; copying it to disk would widen the
blast radius for no benefit, and a plaintext copy is not something this project
will ever write.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    KnowledgePoint,
    Quiz,
    QuizAttempt,
    QuizQuestion,
    Source,
    SourceNote,
    SourceSummary,
    TokenUsage,
    TutorMessage,
)

logger = logging.getLogger("app.workspace")

#: The three fixed sub-folders every workspace carries.
SUBDIRS: tuple[str, ...] = ("profile", "records", "resources")

ACCOUNT_FILE = "account.json"
SCHEMA_VERSION = 1


# ------------------------------------------------------------------ paths


def storage_root() -> Path:
    """Root that holds every user's folder."""
    return Path(get_settings().local_storage_path).resolve()


def workspace_dir(user_id: UUID | str) -> Path:
    return storage_root() / str(user_id)


def workspace_subdir(user_id: UUID | str, name: str) -> Path:
    if name not in SUBDIRS:
        raise ValueError(f"unknown workspace sub-folder: {name!r}")
    return workspace_dir(user_id) / name


def resource_key(user_id: UUID | str, source_id: UUID | str, filename: str | None) -> str:
    """Storage key for an uploaded original, inside its owner's folder.

    Kept relative to the storage root so both the local and the MinIO backend
    resolve it the same way.
    """
    return f"{user_id}/resources/{source_id}/{filename or 'upload'}"


# ------------------------------------------------------------------ helpers


def _write_json(path: Path, payload: Any) -> None:
    """Write *payload* as UTF-8 JSON, atomically.

    A crash mid-write must not leave a half-written snapshot that a learner
    would mistake for their real data.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")
    tmp.replace(path)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.isoformat()
    return str(value) if value is not None else None


# ------------------------------------------------------------------ ensure


def ensure_workspace(user_id: UUID | str, email: str, *, created_at: Any = None) -> Path:
    """Create the three sub-folders and refresh ``profile/account.json``.

    Idempotent on purpose: it is called on registration *and* on every
    successful login, so a folder deleted by hand (or by an empty-directory
    prune) comes back without any operator action. Returns the workspace root.
    """
    root = workspace_dir(user_id)
    for name in SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)

    _write_json(
        root / "profile" / ACCOUNT_FILE,
        {
            "schema_version": SCHEMA_VERSION,
            "user_id": str(user_id),
            "email": email,
            "registered_at": _iso(created_at),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "folders": {
                "profile": "账号元数据",
                "records": "学习记录存档",
                "resources": "导入的学习资源原件",
            },
            "password": (
                "未保存在此目录：口令仅以 bcrypt 单向哈希存于数据库 users 表，"
                "不可逆、也无法导出为明文。"
            ),
        },
    )
    return root


def workspace_report(user_id: UUID | str) -> dict[str, Any]:
    """Shape and size of one workspace, for diagnostics and verification."""
    root = workspace_dir(user_id)
    dirs: dict[str, Any] = {}
    total_files = 0
    total_bytes = 0
    for name in SUBDIRS:
        path = root / name
        files = 0
        size = 0
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    files += 1
                    try:
                        size += child.stat().st_size
                    except OSError:
                        pass
        dirs[name] = {"exists": path.is_dir(), "files": files, "bytes": size}
        total_files += files
        total_bytes += size
    return {
        "user_id": str(user_id),
        "path": str(root),
        "exists": root.is_dir(),
        "subdirs": dirs,
        "total_files": total_files,
        "total_bytes": total_bytes,
    }


# ------------------------------------------------------------------ records


async def export_records(db: AsyncSession, user_id: UUID) -> dict[str, int]:
    """Write the learner's records into ``records/`` and return row counts.

    Snapshots are grouped by domain rather than one file per table so the folder
    stays readable::

        study_notes.json   摘要 / 知识点 / 笔记
        quiz_records.json  题组 / 题目 / 作答与成绩
        tutor_qa.json      问答记录
        usage.json         token 用量
        overview.json      总览与生成时间

    Called after a quiz is graded (so 成绩 is archived as it happens) and from
    the explicit export endpoint.
    """
    root = workspace_dir(user_id)
    for name in SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    records = root / "records"

    sources = (
        (await db.execute(select(Source).where(Source.user_id == user_id).order_by(Source.created_at)))
        .scalars()
        .all()
    )
    source_ids = [s.id for s in sources]

    summaries: list[SourceSummary] = []
    knowledge: list[KnowledgePoint] = []
    quizzes: list[Quiz] = []
    if source_ids:
        summaries = list(
            (await db.execute(select(SourceSummary).where(SourceSummary.source_id.in_(source_ids))))
            .scalars()
            .all()
        )
        knowledge = list(
            (await db.execute(select(KnowledgePoint).where(KnowledgePoint.source_id.in_(source_ids))))
            .scalars()
            .all()
        )
        quizzes = list(
            (await db.execute(select(Quiz).where(Quiz.source_id.in_(source_ids)))).scalars().all()
        )

    notes = list(
        (await db.execute(select(SourceNote).where(SourceNote.user_id == user_id))).scalars().all()
    )
    tutor = list(
        (await db.execute(select(TutorMessage).where(TutorMessage.user_id == user_id))).scalars().all()
    )
    usage = list(
        (await db.execute(select(TokenUsage).where(TokenUsage.user_id == user_id))).scalars().all()
    )

    quiz_ids = [q.id for q in quizzes]
    questions: list[QuizQuestion] = []
    attempts: list[QuizAttempt] = []
    if quiz_ids:
        questions = list(
            (await db.execute(select(QuizQuestion).where(QuizQuestion.quiz_id.in_(quiz_ids))))
            .scalars()
            .all()
        )
        attempts = list(
            (await db.execute(select(QuizAttempt).where(QuizAttempt.quiz_id.in_(quiz_ids))))
            .scalars()
            .all()
        )

    _write_json(
        records / "study_notes.json",
        {
            "sources": [
                {
                    "id": str(s.id),
                    "title": s.title,
                    "filename": s.filename,
                    "kind": s.kind,
                    "status": s.status,
                    "byte_size": s.byte_size,
                    "created_at": _iso(s.created_at),
                }
                for s in sources
            ],
            "summaries": [
                {"source_id": str(x.source_id), "title": x.title, "overview": x.overview,
                 "outline": json.loads(x.outline_json or "[]")}
                for x in summaries
            ],
            "knowledge_points": [
                {"id": str(x.id), "source_id": str(x.source_id), "title": x.title, "summary": x.summary,
                 "key_terms": json.loads(x.key_terms_json or "[]")}
                for x in knowledge
            ],
            "notes": [
                {"id": str(x.id), "source_id": str(x.source_id), "title": x.title, "content": x.content,
                 "origin": x.origin, "anchor": x.anchor, "created_at": _iso(x.created_at)}
                for x in notes
            ],
        },
    )

    _write_json(
        records / "quiz_records.json",
        {
            "quizzes": [
                {"id": str(q.id), "source_id": str(q.source_id), "title": q.title,
                 "created_at": _iso(q.created_at), "deleted_at": _iso(q.deleted_at)}
                for q in quizzes
            ],
            "questions": [
                {"id": str(x.id), "quiz_id": str(x.quiz_id), "ordinal": x.ordinal,
                 "section_title": x.section_title, "question_type": x.question_type,
                 "question": x.question, "correct_index": x.correct_index,
                 "reference_answer": x.reference_answer, "explanation": x.explanation}
                for x in questions
            ],
            "attempts": [
                {"id": str(a.id), "quiz_id": str(a.quiz_id), "score": a.score, "passed": a.passed,
                 "graded_count": a.graded_count, "submitted_at": _iso(a.created_at),
                 "results": json.loads(a.details_json or "[]")}
                for a in attempts
            ],
        },
    )

    _write_json(
        records / "tutor_qa.json",
        [
            {"id": str(m.id), "source_id": str(m.source_id), "role": m.role, "content": m.content,
             "citations": json.loads(m.citations_json or "[]"), "created_at": _iso(m.created_at)}
            for m in tutor
        ],
    )

    _write_json(
        records / "usage.json",
        [
            {"task": u.task, "provider": u.provider, "model": u.model,
             "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
             "source_id": str(u.source_id) if u.source_id else None, "created_at": _iso(u.created_at)}
            for u in usage
        ],
    )

    counts = {
        "sources": len(sources),
        "summaries": len(summaries),
        "knowledge_points": len(knowledge),
        "notes": len(notes),
        "quizzes": len(quizzes),
        "questions": len(questions),
        "attempts": len(attempts),
        "tutor_messages": len(tutor),
        "token_usages": len(usage),
    }
    _write_json(
        records / "overview.json",
        {
            "schema_version": SCHEMA_VERSION,
            "user_id": str(user_id),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counts": counts,
        },
    )
    return counts


async def provision_for_user(db: AsyncSession, user_id: UUID, email: str, *, created_at: Any = None) -> None:
    """Best-effort provisioning used by the auth flow.

    A disk problem must never lock a learner out of their account, so failures
    are logged loudly and swallowed. Uploads still fail loudly on their own if
    the folder is genuinely unusable.
    """
    try:
        ensure_workspace(user_id, email, created_at=created_at)
    except Exception as exc:
        logger.warning("could not provision workspace for %s: %s", user_id, exc)


async def archive_records(db: AsyncSession, user_id: UUID) -> None:
    """Best-effort records snapshot, for hooks inside a live request.

    Archiving must never fail the action that triggered it (grading a quiz),
    so a write error is logged and swallowed.
    """
    try:
        await export_records(db, user_id)
    except Exception as exc:
        logger.warning("could not archive records for %s: %s", user_id, exc)