"""EPUB parsing: chapters, typed blocks, and embedded images.

The PDF path reconstructs structure that a typesetter encoded into geometry
(baselines, indents, running heads). An EPUB needs none of that: the book is a
ZIP of XHTML files whose tags *are* the structure -- ``<h1>`` is a heading, a
``<p>`` is a paragraph, the spine is the reading order. This module therefore
does no heuristic layout work; it maps XHTML elements onto the same
``Block`` model the PDF analyser produces, so the downstream pipeline
(section assignment, outline, chunking, retrieval) runs unchanged.

Deliberately standard-library only (zipfile / xml.etree / html.parser):
the EPUB format has been stable since 2011, XHTML chapters must be
well-formed, and avoiding new dependencies keeps deployments (Windows
local-first, docker) exactly as they are. If a future batch of badly-built
books demands BeautifulSoup-grade tolerance, swap the ``_ChapterParser``
internals; the output contract stays the same.

Guard rails, in the order they fire:

* corrupt ZIP container          -> ``EpubCorrupt``
* DRM (``META-INF/encryption.xml``) -> ``EpubUnsupported``
* fixed-layout books (comics)    -> ``EpubUnsupported``
* empty/illustration-only chapters -> skipped, counted in ``skipped_chapters``

Page-number semantics: a chapter is a "page". ``Block.page_number`` is the
1-based chapter ordinal, which ``IngestPipeline`` relabels from ``p.`` to
``ch.`` in chunk locators so citations read "ch.3 第3章" instead of a
misleading "p.3".
"""

from __future__ import annotations

import logging
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO

from app.services.layout import (
    BODY,
    CAPTION,
    HEADING,
    TABLE,
    Block,
)

logger = logging.getLogger("app.epub")

#: Below this many characters a chapter is an empty shell (cover, copyright
#: page, or an image-only spread). Embedding it would pollute retrieval with
#: "[image]"-style noise, so such chapters are skipped and counted.
MIN_CHAPTER_CHARS = 100

#: Images smaller than this are almost always bullets, logos and rules
#: (same threshold the PDF path uses for embedded images).
MIN_IMAGE_BYTES = 4096

#: Raster formats the downstream OCR stack (Pillow + RapidOCR) can read.
#: GIF (often animated) and SVG (vector) are excluded on purpose.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

_XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}

_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
#: Elements that open a text block of their own. Everything else is either a
#: container (recursed into implicitly by the streaming parser) or skipped.
_BLOCK_TAGS = {"p", "li", "blockquote", "pre", "figcaption"}
#: Elements whose entire subtree is noise for a reader.
_SKIP_TAGS = {"script", "style", "svg", "head", "template"}


class EpubError(RuntimeError):
    """Base class. ``str(exc)`` is a learner-facing Chinese message."""


class EpubUnsupported(EpubError):
    """A structurally valid EPUB this pipeline refuses (DRM, fixed-layout)."""


class EpubCorrupt(EpubError):
    """Not a readable EPUB at all."""


# --------------------------------------------------------------------- model


@dataclass
class EpubChapter:
    number: int  # 1-based ordinal over the *kept* chapters
    href: str
    title: str
    blocks: list[Block] = field(default_factory=list)
    #: Raster images worth OCRing, in document order.
    images: list[bytes] = field(default_factory=list)


@dataclass
class EpubBook:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    chapters: list[EpubChapter] = field(default_factory=list)
    skipped_chapters: int = 0

    @property
    def char_count(self) -> int:
        return sum(len(b.content) for ch in self.chapters for b in ch.blocks)


# ------------------------------------------------------------- HTML parsing


