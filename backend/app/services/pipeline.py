from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.core.config import get_settings
from app.models.chunk import DocumentChunk
from app.models.source import Source
from app.prompts import load_prompt
from app.services.chunking import (
    RawChunk,
    chunk_document,
    chunk_pages,
    chunk_segments,
)
from app.services.epub_parser import (
    EpubBook,
    EpubError,
    parse_epub,
    relabel_chapter_locators,
)
from app.services.knowledge import generate_summary_and_knowledge
from app.services.layout import (
    FIGURE,
    Block,
    PageAnalysis,
    analyse_pdf,
    analyse_text_pages,
    finalise,
)
from app.services.llm.base import ChatMessage, ProviderHTTPError
from app.services.llm.router import ModelRouter
from app.services.ocr import OcrUnavailable, get_ocr
from app.services.pdf_parser import (
    MIN_USABLE_CHARS,
    document_text_length,
    looks_textless,
    parse_pdf,
)
from app.core.ffmpeg import FFmpegMissing
from app.services.llm.router import LLMConfigError
from app.services.progress import ProgressService
from app.services.storage import get_storage
from app.services.workspace import read_source_bytes

logger = logging.getLogger("app.pipeline")

#: Junk that library-site exports append to a filename, stripped when the file
#: name has to stand in for a missing book title.
_FILENAME_JUNK = re.compile(
    r"\s*[\(\[]\s*(z-library|1lib|z-lib|libgen|annas?[- ]archive)[^)\]]*[)\]]",
    re.IGNORECASE,
)


def _title_from_filename(filename: str | None) -> str:
    """A readable title out of a messy filename, or "" when there is nothing.

    `上头Obsidian手把手教你用AI做好知识管理 (Shiki实验室) (z-library.sk, ...).epub`
    becomes `上头Obsidian手把手教你用AI做好知识管理` -- still not a real title
    page, but far closer than a model's paraphrase.
    """
    raw = (filename or "").strip()
    if not raw:
        return ""
    stem = raw.rsplit(".", 1)[0] if "." in raw else raw
    stem = _FILENAME_JUNK.sub("", stem)
    stem = re.sub(r"\s*\([^)]*\)\s*$", "", stem).strip(" -_")
    return stem[:120]
from app.services.stt import (
    TranscriptSegment,
    extract_audio,
    extract_video_frames,
    get_stt,
    transcribe_audio_resilient,
)
from app.utils.jsonutil import parse_json_object
from app.utils.text import normalise_text


class IngestError(RuntimeError):
    """Ingest failure with a message meant for the person who uploaded the file."""


#: Pages read by the layout probe that decides "text layer or scan". Kept small
#: because its only job is to answer that one question cheaply.
LAYOUT_PROBE_PAGES = 12


def _fmt_time(seconds: float) -> str:
    """Seconds -> m:ss (h:mm:ss past an hour), for chapter labels."""
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


