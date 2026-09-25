from __future__ import annotations

import json
import urllib.parse
from io import BytesIO
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, get_current_user_allow_query_token
from app.domain.schemas import (
    OutlineEntry,
    SourceDeleteOut,
    SourceOut,
    SourceStructureOut,
    StructureChunkOut,
    TaskOut,
)
from app.models.chunk import DocumentChunk
from app.models.source import Source
from app.models.task import Task
from app.models.user import User
from app.services.library import delete_source_cascade
from app.services.queue import get_queue
from app.services.storage import get_storage
from app.services.workspace import ensure_workspace, resource_key

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])

# Scope A originally accepted PDF + MP4 only, which rejected photos and scans of
# textbook pages outright. Images now go through the OCR stage like a scanned PDF.
ALLOWED: dict[str, str] = {
    "application/pdf": "pdf",
    "video/mp4": "video",
    "image/png": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "image/gif": "image",
    "image/bmp": "image",
    "image/tiff": "image",
}

EXTENSION_KINDS: dict[str, str] = {
    ".pdf": "pdf",
    ".mp4": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".bmp": "image",
    ".tif": "image",
    ".tiff": "image",
}

SUPPORTED_HINT = "PDF, MP4, PNG, JPG, JPEG, WEBP, GIF, BMP or TIFF"


def _kind_for(file: UploadFile) -> str:
    name = (file.filename or "").lower()
    # Browsers sometimes append parameters, e.g. "text/plain; charset=utf-8".
    ctype = (file.content_type or "").lower().split(";")[0].strip()
    if ctype in ALLOWED:
        return ALLOWED[ctype]
    for extension, kind in EXTENSION_KINDS.items():
        if name.endswith(extension):
            return kind
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported file type. Supported: {SUPPORTED_HINT}.",
    )


@router.post("/upload", response_model=SourceOut)
async def upload_source(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SourceOut:
    kind = _kind_for(file)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if kind == "image":
        # Fail fast on a corrupt image instead of surfacing it minutes later from
        # deep inside the OCR stage. Image.open is lazy and only reads the header.
        try:
            with Image.open(BytesIO(data)) as probe:
                probe.verify()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not read this image ({type(exc).__name__}). "
                "Re-export it as PNG or JPG and try again.",
            ) from None
    source_id = uuid4()
    # The original lands in its owner's own folder: <user_id>/resources/<source_id>/.
    key = resource_key(user.id, source_id, file.filename)
    storage = get_storage()
    await storage.ensure_ready()
    await storage.put_bytes(key, data, file.content_type or "application/octet-stream")

    source = Source(
        id=source_id,
        user_id=user.id,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
        kind=kind,
        storage_key=key,
        byte_size=len(data),
        status="queued",
        title=file.filename,
    )
    task = Task(user_id=user.id, source_id=source.id, kind="ingest", status="queued", progress=0, step="queued")
    db.add_all([source, task])
    await db.commit()
    await db.refresh(source)
    await db.refresh(task)

    job_id = await get_queue().enqueue_ingest(source.id, task.id)
    task.arq_job_id = job_id
    await db.commit()

    out = SourceOut.model_validate(source)
    out.task_id = task.id
    return out


