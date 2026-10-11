"""Per-user workspace folders.

Every account owns one directory tree under the storage root::

    <storage_root>/<username>/
        profile/account.json        账号元数据（用户 ID / 邮箱 / 注册时间）
        records/*.json              学习记录存档（笔记、摘要、知识点、成绩、问答、用量）
        resources/<source_id>/...   导入的学习资源原件（文档 / 视频 / 音频）

The database stays the source of truth for the running application; ``profile/``
and ``records/`` are an on-disk mirror so a learner can see and back up exactly
what the platform holds for them, and support can map a folder back to an
account without opening the database.

Directory naming: the folder is the account's *username* (its email), so anyone
opening ``data\\storage`` sees ``zhangsan@example.com`` instead of an opaque
UUID. Characters Windows forbids are replaced with ``_`` and reserved device
names are prefixed, so an unusual email can never escape the storage root or
create an unopenable path. Accounts created before this scheme have their UUID
folder moved into the named one on the next login
(``migrate_legacy_folder``), and reads fall back to the old key, so an upload
made earlier still opens.

The password is deliberately absent from this tree. It exists only as a one-way
bcrypt hash in ``users.hashed_password``; copying it to disk would widen the
blast radius for no benefit, and a plaintext copy is not something this project
will ever write.
"""

from __future__ import annotations

import json
import logging
import re
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

#: Characters Windows refuses in a path component, plus control characters.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Device names Windows reserves regardless of extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

#: How long a stored object name may get. Windows still cannot open a path past
#: ~260 characters unless long-path support is enabled machine-wide, and a
#: textbook exported from a library site really does arrive as
#: "The Hermes Agent Handbook ... (Lu, Xiao) (z-library.sk, 1lib.sk, z-lib.sk).epub".
#: That name is fine to *display*, but writing it produced two empty resource
#: folders and a FileNotFoundError on every later read, so the on-disk name is
#: bounded and the original is kept in the database.
MAX_OBJECT_STEM = 60


def safe_object_name(source_id: UUID | str, filename: str | None) -> str:
    """A filesystem-safe, length-bounded name for the stored original.

    The extension is preserved (FFmpeg and the PDF/EPUB readers use it), the
    stem is trimmed, and a short hash of the original name keeps two different
    files with the same visible prefix apart. The display name never changes:
    ``Source.filename`` keeps what the learner uploaded.
    """
    import hashlib

    raw = (filename or "upload").strip() or "upload"
    stem, dot, ext = raw.rpartition(".")
    if not dot:                      # no extension
        stem, ext = raw, ""
    clean_ext = _UNSAFE.sub("", ext)[:12]
    clean_stem = _UNSAFE.sub("_", stem).strip().strip(".")
    if not clean_stem:
        clean_stem = "upload"
    if clean_stem.split(".")[0].lower() in _RESERVED:
        clean_stem = f"file_{clean_stem}"
    digest = hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:8]
    budget = max(16, MAX_OBJECT_STEM - len(digest) - 1)
    trimmed = clean_stem[:budget].rstrip(" .")
    name = f"{trimmed}-{digest}"
    if clean_ext:
        name = f"{name}.{clean_ext}"
    return name


def user_folder_name(user_id: UUID | str, email: str | None) -> str:
    """Folder name for one account: its username, made filesystem-safe.

    Falls back to the UUID when there is no usable email, so a workspace always
    has a name and can never collapse onto the storage root itself.
    """
    raw = (email or "").strip()
    name = _UNSAFE.sub("_", raw).strip().strip(".")
    if not name:
        return str(user_id)
    if name.split(".")[0].lower() in _RESERVED:
        name = f"user_{name}"
    # Stay comfortably under the Windows 255-char component limit.
    return name[:120]


# ------------------------------------------------------------------ paths


def storage_root() -> Path:
    """Root that holds every user's folder."""
    return Path(get_settings().local_storage_path).resolve()


def workspace_dir(user_id: UUID | str, email: str | None = None) -> Path:
    """This account's folder (named after its username when known)."""
    return storage_root() / user_folder_name(user_id, email)


def workspace_subdir(user_id: UUID | str, name: str, email: str | None = None) -> Path:
    if name not in SUBDIRS:
        raise ValueError(f"unknown workspace sub-folder: {name!r}")
    return workspace_dir(user_id, email) / name


def resource_key(
    user_id: UUID | str,
    source_id: UUID | str,
    filename: str | None,
    email: str | None = None,
) -> str:
    """Storage key for an uploaded original, inside its owner's folder.

    Kept relative to the storage root so both the local and the MinIO backend
    resolve it the same way. The name component is bounded by
    ``safe_object_name`` -- a 200-character title may be stored, but the file it
    points at must stay openable on Windows.
    """
    folder = user_folder_name(user_id, email)
    return f"{folder}/resources/{source_id}/{safe_object_name(source_id, filename)}"


def legacy_resource_key(user_id: UUID | str, source_id: UUID | str, filename: str | None) -> str:
    """The pre-username key (`<uuid>/resources/...`), for files already on disk.

    Uses the raw filename on purpose: rows written before the object name was
    bounded point at a file that really is called that.
    """
    return f"{user_id}/resources/{source_id}/{filename or 'upload'}"