class IngestPipeline:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.progress = ProgressService(session)
        self.router = ModelRouter(session)
        # Provenance of the text extraction, surfaced in the task message so the
        # learner can tell real OCR from a mock transcription.
        self.extraction_note: str | None = None
        #: The EPUB's own <dc:title>, kept so the book is displayed under the
        #: name it was published with instead of the model's invented headline.
        self.book_title: str = ""

    async def run(self, source_id: UUID, task_id: UUID) -> None:
        source = (await self.session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        try:
            await self.progress.update(task_id, progress=5, step="download", status="running")
            data = await read_source_bytes(source, source.user_id)
            if not data:
                raise IngestError("The stored file is empty.")

            if source.kind == "video":
                chunks = await self._video_chunks(source, task_id, data)
            elif source.kind == "image":
                chunks = await self._image_chunks(source, task_id, data)
            elif source.kind == "epub":
                chunks = await self._epub_chunks(source, task_id, data)
            else:
                chunks = await self._pdf_chunks(source, task_id, data)

            await self.progress.update(task_id, progress=50, step="persist_chunks")
            await self._replace_chunks(source, chunks)

            await self.progress.update(task_id, progress=60, step="embed")
            await self._embed_chunks(source)

            await self.progress.update(task_id, progress=80, step="knowledge")
            summary, _ = await generate_summary_and_knowledge(
                self.session,
                source_id=source.id,
                user_id=source.user_id,
                router=self.router,
            )
            provider = self.router.provider_for("summarize")
            # A recording keeps its filename: the model title describes the
            # content, and dropping it on top of "lecture-03.mp4" made the file
            # look renamed the moment the import finished. The generated title
            # is still stored on the summary itself.
            #
            # A book keeps its *own* title. `21世纪的21位思想家` was relabelled
            # "当代批判理论家群像：从马克思场域到盖亚入侵" -- a plausible but
            # invented headline -- because the model title overrode the EPUB
            # metadata. Metadata wins; the model only fills a blank.
            if source.kind == "epub":
                source.title = self.book_title or _title_from_filename(source.filename) or summary.title
            elif source.kind != "video":
                source.title = summary.title
            elif not (source.title or "").strip():
                # A row created before upload started seeding `title` would
                # otherwise keep showing nothing at all for the mp4.
                source.title = source.filename
            source.status = "ready"
            source.error_message = None
            if not await self._commit_source():
                return
            note = getattr(self.router, "embed_note", None)
            fallback = getattr(self.router, "fallback_note", None)
            detail = f"LLM: {provider.name} / {provider.model_for('summarize')}"
            if self.extraction_note:
                detail = f"{self.extraction_note} · {detail}"
            if fallback:
                detail += f" · {fallback}"
            if note:
                detail += f" · {note}"
            await self.progress.update(
                task_id,
                progress=100,
                step="done",
                status="succeeded",
                message=detail,
            )
        except Exception as exc:
            detail = str(exc)[:2000]
            # Name the provider that actually failed. Prefixing every error with
            # the summarize (chat) vendor made a SiliconFlow /embeddings 400
            # look like "[deepseek / deepseek-chat] ...".
            task = _failing_task(exc)
            if task:
                try:
                    provider = self.router.provider_for(task)
                    detail = f"[{provider.name} / {provider.model_for(task)}] {detail}"
                except Exception:
                    pass
            source.status = "failed"
            source.error_message = detail
            # A learner may delete the source while it is still parsing. The row
            # is then gone, and flushing the change would raise StaleDataError
            # and spray a traceback for a perfectly reasonable action.
            await self._commit_source()
            await self.progress.update(
                task_id, progress=100, step="failed", status="failed", message=detail
            )
            raise

    # ------------------------------------------------------------------ pdf

    @staticmethod
    def _clean_book_title(name: str | None) -> str:
        return _title_from_filename(name)

    async def _commit_source(self) -> bool:
        """Commit source changes; False when the row was deleted meanwhile.

        Deleting a source mid-parse is legal and even sensible (the learner
        changed their mind). The pipeline must then stop quietly instead of
        failing with "expected to update 1 row(s); 0 were matched".
        """
        try:
            await self.session.commit()
            return True
        except (StaleDataError, ObjectDeletedError, InvalidRequestError) as exc:
            logger.info("source vanished while parsing; dropping its progress (%s)", type(exc).__name__)
            try:
                await self.session.rollback()
                # Drop the stale objects too: a session left holding a modified
                # row that no longer exists warns on teardown ("state will be
                # discarded") and keeps the failure alive past its cause.
                self.session.expunge_all()
            except Exception:
                pass
            return False

    async def _pdf_chunks(self, source: Source, task_id: UUID, data: bytes) -> list[RawChunk]:
        await self.progress.update(task_id, progress=15, step="parse_pdf")

        # Probe the first pages with the layout analyser before committing to a
        # full pass. On a scanned book the text runs are empty, which is a much
        # more reliable "needs OCR" signal than a character count, and it costs
        # a fraction of a full analysis.
        probe = analyse_pdf(data, max_pages=LAYOUT_PROBE_PAGES)
        if probe.has_text():
            doc = probe if not probe.truncated else analyse_pdf(data)
            await self.progress.update(task_id, progress=35, step="layout")
            self._record_structure(source, doc)
            counts = ", ".join(
                f"{kind}:{n}" for kind, n in sorted(doc.tallies.items()) if n
            )
            self.extraction_note = (
                f"text layer ({doc.char_count} chars, {len(doc.outline)} sections"
                + (f", 书内页码 {doc.page_offset:+d}" if doc.page_offset is not None else "")
                + f") · {counts}"
            )
            return chunk_document(doc)

        pages = parse_pdf(data)
        if not looks_textless(pages):
            # Layout extraction produced nothing but plain extraction has text
            # (an unusual content stream). Take the text, lose the structure.
            await self.progress.update(task_id, progress=35, step="chunk")
            self.extraction_note = f"text layer ({document_text_length(pages)} chars, no layout)"
            return chunk_pages([(p.page_number, p.text) for p in pages])

        # No usable text layer: this is a scan or a photo of a page. Re-read the
        # PDF pulling embedded images out, then OCR them.
        await self.progress.update(task_id, progress=20, step="ocr_prepare")
        pages = parse_pdf(data, extract_images=True)
        with_images = [p for p in pages if not p.has_text and p.image]
        if not with_images:
            raise IngestError(
                "This PDF has no text layer and no extractable page images, so there is "
                "nothing to read. Export the pages as PNG/JPG and upload those instead, "
                "or supply a PDF that contains a real text layer."
            )

        engine = get_ocr(self.router)
        settings = get_settings()
        cap = max(1, int(settings.ocr_max_pages or 40))
        selected = with_images[:cap]
        truncated = len(with_images) - len(selected)

        await self.progress.update(
            task_id,
            progress=30,
            step="ocr",
            message=f"OCR {len(selected)} page(s) via {engine.describe()}",
        )
        try:
            result = await engine.transcribe(
                [(p.page_number, p.image) for p in selected],
                user_id=source.user_id,
                source_id=source.id,
            )
        except OcrUnavailable as exc:
            raise IngestError(str(exc)) from exc

        self.extraction_note = f"OCR {result.provider}/{result.model} ({len(selected)} pages)"
        if result.note:
            self.extraction_note += f" · {result.note}"
        if truncated:
            self.extraction_note += f" · {truncated} page(s) beyond OCR_MAX_PAGES skipped"

        pages_out = [(p.page_number, p.text) for p in result.pages if p.text.strip()]
        if not pages_out:
            raise IngestError(
                f"OCR ran via {engine.describe()} but produced no text. "
                "The scan may be too low-resolution or rotated."
            )
        await self.progress.update(task_id, progress=40, step="chunk")
        return self._structure_ocr(source, pages_out)

    # ---------------------------------------------------------------- image

    async def _image_chunks(self, source: Source, task_id: UUID, data: bytes) -> list[RawChunk]:
        engine = get_ocr(self.router)
        await self.progress.update(
            task_id,
            progress=25,
            step="ocr",
            message=f"reading image via {engine.describe()}",
        )
        try:
            result = await engine.transcribe(
                [(1, data)],
                user_id=source.user_id,
                source_id=source.id,
            )
        except OcrUnavailable as exc:
            raise IngestError(str(exc)) from exc

        self.extraction_note = f"OCR {result.provider}/{result.model} ({source.filename})"
        if result.note:
            self.extraction_note += f" · {result.note}"

        pages_out = [(p.page_number, p.text) for p in result.pages if p.text.strip()]
        if not pages_out:
            raise IngestError(
                f"{engine.describe()} returned no text for this image. Try a sharper, "
                "straighter, higher-resolution photo."
            )
        await self.progress.update(task_id, progress=40, step="chunk")
        return self._structure_ocr(source, pages_out)

    # ----------------------------------------------------------------- epub

    async def _epub_chunks(self, source: Source, task_id: UUID, data: bytes) -> list[RawChunk]:
        """EPUB ingestion: XHTML chapters are already structured, so no layout
        heuristics run -- the parser's typed blocks go straight into the shared
        section/outline machinery the PDF path built."""
        await self.progress.update(task_id, progress=15, step="parse_epub")
        try:
            book = parse_epub(data)
        except EpubError as exc:
            raise IngestError(str(exc)) from exc
        # The book knows its own name; the model only writes a summary headline.
        if (book.title or "").strip():
            self.book_title = book.title.strip()

        await self.progress.update(task_id, progress=25, step="epub_images")
        await self._epub_image_blocks(source, book)

        # A chapter is a "page": section paths, the outline and citations all
        # address content by chapter ordinal.
        pages = [
            PageAnalysis(page_number=chapter.number, blocks=chapter.blocks, source="epub")
            for chapter in book.chapters
        ]
        doc = finalise(pages)
        self._record_structure(source, doc)

        counts = ", ".join(
            f"{kind}:{n}" for kind, n in sorted(doc.tallies.items()) if n
        )
        note = f"EPUB 文本层（{len(book.chapters)} 章 / {doc.char_count} 字符 / {len(doc.outline)} 小节）"
        if counts:
            note += f" · {counts}"
        if book.skipped_chapters:
            note += f" · 跳过 {book.skipped_chapters} 个空白/插图章节"
        if self.extraction_note:
            note = f"{self.extraction_note} · {note}"
        self.extraction_note = note

        await self.progress.update(task_id, progress=40, step="chunk")
        chunks = chunk_document(doc)
        return self._relabel_epub_locators(chunks, book)

    async def _epub_image_blocks(self, source: Source, book: EpubBook) -> None:
        """OCR the meaningful illustrations and append them as figure blocks.

        Mirrors the video path's resilience: an unavailable or failing OCR
        engine downgrades to an explanatory note -- the text of the book must
        still reach the learner.
        """
        settings = get_settings()
        cap = max(1, int(settings.ocr_max_pages or 40))
        pending = [
            (chapter, image)
            for chapter in book.chapters
            for image in chapter.images
        ]
        truncated = len(pending) - min(len(pending), cap)
        pending = pending[:cap]
        if not pending:
            return

        try:
            engine = get_ocr(self.router)
            result = await engine.transcribe(
                [(index + 1, image) for index, (_chapter, image) in enumerate(pending)],
                user_id=source.user_id,
                source_id=source.id,
            )
        except Exception as exc:
            self.extraction_note = f"插图未转写（{type(exc).__name__}）"
            return

        by_page = {page.page_number: (page.text or "").strip() for page in result.pages}
        used = 0
        for index, (chapter, _image) in enumerate(pending, start=1):
            text = by_page.get(index, "")
            if len(text) < 4:
                continue
            used += 1
            chapter.blocks.append(
                Block(
                    content=f"[插图转写] {text}",
                    content_type=FIGURE,
                    page_number=chapter.number,
                    page_end=chapter.number,
                )
            )
        note = f"插图 OCR {result.provider}/{result.model}（{used} 张）"
        if truncated > 0:
            note += f" · 超出上限跳过 {truncated} 张"
        self.extraction_note = note

    @staticmethod
    def _relabel_epub_locators(chunks: list[RawChunk], book: EpubBook) -> list[RawChunk]:
        """Swap the page-flavoured locator "p.7" for "ch.7 <chapter title>".

        Implemented in ``epub_parser`` so it stays unit-testable without a
        database session; see ``relabel_chapter_locators``.
        """
        return relabel_chapter_locators(chunks, book)

    def _structure_ocr(
        self, source: Source, pages_out: list[tuple[int, str]]
    ) -> list[RawChunk]:
        """Type and section-tag an OCR transcription.

        Coordinates are gone, so headings come from numbering and captions from
        their label. That is enough to stop figure text and captions from being
        embedded as prose, which is what made OCR'd books read as one long run-on
        paragraph.
        """
        doc = analyse_text_pages(pages_out)
        self._record_structure(source, doc)
        if doc.outline:
            self.extraction_note = (
                f"{self.extraction_note} · {len(doc.outline)} sections detected"
            )
        return chunk_document(doc)

    def _record_structure(self, source: Source, doc) -> None:
        """Persist the page map and section tree so retrieval can use them."""
        source.page_count = doc.total_pages or len(doc.pages)
        source.page_offset = doc.page_offset
        source.section_outline_json = json.dumps(
            [
                {
                    "level": node.level,
                    "number": node.number,
                    "title": node.title,
                    "page_number": node.page_number,
                    "printed_page": node.printed_page,
                    "page_end": node.page_end,
                }
                for node in doc.outline
            ],
            ensure_ascii=False,
        )

    # ---------------------------------------------------------------- video

    async def _video_chunks(self, source: Source, task_id: UUID, data: bytes) -> list[RawChunk]:
        await self.progress.update(task_id, progress=15, step="ffmpeg")
        work, wav, duration = await extract_audio(data)
        try:
            await self.progress.update(task_id, progress=25, step="stt")
            stt = get_stt()
            transcript = await transcribe_audio_resilient(stt, wav)
            segments = list(transcript.segments)
            if not segments:
                raise IngestError(
                    "Speech-to-text produced no segments. The recording may have no "
                    "usable audio track."
                )
            await self.progress.update(
                task_id,
                progress=34,
                step="video_visuals",
                message=transcript.note,
            )
            visual_segments, visual_note = await self._video_visual_segments(
                source, work / "source.mp4", duration
            )
            segments.extend(visual_segments)
            segments.sort(key=lambda segment: (segment.start, segment.end))
            await self.progress.update(task_id, progress=40, step="chapters")
            chapters = await self._video_chapters(source, segments, duration)
            self._record_video_structure(source, chapters, duration)
            note = transcript.note
            if visual_note:
                note += f" · {visual_note}"
            if chapters:
                note += f" · {len(chapters)} chapters"
            if self.extraction_note:
                note = f"{self.extraction_note} · {note}"
            self.extraction_note = note
            return chunk_segments([(s.start, s.end, s.text) for s in segments], chapters)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _video_visual_segments(
        self, source: Source, video_path, duration: float | None
    ) -> tuple[list[TranscriptSegment], str]:
        """Read sampled PPT/whiteboard frames; video ingestion remains usable if OCR is unavailable."""
        try:
            frames = await extract_video_frames(video_path, float(duration or 0.0))
            if not frames:
                return [], "no visual frames"
            engine = get_ocr(self.router)
            result = await engine.transcribe(
                [(index + 1, image) for index, (_timestamp, image) in enumerate(frames)],
                user_id=source.user_id,
                source_id=source.id,
            )
        except Exception as exc:
            return [], f"visual analysis skipped ({type(exc).__name__})"

        by_page = {page.page_number: page.text.strip() for page in result.pages}
        interval = max(1.0, float(duration or 0.0) / max(1, len(frames)))
        segments: list[TranscriptSegment] = []
        previous = ""
        for index, (timestamp, _image) in enumerate(frames, 1):
            text = by_page.get(index, "")
            # Consecutive samples of one unchanged slide add no knowledge and
            # would otherwise dominate retrieval and quiz generation.
            if len(text) < 4 or text == previous:
                continue
            previous = text
            segments.append(
                TranscriptSegment(
                    start=timestamp,
                    end=min(float(duration or timestamp + interval), timestamp + interval),
                    text=f"[画面/PPT/板书] {text}",
                )
            )
        return segments, f"{len(segments)} visual frame(s) via {result.provider}/{result.model}"

    @staticmethod
    def _time_windows(segments: list, char_budget: int = 800, max_windows: int = 60) -> list[list]:
        """Group segments into windows small enough for one summarization call.

        `max_windows` bounds the number of LLM calls: a three-hour recording must
        not turn into three hundred requests. Retrieval still sees every segment
        — only chapter granularity is coarsened.
        """
        windows: list[list] = []
        current: list = []
        chars = 0
        for seg in segments:
            if chars + len(seg.text) > char_budget and current:
                windows.append(current)
                current = []
                chars = 0
            current.append(seg)
            chars += len(seg.text)
        if current:
            windows.append(current)
        if len(windows) > max_windows:
            step = len(windows) / max_windows
            picked = [windows[min(int(i * step), len(windows) - 1)] for i in range(max_windows)]
            picked[-1] = windows[-1]
            windows = picked
        return windows

    async def _summarize_window(self, source: Source, index: int, window: list) -> dict:
        """Map step: one time window -> one summary. Retries only this window."""
        base = {
            "window": index,
            "start": float(window[0].start),
            "end": float(window[-1].end),
        }
        body = "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in window)
        last_exc: Exception | None = None
        for _attempt in range(3):
            try:
                result = await self.router.complete(
                    task="segment_summarize",
                    # The prompt file says "Return JSON only" in so many words.
                    # DeepSeek rejects response_format=json_object otherwise, and
                    # a bare "Return JSON." system line was exactly that bug.
                    messages=[
                        ChatMessage(
                            role="system",
                            content=load_prompt("segment_summarize.v2.txt"),
                        ),
                        ChatMessage(role="user", content=body),
                    ],
                    user_id=source.user_id,
                    source_id=source.id,
                )
                data = parse_json_object(result.content)
                return {
                    **base,
                    "summary": data.get("summary") or result.content[:400],
                    "key_points": data.get("key_points") or [],
                }
            except Exception as exc:  # keep going; one bad window is not fatal
                last_exc = exc
        return {
            **base,
            "summary": " ".join(s.text for s in window)[:400],
            "error": str(last_exc)[:200] if last_exc else "unknown error",
        }

    async def _video_chapters(
        self, source: Source, segments: list, duration: float | None
    ) -> list[tuple[float, float, str]]:
        """Map-reduce the transcript into timestamped chapters.

        Always returns something usable: when the model is unreachable the time
        windows themselves become the chapters, so the player still gets working
        jump targets and the UI says where the structure came from.
        """
        windows = self._time_windows(segments)
        if not windows:
            return []

        # Windows are independent and each is one LLM call, so run a few at a
        # time: a 90-minute lecture used to summarize strictly one window after
        # another, which dominated the import time.
        limit = max(1, int(getattr(get_settings(), "summarize_concurrency", 4) or 4))
        semaphore = asyncio.Semaphore(limit)

        async def guarded(i: int, window: list) -> dict:
            async with semaphore:
                return await self._summarize_window(source, i, window)

        summaries = list(
            await asyncio.gather(*(guarded(i, w) for i, w in enumerate(windows)))
        )
        failed = sum(1 for s in summaries if s.get("error"))
        if failed:
            self.extraction_note = f"{failed}/{len(summaries)} windows summarized locally"

        chapters = await self._reduce_chapters(source, summaries, duration)
        if chapters:
            return chapters

        # Fallback: the windows are already contiguous and timestamped.
        self.extraction_note = (
            f"{self.extraction_note} · " if self.extraction_note else ""
        ) + "chapter titles unavailable, using time windows"
        fallback: list[tuple[float, float, str]] = []
        for i, item in enumerate(summaries):
            start = float(item["start"])
            end = float(item["end"])
            label = str(item.get("summary") or "").strip().replace("\n", " ")
            fallback.append((start, end, label[:28] or f"片段 {i + 1}（{_fmt_time(start)}）"))
        return fallback

    async def _reduce_chapters(
        self, source: Source, summaries: list[dict], duration: float | None
    ) -> list[tuple[float, float, str]]:
        """Reduce step: merge adjacent window summaries into a chapter list."""
        if not summaries:
            return []
        listing = "\n".join(
            f"[{float(s['start']):.0f}-{float(s['end']):.0f}] {str(s.get('summary') or '')[:300]}"
            for s in summaries
        )
        head = (
            "You are given summaries of consecutive time windows of one recorded lesson"
            + (f" (total length {duration:.0f}s). " if duration else ". ")
            + "Merge adjacent windows into 4-12 coherent chapters. Reuse the timestamps "
            "that appear in the input; never invent one.\n\n"
            "Return JSON only:\n"
            '{"chapters":[{"title":"...","start":0.0,"end":0.0,"summary":"..."}]}\n\n'
            "Prompt version: video_chapters.v1"
        )
        try:
            result = await self.router.complete(
                task="summarize",
                messages=[
                    ChatMessage(role="system", content=head),
                    ChatMessage(role="user", content=listing[:6000]),
                ],
                user_id=source.user_id,
                source_id=source.id,
            )
            data = parse_json_object(result.content)
        except Exception:
            return []

        chapters: list[tuple[float, float, str]] = []
        for item in data.get("chapters") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            try:
                start = float(item.get("start"))
                end = float(item.get("end"))
            except (TypeError, ValueError):
                continue
            if not title or end <= start:
                continue
            chapters.append((start, end, title[:120]))
        chapters.sort(key=lambda c: c[0])
        return chapters

    def _record_video_structure(
        self, source: Source, chapters: list[tuple[float, float, str]], duration: float | None
    ) -> None:
        """Persist the chapter tree so the player can jump and the UI can list it.

        Stored as an object rather than a bare list so the recording's duration
        rides along without a schema migration.
        """
        source.page_count = None
        source.page_offset = None
        source.section_outline_json = json.dumps(
            {
                "kind": "video",
                "duration": duration,
                "sections": [
                    {
                        "level": 1,
                        "number": str(i),
                        "title": title,
                        "page_number": 0,
                        "start_time": start,
                        "end_time": end,
                    }
                    for i, (start, end, title) in enumerate(chapters, 1)
                ],
            },
            ensure_ascii=False,
        )

    # -------------------------------------------------------------- shared

    async def _replace_chunks(self, source: Source, raw: list[RawChunk]) -> None:
        existing = (
            (await self.session.execute(select(DocumentChunk).where(DocumentChunk.source_id == source.id)))
            .scalars()
            .all()
        )
        for row in existing:
            await self.session.delete(row)
        await self.session.flush()
        for i, chunk in enumerate(raw):
            self.session.add(
                DocumentChunk(
                    source_id=source.id,
                    user_id=source.user_id,
                    ordinal=i,
                    content=normalise_text(chunk.content),
                    page_number=chunk.page_number,
                    printed_page=chunk.printed_page,
                    start_time=chunk.start_time,
                    end_time=chunk.end_time,
                    locator=normalise_text(chunk.locator or "") or None,
                    section_title=normalise_text(chunk.section_title or "") or None,
                    content_type=chunk.content_type,
                    heading_level=chunk.heading_level,
                )
            )
        await self.session.commit()

    async def _embed_chunks(self, source: Source) -> None:
        chunks = (
            (
                await self.session.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.source_id == source.id)
                    .order_by(DocumentChunk.ordinal)
                )
            )
            .scalars()
            .all()
        )
        batch_size = 16
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            # SiliconFlow BAAI/bge-m3 returns 400/20015 on ~8k+ char inputs.
            # A full-book outline chunk can be 20k chars on a long textbook.
            result = await self.router.embed(
                task="embed",
                texts=[(c.content or "")[:8000] for c in batch],
                user_id=source.user_id,
                source_id=source.id,
            )
            for chunk, vector in zip(batch, result.vectors, strict=False):
                chunk.embedding = vector
        await self.session.commit()