@router.get("", response_model=list[SourceOut])
async def list_sources(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SourceOut]:
    rows = (
        (await db.execute(select(Source).where(Source.user_id == user.id).order_by(Source.created_at.desc())))
        .scalars()
        .all()
    )
    latest = await _latest_tasks_by_source(db, [r.id for r in rows])
    return [_source_out_with_task(r, latest.get(r.id)) for r in rows]


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(
    source_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SourceOut:
    source = await _owned_source(db, source_id, user.id)
    task = (
        await db.execute(
            select(Task).where(Task.source_id == source.id).order_by(Task.created_at.desc())
        )
    ).scalars().first()
    return _source_out_with_task(source, task)


def _source_out_with_task(source: Source, task: Task | None) -> SourceOut:
    """Attach the latest ingest task so the UI can stream progress and show provenance."""
    out = SourceOut.model_validate(source)
    if task is None:
        return out
    out.task_id = task.id
    out.task_status = task.status
    out.task_progress = task.progress
    out.task_step = task.step
    out.task_message = task.message
    out.extraction_note = task.message
    return out


async def _latest_tasks_by_source(db: AsyncSession, source_ids: list[UUID]) -> dict[UUID, Task]:
    if not source_ids:
        return {}
    rows = (
        (await db.execute(select(Task).where(Task.source_id.in_(source_ids)).order_by(Task.created_at.desc())))
        .scalars()
        .all()
    )
    latest: dict[UUID, Task] = {}
    for task in rows:
        if task.source_id not in latest:
            latest[task.source_id] = task
    return latest


async def _owned_source(db: AsyncSession, source_id: UUID, user_id: UUID) -> Source:
    source = (await db.execute(select(Source).where(Source.id == source_id))).scalar_one_or_none()
    if source is None or source.user_id != user_id:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.get("/{source_id}/structure", response_model=SourceStructureOut)
async def get_structure(
    source_id: UUID,
    limit: int = Query(default=300, ge=1, le=3000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SourceStructureOut:
    """What the parser understood: page map, section tree, and typed chunks.

    Exposed so the reading can be inspected rather than trusted. If a section is
    missing or the page offset is wrong, it is visible here immediately instead
    of surfacing as a puzzling answer three questions later.
    """
    source = await _owned_source(db, source_id, user.id)

    chunks = (
        (
            await db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.source_id == source.id)
                .order_by(DocumentChunk.ordinal)
            )
        )
        .scalars()
        .all()
    )

    # A recording stores {"kind","duration","sections"}; a document stores a bare
    # list. Both shapes are accepted so existing rows keep working.
    duration: float | None = None
    outline: list[OutlineEntry] = []
    try:
        raw = json.loads(source.section_outline_json or "[]")
        if isinstance(raw, dict):
            sections = raw.get("sections") or []
            raw_duration = raw.get("duration")
            duration = float(raw_duration) if raw_duration is not None else None
        else:
            sections = raw or []
        for item in sections:
            outline.append(OutlineEntry.model_validate(item))
    except Exception:
        outline = []

    # Recordings ingested before the chapter pass existed have an empty outline
    # column even though their transcript chunks carry real timestamps — the
    # panel then claimed "没有任何结构分析结果" while 900 usable timestamps sat in
    # the table. Rebuild a chapter list from the chunks instead of showing
    # nothing, and say so through the same jumpable structure the fresh pipeline
    # produces.
    if source.kind == "video":
        if not outline:
            outline = _outline_from_timed_chunks(chunks)
        if duration is None:
            ends = [float(c.end_time) for c in chunks if c.end_time is not None]
            duration = max(ends) if ends else None

    counts: dict[str, int] = {}
    for chunk in chunks:
        key = chunk.content_type or "body"
        counts[key] = counts.get(key, 0) + 1

    # "Where is this section?" — a page for a book, a timestamp for a recording.
    section_index: dict[str, list[float]] = {}
    for chunk in chunks:
        title = (chunk.section_title or "").strip()
        if not title:
            continue
        if chunk.start_time is not None:
            marks = section_index.setdefault(title, [])
            seconds = float(chunk.start_time)
            if seconds not in marks:
                marks.append(seconds)
        elif (chunk.content_type or "") == "heading":
            page = chunk.printed_page if chunk.printed_page is not None else chunk.page_number
            if page is not None:
                marks = section_index.setdefault(title, [])
                if float(page) not in marks:
                    marks.append(float(page))

    return SourceStructureOut(
        source_id=source.id,
        kind=source.kind,
        duration=duration,
        page_offset=source.page_offset,
        page_count=source.page_count,
        outline=outline,
        content_types=counts,
        chunk_total=len(chunks),
        chunks=[
            StructureChunkOut(
                id=chunk.id,
                ordinal=chunk.ordinal,
                content_type=chunk.content_type or "body",
                section_title=chunk.section_title,
                page_number=chunk.page_number,
                printed_page=chunk.printed_page,
                locator=chunk.locator,
                heading_level=chunk.heading_level,
                start_time=chunk.start_time,
                end_time=chunk.end_time,
                preview=(chunk.content or "")[:160],
            )
            for chunk in chunks[:limit]
        ],
        section_index=section_index,
    )


#: How many chapters a synthesised recording outline aims for when the chunker
#: did not leave section tags behind.
_SYNTH_MIN_CHAPTERS = 4
_SYNTH_MAX_CHAPTERS = 12
_SYNTH_SECONDS_PER_CHAPTER = 180.0


def _outline_from_timed_chunks(chunks: list[DocumentChunk]) -> list[OutlineEntry]:
    """Rebuild a chapter tree for a recording from its own transcript chunks.

    Two strategies, in order of fidelity:

    1. the chunker already tagged every body chunk with the chapter it belongs
       to (`section_title`) — group consecutive runs of the same tag;
    2. there is no tagging at all, so fall back to even time windows over the
       transcript, labelled exactly like the pipeline's own fallback
       (``片段 N（m:ss）``).

    Never invents a timestamp: every boundary comes from a real chunk.
    """
    timed = [c for c in chunks if c.start_time is not None]
    if not timed:
        return []

    def start_of(chunk: DocumentChunk) -> float:
        return float(chunk.start_time or 0.0)

    def end_of(chunk: DocumentChunk) -> float:
        return float(chunk.end_time) if chunk.end_time is not None else start_of(chunk)

    body = [c for c in timed if (c.content_type or "body") != "outline"]
    if not body:
        body = timed

    # --- strategy 1: use the chapter tags the chunker wrote
    runs: list[tuple[str, list[DocumentChunk]]] = []
    for chunk in body:
        title = (chunk.section_title or "").strip()
        if runs and runs[-1][0] == title:
            runs[-1][1].append(chunk)
        else:
            runs.append((title, [chunk]))
    titled = [run for run in runs if run[0]]
    if len(titled) >= 2:
        entries = [
            OutlineEntry(
                level=1,
                number=str(i),
                title=title[:200],
                page_number=0,
                start_time=min(start_of(c) for c in rows),
                end_time=max(end_of(c) for c in rows),
            )
            for i, (title, rows) in enumerate(titled, 1)
        ]
        entries.sort(key=lambda e: e.start_time or 0.0)
        for i, entry in enumerate(entries, 1):
            entry.number = str(i)
        return entries

    # --- strategy 2: even time windows over the transcript
    ordered = sorted(body, key=start_of)
    span = end_of(ordered[-1]) - start_of(ordered[0])
    target = int(span // _SYNTH_SECONDS_PER_CHAPTER) if span > 0 else _SYNTH_MIN_CHAPTERS
    target = max(_SYNTH_MIN_CHAPTERS, min(_SYNTH_MAX_CHAPTERS, target))
    step = max(1, len(ordered) // target)
    buckets: list[list[DocumentChunk]] = [
        ordered[i : i + step] for i in range(0, len(ordered), step)
    ]
    entries: list[OutlineEntry] = []
    for i, bucket in enumerate(buckets, 1):
        if not bucket:
            continue
        begin = start_of(bucket[0])
        entries.append(
            OutlineEntry(
                level=1,
                number=str(i),
                title=f"片段 {i}（{_clock(begin)}）",
                page_number=0,
                start_time=begin,
                end_time=max(end_of(c) for c in bucket),
            )
        )
    return entries


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def _resolve_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Turn a `Range: bytes=` header into an inclusive (start, end) pair.

    Returns None when the header should be ignored and the whole file sent.
    Raises 416 for a syntactically valid but unsatisfiable range, which is what
    tells a <video> element that the resource simply ends here.
    """
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].strip()
    if "," in spec:
        # Multipart ranges are not worth supporting for a single media file.
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:  # bytes=-N -> the last N bytes
            n = int(end_s)
            if n <= 0:
                return None
            start = max(0, size - n)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail=f"Requested range starts past the end of a {size}-byte file",
            headers={"Content-Range": f"bytes */{size}"},
        )
    end = min(end, size - 1)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="Requested range is empty",
            headers={"Content-Range": f"bytes */{size}"},
        )
    return start, end


@router.get("/{source_id}/file")
async def download_source_file(
    source_id: UUID,
    request: Request,
    token: str | None = Query(default=None, description="JWT, for iframe/embed viewers"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_allow_query_token),
) -> Response:
    """Stream the original upload back to the browser.

    `Content-Disposition: inline` lets the browser's built-in viewer render it in
    place. Byte ranges are honoured because without them a <video> element can
    play but cannot be scrubbed: the player has no way to fetch "minute 42 only",
    so dragging the progress bar either does nothing or restarts the file.
    """
    source = await _owned_source(db, source_id, user.id)
    storage = get_storage()
    media_type = source.content_type or "application/octet-stream"
    quoted = urllib.parse.quote(source.filename)
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quoted}",
        "Cache-Control": "private, max-age=0, no-store",
        "X-Content-Type-Options": "nosniff",
        "Accept-Ranges": "bytes",
    }

    try:
        size = await storage.size(source.storage_key)
    except Exception:
        size = None

    if not size:
        # No size (or an empty object): keep the previous whole-body behaviour.
        try:
            data = await storage.get_bytes(source.storage_key)
        except FileNotFoundError:
            raise HTTPException(status_code=410, detail="File is no longer in storage") from None
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Storage read failed: {exc}") from exc
        headers["Content-Length"] = str(len(data))
        return Response(content=data, media_type=media_type, headers=headers)

    rng = _resolve_range(request.headers.get("range"), size)
    start, end = rng if rng else (0, size - 1)
    length = end - start + 1
    headers["Content-Length"] = str(length)
    status_code = status.HTTP_200_OK
    if rng:
        status_code = status.HTTP_206_PARTIAL_CONTENT
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(
        storage.iter_range(source.storage_key, start, length),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


@router.delete("/{source_id}", response_model=SourceDeleteOut)
async def delete_source(
    source_id: UUID,
    delete_files: bool = Query(
        default=True,
        description="Also remove the stored file from local disk / MinIO. "
        "Set false to keep the blob and only drop database rows.",
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SourceDeleteOut:
    """Delete a source together with everything derived from it."""
    source = await _owned_source(db, source_id, user.id)
    report = await delete_source_cascade(
        db, source=source, storage=get_storage(), delete_files=delete_files
    )
    # Removing the last original can prune the now-empty resources/ folder.
    # Re-provision so the account keeps its three-folder layout after a delete;
    # the account is never left without its own folder.
    ensure_workspace(user.id, user.email, created_at=user.created_at)
    return SourceDeleteOut(
        source_id=report.source_id,
        filename=report.filename,
        storage_key=report.storage_key,
        storage_deleted=report.storage_deleted,
        storage_error=report.storage_error,
        rows_deleted=report.rows_deleted,
        total_rows_deleted=report.total_rows,
    )
