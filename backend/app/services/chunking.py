from __future__ import annotations

from dataclasses import dataclass

from app.services.front_matter import is_front_matter_section
from app.services.layout import (
    ATOMIC_TYPES,
    BODY,
    CAPTION,
    HEADING,
    OUTLINE,
    RETRIEVABLE_TYPES,
    TYPE_LABELS,
    Block,
    DocumentLayout,
    outline_text,
)


@dataclass
class RawChunk:
    content: str
    page_number: int | None = None
    start_time: float | None = None
    end_time: float | None = None
    locator: str | None = None
    #: Page as printed on the paper, when the running heads revealed the offset.
    printed_page: int | None = None
    #: "第3章 指令级并行 > 3.2 分支预测", empty when the document has no headings.
    section_title: str = ""
    content_type: str = BODY
    heading_level: int | None = None


def _split_window(text: str, size: int = 900, overlap: int = 120) -> list[str]:
    text = " ".join(text.split())
    if len(text) <= size:
        return [text] if text else []
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        parts.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return parts


def _split_paragraphs(text: str, size: int, overlap: int) -> list[str]:
    """Split on blank lines first, then by window.

    A paragraph is the smallest unit a reader would quote, so keeping its edges
    intact matters more than hitting an exact chunk size. Only paragraphs longer
    than ``size`` are windowed, and then with overlap so a fact straddling the
    cut is still retrievable from one of the two halves.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    parts: list[str] = []
    buffer = ""
    for block in blocks:
        if len(block) > size:
            if buffer:
                parts.extend(_split_window(buffer, size, overlap))
                buffer = ""
            parts.extend(_split_window(block, size, overlap))
            continue
        candidate = f"{buffer}\n\n{block}" if buffer else block
        if len(candidate) > size:
            if buffer:
                parts.extend(_split_window(buffer, size, overlap))
            buffer = block
        else:
            buffer = candidate
    if buffer:
        parts.extend(_split_window(buffer, size, overlap))
    return parts


def _locator(page_number: int | None, printed_page: int | None, section: str, kind: str) -> str:
    """Human-facing pointer. Prefers the printed page, since that is what the
    learner sees on the paper and what they will type into a question."""
    bits: list[str] = []
    if printed_page is not None:
        bits.append(f"书内 p.{printed_page}")
        if page_number is not None and page_number != printed_page:
            bits.append(f"PDF p.{page_number}")
    elif page_number is not None:
        bits.append(f"p.{page_number}")
    label = TYPE_LABELS.get(kind, kind)
    if kind not in (BODY, HEADING):
        bits.append(label)
    return " · ".join(bits)


def chunk_pages(pages: list[tuple[int, str]]) -> list[RawChunk]:
    """Legacy path: page -> text, no structure. Kept for callers that only have
    plain text (and for the corrupt-PDF fallback)."""
    chunks: list[RawChunk] = []
    for page_number, text in pages:
        for part in _split_window(text):
            chunks.append(
                RawChunk(
                    content=part,
                    page_number=page_number,
                    locator=f"p.{page_number}",
                    content_type=BODY,
                )
            )
    return chunks or [RawChunk(content="[no text]", page_number=1, locator="p.1")]


def chapter_of(
    start: float, chapters: list[tuple[float, float, str]]
) -> tuple[str, int | None]:
    """Which chapter a timestamp belongs to. Returns (title, heading_level)."""
    for chapter_start, chapter_end, title in chapters:
        if chapter_start <= start < chapter_end:
            return title, 1
    return "", None


def video_outline_chunk(
    chapters: list[tuple[float, float, str]], duration: float | None = None
) -> RawChunk | None:
    """Up-front chunk holding the chapter list of a recording.

    Mirrors `outline_chunk` for books: "这节课一共讲了什么？" is answerable only
    when the shape of the recording is retrievable, not reconstructed by luck
    from a handful of cosine-similar fragments.
    """
    if not chapters:
        return None
    header = "视频章节大纲（章节 / 标题 / 时间点）"
    if duration:
        header += f"；总时长 {_fmt(duration)}"
    body = "\n".join(
        f"第{i}章 {title}（{_fmt(start)}–{_fmt(end)}）"
        for i, (start, end, title) in enumerate(chapters, 1)
    )
    return RawChunk(
        content=f"{header}\n{body}",
        start_time=chapters[0][0],
        end_time=chapters[-1][1],
        locator="章节大纲",
        content_type=OUTLINE,
    )


def chunk_segments(
    segments: list[tuple[float, float, str]],
    chapters: list[tuple[float, float, str]] | None = None,
) -> list[RawChunk]:
    """Transcript windows -> chunks, tagged with the chapter they fall in.

    Without the chapter tagging every video chunk arrives unlabelled, so an
    answer can cite "12:31" but never "第三章 · 分支预测" — and the structure
    inspector has nothing to show.
    """
    chapters = chapters or []
    chunks: list[RawChunk] = []

    front = video_outline_chunk(chapters)
    if front is not None:
        chunks.append(front)

    for start, end, text in segments:
        section_title, heading_level = chapter_of(start, chapters)
        for part in _split_window(text, size=700, overlap=80):
            chunks.append(
                RawChunk(
                    content=part,
                    start_time=start,
                    end_time=end,
                    locator=f"{_fmt(start)}–{_fmt(end)}",
                    content_type=BODY,
                    section_title=section_title,
                    heading_level=heading_level,
                )
            )
    return chunks or [
        RawChunk(content="[no transcript]", start_time=0, end_time=0, locator="0:00–0:00")
    ]


def _fmt(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def outline_chunk(doc: DocumentLayout) -> RawChunk | None:
    """A single up-front chunk holding the whole section tree.

    This is what makes "这本书每章讲了什么？" answerable: instead of hoping five
    cosine-similar fragments happen to reconstruct the book's shape, the model is
    handed the shape itself, with page ranges.
    """
    body = outline_text(doc)
    if not body:
        return None
    header = "全书目录大纲（层级 / 标题 / 页码）"
    if doc.page_offset is not None and doc.printed_range:
        header += f"；书内页码 = PDF 页码 {doc.page_offset:+d}"
    return RawChunk(
        content=f"{header}\n{body}",
        page_number=1,
        printed_page=None,
        locator="全书大纲",
        content_type=OUTLINE,
    )


def chunk_document(doc: DocumentLayout) -> list[RawChunk]:
    """Turn typed blocks into chunks, preserving type, section and page.

    Ordering rule that keeps retrieval honest: a figure/table/formula chunk opens
    with its caption, so a semantic match on the caption's words lands on the
    object it describes instead of on a bare wall of cell text.
    """
    chunks: list[RawChunk] = []

    front = outline_chunk(doc)
    if front is not None:
        if len(front.content) > 4000:
            for i, part in enumerate(
                _split_window(front.content, size=4000, overlap=200), start=1
            ):
                chunks.append(
                    RawChunk(
                        content=part,
                        page_number=front.page_number,
                        printed_page=front.printed_page,
                        locator=f"{front.locator}#{i}" if front.locator else f"outline#{i}",
                        section_title=front.section_title,
                        content_type=front.content_type,
                        heading_level=front.heading_level,
                    )
                )
        else:
            chunks.append(front)

    last_caption = ""
    last_caption_page = -1
    #: Section the reader is currently inside. Blocks that arrive without their
    #: own `section_path` (a paragraph two pages after its heading, an appendix
    #: table) inherit it, so no chunk is ever section-less. Without this, "跳到
    #: 对应章节" had nothing to jump to and sections looked shuffled.
    current_section = ""
    current_level: int | None = None

    for block in doc.blocks:
        if block.content_type == CAPTION:
            last_caption = block.content.strip() or last_caption
            last_caption_page = block.page_number
        if block.content_type not in RETRIEVABLE_TYPES:
            continue
        content = block.content.strip()
        if not content:
            continue
        if block.content_type == HEADING:
            current_section = block.section_path or content
            current_level = block.heading_level
        elif block.section_path:
            # The parser knows better than the running state (e.g. it matched a
            # printed TOC entry): trust it and carry it forward.
            current_section = block.section_path
            current_level = block.heading_level or current_level
        section_path = block.section_path or current_section
        heading_level = block.heading_level or (current_level if section_path else None)

        if block.content_type in ATOMIC_TYPES:
            # Headings, captions, formulas and table/figure grids are single
            # units: splitting them would separate a label from its object.
            pieces = [content]
        else:
            pieces = _split_paragraphs(content, 900, 120)

        # Fall back to the nearest caption above when the parser could not
        # associate one: a figure is meaningless without its label. The fallback
        # must not cross a page boundary -- a caption left over from the previous
        # page belongs to a different figure, and prefixing it mislabels this one.
        caption = block.parent_caption or (
            last_caption
            if block.content_type in ("table", "figure", "formula")
            and last_caption_page == block.page_number
            else ""
        )

        for piece in pieces:
            body = piece
            if caption and block.content_type in ("table", "figure", "formula"):
                body = f"{caption}\n{piece}"
            if section_path:
                body = f"【{section_path}】\n{body}"
            chunks.append(
                RawChunk(
                    content=body,
                    page_number=block.page_number,
                    printed_page=block.printed_page,
                    section_title=section_path,
                    content_type=block.content_type,
                    locator=_locator(
                        block.page_number, block.printed_page, section_path, block.content_type
                    ),
                    heading_level=heading_level,
                )
            )
    return chunks or [RawChunk(content="[no text]", page_number=1, locator="p.1")]


def format_excerpts(chunks: list, *, max_chars: int = 6000) -> str:
    """Build a prompt body with chunk ids *and structure* for traceability.

    The header now carries the section and the content type. Without them the
    summariser sees a flat bag of text and produces the scattered outline the
    learner complained about; with them it can say which chapter a point belongs
    to and can treat a formula as a formula.
    """
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        content_type = getattr(chunk, "content_type", BODY) or BODY
        section = getattr(chunk, "section_title", "") or ""
        header = f"[chunk_id={chunk.id} locator={chunk.locator or ''} type={content_type}]"
        if section:
            header += f"\n[section={section}]"
        body = f"{header}\n{chunk.content}"
        if used + len(body) > max_chars and parts:
            break
        parts.append(body)
        used += len(body)
    return "\n\n".join(parts)


def _excerpt_block(chunk) -> str:
    content_type = getattr(chunk, "content_type", BODY) or BODY
    section = getattr(chunk, "section_title", "") or ""
    printed = getattr(chunk, "printed_page", None)
    page = getattr(chunk, "page_number", None)
    header = f"[chunk_id={chunk.id} locator={chunk.locator or ''} type={content_type}"
    if printed is not None:
        header += f" printed_page={printed}"
    if page is not None and page != printed:
        header += f" pdf_page={page}"
    header += "]"
    if section:
        header += f"\n[section={section}]"
    return f"{header}\n{chunk.content}"


def format_document_excerpts(chunks: list, *, max_chars: int = 10000) -> str:
    """Sample a whole document by structure, not by position.

    The previous behaviour took the first ``max_chars`` of the chunk list. On a
    649-page book that is the cover, the CIP page and the preface -- which is why
    the generated summary described nothing and the outline was five unrelated
    topics. Here the budget goes, in order, to:

    1. the document's own table of contents (type ``outline``),
    2. every heading, which *is* the section structure,
    3. an even stride through the rest, so chapter 12 is as likely to be
       represented as chapter 1.
    """
    if not chunks:
        return ""

    def kind(chunk) -> str:
        return getattr(chunk, "content_type", BODY) or BODY

    parts: list[str] = []
    used = 0

    def add(chunk) -> bool:
        nonlocal used
        block = _excerpt_block(chunk)
        if used + len(block) > max_chars:
            return False
        parts.append(block)
        used += len(block)
        return True

    outline = [c for c in chunks if kind(c) == OUTLINE]
    headings = [
        c
        for c in chunks
        if kind(c) == HEADING
        and not is_front_matter_section(getattr(c, "section_title", None) or c.content)
    ]
    rest = [
        c
        for c in chunks
        if kind(c) not in (OUTLINE, HEADING)
        and not is_front_matter_section(getattr(c, "section_title", None) or "")
    ]

    for chunk in outline:
        add(chunk)

    # Headings are short and carry the structure; cap them so a 200-section book
    # cannot crowd out the prose that explains the sections.
    heading_budget = int(max_chars * 0.45)
    heading_used = 0
    for chunk in headings:
        block = _excerpt_block(chunk)
        if heading_used + len(block) > heading_budget:
            break
        if not add(chunk):
            break
        heading_used += len(block)

    if rest and used < max_chars:
        remaining = max_chars - used
        buckets: dict[str, list] = {}
        order: list[str] = []
        for chunk in rest:
            key = getattr(chunk, "section_title", "") or ""
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(chunk)

        # Evenly pick sections across the book so chapter N is not crowded out
        # by a long preface. Always keep the first and last named sections.
        section_slots = max(1, remaining // 280)
        if len(order) <= section_slots:
            chosen_keys = order
        else:
            step = len(order) / section_slots
            idxs = {0, len(order) - 1}
            for i in range(section_slots):
                idxs.add(min(int(i * step), len(order) - 1))
            chosen_keys = [order[i] for i in sorted(idxs)]

        queues: list[list] = []
        for key in chosen_keys:
            group = buckets[key]
            stride = max(1, len(group) // 4)
            queues.append(group[::stride] or group[:1])

        # First and last chosen sections are reserved so a long preface cannot
        # spend the budget before chapter N is seen.
        reserved: list[list] = []
        fill: list[list] = []
        if queues:
            reserved.append(queues[0])
        if len(queues) > 1:
            reserved.append(queues[-1])
        if len(queues) > 2:
            fill = queues[1:-1]

        for group in reserved + fill:
            if group and not add(group[0]):
                break
        idx = 1
        while used < max_chars:
            progressed = False
            for group in reserved + fill:
                if idx < len(group) and add(group[idx]):
                    progressed = True
                if used >= max_chars:
                    break
            if not progressed:
                break
            idx += 1
    return "\n\n".join(parts)


__all__ = [
    "CAPTION",
    "HEADING",
    "OUTLINE",
    "TYPE_LABELS",
    "RawChunk",
    "chapter_of",
    "chunk_document",
    "chunk_pages",
    "chunk_segments",
    "format_document_excerpts",
    "format_excerpts",
    "outline_chunk",
    "video_outline_chunk",
]