class _ChapterParser(HTMLParser):
    """Streaming XHTML -> typed ``Block`` list.

    One pass, no DOM: buffers are opened by block-level start tags and closed
    by the matching end tag. Text with no open buffer (stray text directly
    inside a ``<div>``) lazily opens an implicit body buffer so nothing
    readable is lost. Tables are flattened to ``cell | cell`` rows, matching
    what the PDF grid renderer emits.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.image_hrefs: list[str] = []
        self.first_heading: str = ""
        self._skip_depth = 0
        self._buf: list[str] = []
        self._tag: str | None = None
        self._kind: str | None = None
        self._level: int | None = None
        self._preserve = False
        # table state
        self._rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    # -- helpers ----------------------------------------------------------

    def _flush(self) -> None:
        if self._tag is None:
            self._buf.clear()
            return
        text = "".join(self._buf)
        if not self._preserve:
            text = " ".join(text.split())
        if text:
            if self._kind == HEADING:
                if not self.first_heading:
                    self.first_heading = text
                self.blocks.append(
                    Block(
                        content=text,
                        content_type=HEADING,
                        page_number=0,
                        heading_level=self._level or 2,
                        heading_number="",
                        heading_title=text,
                    )
                )
            else:
                self.blocks.append(
                    Block(
                        content=text,
                        content_type=self._kind or BODY,
                        page_number=0,
                    )
                )
        self._buf.clear()
        self._tag = None
        self._kind = None
        self._level = None
        self._preserve = False

    def _flush_table(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        if self._row is not None and any(cell.strip() for cell in self._row):
            self._rows.append(self._row)
        if self._rows:
            self.blocks.append(
                Block(
                    content="\n".join(" | ".join(r) for r in self._rows),
                    content_type=TABLE,
                    page_number=0,
                )
            )
        self._rows, self._row, self._cell = [], None, None

    # -- HTMLParser events -------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "img":
            src = dict(attrs).get("src")
            if src:
                self.image_hrefs.append(src)
            return
        if tag == "br":
            if self._tag is not None or self._cell is not None:
                self._buf.append(" ")
            return
        if self._cell is not None:  # inside a table cell: text stays inline
            return
        if self._row is not None or self._rows:
            if tag == "tr":
                if self._row is not None and any(c.strip() for c in self._row):
                    self._rows.append(self._row)
                self._row = []
                return
            if tag in ("td", "th"):
                self._cell = []
                self._buf.clear()
                return
            if tag == "table":
                return  # nested table: fold into the outer one
            return
        if tag == "table":
            self._flush()
            self._rows, self._row, self._cell = [], [], None
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self._tag, self._kind, self._level = tag, HEADING, _HEADING_TAGS[tag]
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._tag = tag
            self._kind = CAPTION if tag == "figcaption" else BODY
            self._preserve = tag == "pre"
            return
        # container tags (div, section, article, ...) need no state: either a
        # block tag follows and opens a buffer, or loose text opens one lazily.

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth -= 1
            return
        if self._cell is not None:
            if tag in ("td", "th"):
                if self._row is None:
                    self._row = []
                self._row.append(" ".join("".join(self._cell).split()))
                self._cell = None
                self._buf.clear()
            elif tag in ("tr", "table"):  # malformed cell: close what we have
                self.handle_endtag("td")
                self.handle_endtag(tag)
            return
        if self._row is not None or self._rows:
            if tag == "tr":
                if self._row is not None and any(c.strip() for c in self._row):
                    self._rows.append(self._row)
                self._row = None
            elif tag == "table":
                self._flush_table()
            return
        if self._tag is not None and tag == self._tag:
            self._flush()

    def handle_data(self, data):
        if self._skip_depth or not data.strip():
            return
        if self._cell is not None:
            self._cell.append(data)
            return
        if self._tag is None:
            if self._rows:  # stray text between rows: ignore
                return
            self._tag, self._kind, self._level, self._preserve = (
                "text",
                BODY,
                None,
                False,
            )
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()
        self._flush_table()


class _AnchorParser(HTMLParser):
    """Collect (href, text) pairs from a nav document, best-effort."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._buf).split())
            if text:
                self.anchors.append((self._href, text))
            self._href = None

    def close(self) -> None:
        super().close()
        if self._href is not None and self._buf:
            text = " ".join("".join(self._buf).split())
            if text:
                self.anchors.append((self._href, text))


# ----------------------------------------------------------- OPF structure


def _read_opf_path(zf: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(zf.read("META-INF/container.xml"))
    except KeyError:
        raise EpubCorrupt("缺少 META-INF/container.xml，不是标准的 EPUB 文件。") from None
    except ET.ParseError:
        raise EpubCorrupt("container.xml 损坏，无法定位书籍目录文件。") from None
    for el in root.iter():
        if el.tag.endswith("}rootfile") or el.tag == "rootfile":
            path = el.get("full-path")
            if path:
                return path
    raise EpubCorrupt("container.xml 中没有指明 OPF 包描述文件（rootfile）。")


def _is_fixed_layout(opf: ET.Element, opf_bytes: bytes) -> bool:
    """EPUB3 stores this as a property element with text, older books as a
    legacy name/content meta -- both shapes are checked."""
    for el in opf.iter():
        if el.get("property") == "rendition:layout":
            if (el.text or "").strip() == "fixed" or el.get("content") == "fixed":
                return True
        if el.get("name") == "fixed-layout" and el.get("content") == "true":
            return True
    text = opf_bytes.decode("utf-8", "ignore")
    return bool(
        re.search(
            r"""property=["']rendition:layout["'][^>]*content=["']fixed["']""", text
        )
        or re.search(
            r"""name=["']fixed-layout["'][^>]*content=["']true["']""", text
        )
    )


def _read_manifest(
    opf: ET.Element, opf_path: str
) -> dict[str, tuple[str, str, str]]:
    """id -> (media-type, zip path, properties).

    Manifest hrefs are relative to the OPF file, which rarely lives at the
    archive root, so the OPF's directory is prepended before anything touches
    the ZIP.
    """
    base = posixpath.dirname(opf_path)
    items: dict[str, tuple[str, str, str]] = {}
    for el in opf.iter():
        if not (el.tag.endswith("}item") or el.tag == "item"):
            continue
        iid, href = el.get("id"), el.get("href")
        if not iid or not href:
            continue
        # Percent-encoded hrefs are legal ("my%20chapter.xhtml"); unquote
        # before touching the ZIP.
        href = urllib.parse.unquote(href)
        path = posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)
        items[iid] = (el.get("media-type") or "", path, el.get("properties") or "")
    return items