def storage_candidates(source: Any, user_id: UUID | str, email: str | None) -> list[str]:
    """Keys to try when reading an upload, newest scheme first.

    A row keeps whatever key it was written with, so the fallback only matters
    for files that predate the rename -- and for those it is the difference
    between "my earlier uploads still open" and a 404.
    """
    keys: list[str] = []
    primary = getattr(source, "storage_key", None)
    if primary:
        keys.append(primary)
    source_id = getattr(source, "id", "")
    filename = getattr(source, "filename", None)
    # After a folder migration the file sits under the username folder while an
    # old row may still name the UUID folder, so both spellings are tried.
    if email:
        current = resource_key(user_id, source_id, filename, email)
        if current not in keys:
            keys.append(current)
    legacy = legacy_resource_key(user_id, source_id, filename)
    if legacy not in keys:
        keys.append(legacy)
    return keys


async def read_source_bytes(source: Any, user_id: UUID | str, email: str | None = None) -> bytes:
    """Read an uploaded original, tolerating the pre-rename key layout."""
    from app.services.storage import get_storage

    storage = get_storage()
    last: Exception | None = None
    for key in storage_candidates(source, user_id, email):
        try:
            data = await storage.get_bytes(key)
            if data:
                return data
        except FileNotFoundError:
            continue
        except Exception as exc:  # backend trouble is not "this file is gone"
            last = exc
    if last is not None:
        raise last
    raise FileNotFoundError(f"原始文件不在存储中：{getattr(source, 'filename', '')}")


def migrate_legacy_folder(user_id: UUID | str, email: str) -> bool:
    """Move `<storage_root>/<uuid>/` into `<storage_root>/<username>/`.

    One-time and idempotent, driven from ``ensure_workspace`` (register +
    login). Contents are *moved*, not copied, and an existing file in the target
    wins, so running it twice cannot duplicate or lose an upload. Returns True
    when something actually moved.
    """
    legacy = storage_root() / str(user_id)
    target = workspace_dir(user_id, email)
    if legacy == target or not legacy.is_dir():
        return False
    moved = False
    try:
        for name in SUBDIRS:
            src = legacy / name
            if not src.is_dir():
                continue
            dst = target / name
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
                moved = True
            else:
                for child in src.iterdir():
                    dest = dst / child.name
                    if dest.exists():
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    child.replace(dest)
                    moved = True
                try:
                    src.rmdir()
                except OSError:
                    pass
        if legacy.is_dir() and not any(legacy.iterdir()):
            try:
                legacy.rmdir()
            except OSError:
                pass
        if moved:
            logger.info("workspace migrated: %s -> %s", legacy.name, target.name)
    except OSError as exc:
        # A locked file (an upload in flight) must not fail the login; the next
        # login retries whatever is left.
        logger.warning("workspace migration incomplete for %s: %s", user_id, exc)
    return moved


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
    migrate_legacy_folder(user_id, email)
    root = workspace_dir(user_id, email)
    for name in SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)

    _write_json(
        root / "profile" / ACCOUNT_FILE,
        {
            "schema_version": SCHEMA_VERSION,
            "user_id": str(user_id),
            "email": email,
            "folder": root.name,
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


def workspace_report(user_id: UUID | str, email: str | None = None) -> dict[str, Any]:
    """Shape and size of one workspace, for diagnostics and verification."""
    root = workspace_dir(user_id, email)
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
    legacy = storage_root() / str(user_id)
    legacy_left = legacy.is_dir() and legacy != root and any(legacy.iterdir())
    return {
        "user_id": str(user_id),
        "email": email,
        "folder": root.name,
        "path": str(root),
        "exists": root.is_dir(),
        "legacy_folder": str(legacy) if legacy_left else None,
        "subdirs": dirs,
        "total_files": total_files,
        "total_bytes": total_bytes,
    }


# ------------------------------------------------------------------ records


async def export_records(db: AsyncSession, user_id: UUID, email: str | None = None) -> dict[str, int]:
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
    root = workspace_dir(user_id, email)
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


async def rewrite_legacy_keys(db: AsyncSession, user_id: UUID, email: str) -> int:
    """Point `sources.storage_key` at the username folder after a migration.

    Without this the row still names `<uuid>/resources/...` while the file now
    lives under `<username>/resources/...`. Reads have a fallback, but leaving
    the row stale would break deletion and make the database disagree with the
    disk, so the prefix is rewritten once, right after the move.
    """
    from sqlalchemy import update

    old = f"{user_id}/"
    new = f"{user_folder_name(user_id, email)}/"
    if old == new:
        return 0
    result = await db.execute(
        update(Source)
        .where(Source.user_id == user_id, Source.storage_key.like(f"{old}%"))
        .values(storage_key=func_replace(Source.storage_key, old, new))
    )
    await db.commit()
    return int(result.rowcount or 0)


def func_replace(column, old: str, new: str):
    from sqlalchemy import func

    return func.replace(column, old, new)


async def provision_for_user(db: AsyncSession, user_id: UUID, email: str, *, created_at: Any = None) -> None:
    """Best-effort provisioning used by the auth flow.

    A disk problem must never lock a learner out of their account, so failures
    are logged loudly and swallowed. Uploads still fail loudly on their own if
    the folder is genuinely unusable.
    """
    try:
        moved = migrate_legacy_folder(user_id, email)
        ensure_workspace(user_id, email, created_at=created_at)
        if moved:
            rows = await rewrite_legacy_keys(db, user_id, email)
            if rows:
                logger.info("storage keys updated for %s row(s) after migration", rows)
    except Exception as exc:
        logger.warning("could not provision workspace for %s: %s", user_id, exc)


async def archive_records(db: AsyncSession, user_id: UUID, email: str | None = None) -> None:
    """Best-effort records snapshot, for hooks inside a live request.

    Archiving must never fail the action that triggered it (grading a quiz),
    so a write error is logged and swallowed.
    """
    try:
        await export_records(db, user_id, email)
    except Exception as exc:
        logger.warning("could not archive records for %s: %s", user_id, exc)