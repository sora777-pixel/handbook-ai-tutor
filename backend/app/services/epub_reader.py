"""Open an EPUB for *rendering* (as opposed to ingest).

`epub_parser.parse_epub` answers "what should the model learn from this book":
plain blocks, no markup, images filtered by size. A reader needs the opposite
-- the chapter's own XHTML, untouched, so the browser can lay it out.

So this module is deliberately thin: it reuses the same ZIP/OPF spine walking
and the same guard rails (DRM, fixed-layout), and additionally keeps each
chapter's raw XHTML for the frontend to render.
"""

from __future__ import annotations

import posixpath
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from io import BytesIO

from app.services.epub_parser import (
    EpubCorrupt,
    EpubUnsupported,
    _is_fixed_layout,
    _read_manifest,
    _read_metadata,
    _read_opf_path,
    _read_spine,
    _toc_titles,
    _XHTML_MEDIA_TYPES,
)

# Re-exported so the API layer has one import for "this book cannot be shown".
EpubReadError = (EpubCorrupt, EpubUnsupported)


@dataclass
class RenderChapter:
    index: int
    href: str
    title: str
    html: str
    text: str = ""


@dataclass
class RenderBook:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    chapters: list[RenderChapter] = field(default_factory=list)
    #: Archive path of the cover image, when the book declares one.
    cover_href: str = ""


def _cover_href(opf: ET.Element, items: dict[str, tuple[str, str, str]], names: set[str]) -> str:
    """Find the cover image the book says it has.

    Three conventions, in decreasing reliability:
      1. EPUB 3: a manifest item with `properties="cover-image"`.
      2. EPUB 2: `<meta name="cover" content="cover-image-id"/>` pointing at an
         item id.
      3. Nothing declared: fall back to an image whose id or href says "cover".

    Returns "" when the book genuinely has no cover -- better an absent picture
    than the wrong page (the first illustration is often a title page).
    """
    image_types = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}

    for _id, (media, href, props) in items.items():
        if "cover-image" in (props or "") and media in image_types:
            return href

    for el in opf.iter():
        if (el.tag.endswith("}meta") or el.tag == "meta") and el.get("name") == "cover":
            content = (el.get("content") or "").strip()
            entry = items.get(content)
            if entry and entry[0] in image_types:
                return entry[1]

    for _id, (media, href, _props) in items.items():
        if media not in image_types:
            continue
        lowered = f"{_id} {href}".lower()
        if "cover" in lowered and href in names:
            return href
    return ""


def open_epub(data: bytes) -> RenderBook:
    """Parse an EPUB into renderable chapters, or raise EpubCorrupt/EpubUnsupported."""
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        raise EpubCorrupt("无法读取该EPUB：不是有效的 ZIP 容器。") from None

    with zf:
        names = set(zf.namelist())
        if "META-INF/encryption.xml" in names or "META-INF/rights.xml" in names:
            raise EpubUnsupported("该 EPUB 受版权保护（DRM 加密），无法阅读。")

        opf_path = _read_opf_path(zf)
        try:
            opf_bytes = zf.read(opf_path)
        except KeyError:
            raise EpubCorrupt(f"包描述文件 {opf_path} 缺失。") from None
        try:
            opf = ET.fromstring(opf_bytes)
        except ET.ParseError:
            raise EpubCorrupt("OPF 损坏，无法读取书籍结构。") from None
        if _is_fixed_layout(opf, opf_bytes):
            raise EpubUnsupported("这是一本固定版式（漫画/绘本）EPUB，暂不支持在浏览器内阅读。")

        title, authors = _read_metadata(opf)
        items = _read_manifest(opf, opf_path)
        spine = _read_spine(opf)
        toc = _toc_titles(zf, opf, items)

        book = RenderBook(title=title, authors=authors)
        book.cover_href = _cover_href(opf, items, names)
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
            body = _extract_body(html)
            if not body.strip():
                continue
            book.chapters.append(
                RenderChapter(
                    index=0,  # renumbered below
                    href=href,
                    title=toc.get(href) or _first_heading(body) or f"第 {len(book.chapters) + 1} 部分",
                    html=body,
                    text=_strip_tags(body),
                )
            )

    if not book.chapters:
        raise EpubCorrupt("EPUB 中没有可渲染的章节。")
    for index, chapter in enumerate(book.chapters):
        chapter.index = index
    return book


def _extract_body(html: str) -> str:
    """The <body> inner HTML, or the whole document when there is no body."""
    lowered = html.lower()
    start = lowered.find("<body")
    if start == -1:
        return html
    gt = html.find(">", start)
    if gt == -1:
        return html
    end = lowered.rfind("</body>")
    if end == -1 or end < gt:
        return html[gt + 1:]
    return html[gt + 1:end]


def _first_heading(body: str) -> str:
    lowered = body.lower()
    for tag in ("h1", "h2", "h3"):
        start = lowered.find(f"<{tag}")
        if start == -1:
            continue
        gt = body.find(">", start)
        if gt == -1:
            continue
        end = lowered.find(f"</{tag}>", gt)
        text = body[gt + 1: end if end != -1 else len(body)]
        cleaned = _strip_tags(text).strip()
        if cleaned:
            return cleaned[:120]
    return ""


def _strip_tags(fragment: str) -> str:
    import re

    text = re.sub(r"<[^>]+>", " ", fragment)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return " ".join(text.split())


__all__ = ["EpubReadError", "RenderBook", "RenderChapter", "open_epub"]