def _read_spine(opf: ET.Element) -> list[str]:
    idrefs: list[str] = []
    for el in opf.iter():
        if el.tag.endswith("}itemref") or el.tag == "itemref":
            idref = el.get("idref")
            if idref:
                idrefs.append(idref)
    return idrefs


def _read_metadata(opf: ET.Element) -> tuple[str, list[str]]:
    title = ""
    for el in opf.iter():
        if el.tag.endswith("}title") and (el.text or "").strip() and not title:
            title = el.text.strip()
    authors = [
        (el.text or "").strip()
        for el in opf.iter()
        if el.tag.endswith("}creator") and (el.text or "").strip()
    ]
    return title, authors


def _toc_titles(
    zf: zipfile.ZipFile,
    opf: ET.Element,
    items: dict[str, tuple[str, str, str]],
) -> dict[str, str]:
    """href (zip path) -> chapter title, from the nav (EPUB3) or NCX (EPUB2)."""

    def _relative(href: str, base_file: str) -> str:
        """Anchor hrefs are relative to the file that declares them."""
        base = posixpath.dirname(base_file)
        return posixpath.normpath(
            posixpath.join(base, urllib.parse.unquote(href.split("#")[0]))
            if base
            else urllib.parse.unquote(href.split("#")[0])
        )

    try:
        for _media, href, props in items.values():
            if "nav" in props.split() and href in zf.namelist():
                parser = _AnchorParser()
                parser.feed(zf.read(href).decode("utf-8", "replace"))
                parser.close()
                out: dict[str, str] = {}
                for link, text in parser.anchors:
                    path = _relative(link, href)
                    if path and path not in out:
                        out[path] = text
                if out:
                    return out
        # EPUB2: the spine's toc attribute names the NCX.
        ncx_href = ""
        for el in opf.iter():
            if el.tag.endswith("}spine") or el.tag == "spine":
                ncx_id = el.get("toc")
                if ncx_id and ncx_id in items:
                    ncx_href = items[ncx_id][1]
                break
        if not ncx_href:
            for media, href, _props in items.values():
                if media == "application/x-dtbncx+xml":
                    ncx_href = href
                    break
        if not ncx_href or ncx_href not in zf.namelist():
            return {}
        ncx = ET.fromstring(zf.read(ncx_href))
        out = {}
        for point in ncx.iter():
            if not (point.tag.endswith("}navPoint") or point.tag == "navPoint"):
                continue
            label = src = ""
            for child in point.iter():
                if child.tag.endswith("}text") and not label:
                    label = (child.text or "").strip()
                if child.tag.endswith("}content") and not src:
                    src = (child.get("src") or "").strip()
            path = _relative(src, ncx_href) if src else ""
            if path and label and path not in out:
                out[path] = label
        return out
    except Exception as exc:  # TOC is enrichment, never load-bearing
        logger.debug("epub toc parsing skipped (%s)", exc)
        return {}


def _load_images(
    zf: zipfile.ZipFile, chapter_href: str, srcs: list[str]
) -> list[bytes]:
    out: list[bytes] = []
    base = posixpath.dirname(chapter_href)
    for src in srcs:
        if src.startswith(("http://", "https://", "data:")):
            continue
        path = posixpath.normpath(
            posixpath.join(base, urllib.parse.unquote(src.split("#")[0]))
        )
        if not path.lower().endswith(IMAGE_SUFFIXES):
            continue
        try:
            raw = zf.read(path)
        except KeyError:
            continue
        if len(raw) >= MIN_IMAGE_BYTES:
            out.append(raw)
    return out