def _failing_task(exc: BaseException) -> str | None:
    """Which routed task is most likely responsible for this ingest exception.

    Getting this wrong is worse than saying nothing: a failed OCR call through
    the vision provider was reported as "[deepseek / deepseek-chat] All
    connection attempts failed" (deepseek had not even been called), which sent
    the learner to check the wrong vendor's key. `ProviderHTTPError` carries the
    real provider and model, so prefer it; the textual heuristics below only run
    when nothing structured is available.
    """
    if isinstance(exc, FFmpegMissing):
        return None
    if isinstance(exc, ProviderHTTPError) and exc.task:
        return exc.task
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, LLMConfigError) and "embed" in lowered:
        return "embed"
    if "/embeddings" in lowered or "embedding" in lowered:
        return "embed"
    if "transcribe_image" in lowered or "vision" in lowered:
        return "transcribe_image"
    if "ffmpeg" in lowered or "ffprobe" in lowered or "winerror 2" in lowered:
        return None
    return "summarize"


def source_readiness_error(source) -> str | None:
    """Chinese reason the learner cannot quiz/tutor this source yet, or None."""
    status = getattr(source, "status", None)
    if status == "ready":
        return None
    if status == "failed":
        err = (getattr(source, "error_message", None) or "").strip()
        extra = f"：{err}" if err else ""
        return (
            f"资料解析失败{extra}。"
            "请回到原文页检查配置（向量模型 / ffmpeg / 语音识别）后重新导入。"
        )
    return "资料仍在解析中，请稍候。解析完成后即可提问或生成练习题。"


__all__ = [
    "IngestError",
    "IngestPipeline",
    "MIN_USABLE_CHARS",
    "source_readiness_error",
]