# ------------------------------------------------------------------- public


def parse_epub(data: bytes) -> EpubBook:
    """Parse an EPUB into chapters of typed blocks.

    Raises ``EpubUnsupported`` / ``EpubCorrupt`` with learner-facing
    messages; the caller turns those into an ingest failure.
    """
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise EpubCorrupt("无法读取该 EPUB：不是有效的 ZIP 容器，文件可能已损坏。") from None

    with zf:
        names = set(zf.namelist())
        if "META-INF/encryption.xml" in names or "META-INF/rights.xml" in names:
            raise EpubUnsupported(
                "该 EPUB 受版权保护（DRM 加密），无法解析。请使用未加密的文件。"
            )

        opf_path = _read_opf_path(zf)
        try:
            opf_bytes = zf.read(opf_path)
        except KeyError:
            raise EpubCorrupt(f"包描述文件 {opf_path} 缺失，EPUB 不完整。") from None
        try:
            opf = ET.fromstring(opf_bytes)
        except ET.ParseError:
            raise EpubCorrupt("OPF 包描述文件损坏，无法读取书籍结构。") from None

        if _is_fixed_layout(opf, opf_bytes):
            raise EpubUnsupported(
                "这是一本 fixed-layout（固定版式）EPUB（常见于漫画/绘本），"
                "页面内容以绝对定位图片为主，暂不支持。请导出为 PDF 或图片后上传。"
            )

        title, authors = _read_metadata(opf)
        items = _read_manifest(opf, opf_path)
        spine = _read_spine(opf)
        toc = _toc_titles(zf, opf, items)

        book = EpubBook(title=title, authors=authors)
        for idref in spine:
            entry = items.get(idref)
            if not entry:
                continue
            media, href, _props = entry
            if media not in _XHTML_MEDIA_TYPES and not href.lower().endswith(
                (".xhtml", ".html", ".htm")
            ):
                continue
            try:
                html = zf.read(href).decode("utf-8", "replace")
            except KeyError:
                continue

            parser = _ChapterParser()
            try:
                parser.feed(html)
                parser.close()
            except Exception as exc:  # one broken chapter is not the book
                logger.warning("epub chapter %s unreadable (%s)", href, exc)
                continue

            text_len = sum(len(b.content) for b in parser.blocks)
            images = _load_images(zf, href, parser.image_hrefs)
            if text_len < MIN_CHAPTER_CHARS and not images:
                book.skipped_chapters += 1
                continue

            book.chapters.append(
                EpubChapter(
                    number=0,  # renumbered below over the kept chapters
                    href=href,
                    title=toc.get(href) or parser.first_heading or "",
                    blocks=parser.blocks,
                    images=images,
                )
            )

    if not book.chapters:
        raise EpubCorrupt(
            "EPUB 中没有可读取的章节内容（spine 为空或正文全部为空）。"
        )

    for number, chapter in enumerate(book.chapters, start=1):
        chapter.number = number
        for block in chapter.blocks:
            block.page_number = number
            block.page_end = number
    return book


def chapter_title_map(book: EpubBook) -> dict[int, str]:
    """Chapter ordinal -> display title, for locator relabelling."""
    return {
        ch.number: (ch.title or f"第 {ch.number} 部分").strip() for ch in book.chapters
    }


def relabel_chapter_locators(chunks: list, book: EpubBook) -> list:
    """Swap the page-flavoured locator "p.7" for "ch.7 <chapter title>".

    Chunks come out of the shared chunker with PDF vocabulary; a learner
    citing an EPUB needs the chapter, not a page that does not exist.
    Kept duck-typed (``page_number`` / ``locator``) so tests need no DB.
    """
    titles = chapter_title_map(book)
    for chunk in chunks:
        number = getattr(chunk, "page_number", None)
        locator = getattr(chunk, "locator", None) or ""
        if number is None:
            continue
        prefix = f"p.{number}"
        if locator.startswith(prefix):
            title = titles.get(number, "")
            suffix = locator[len(prefix):]
            setattr(
                chunk,
                "locator",
                f"ch.{number}" + (f" {title}" if title else "") + suffix,
            )
    return chunks


__all__ = [
    "EpubBook",
    "EpubChapter",
    "EpubCorrupt",
    "EpubError",
    "EpubUnsupported",
    "MIN_CHAPTER_CHARS",
    "MIN_IMAGE_BYTES",
    "chapter_title_map",
    "parse_epub",
    "relabel_chapter_locators",
]
