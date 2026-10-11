"""Serve an imported EPUB to the browser reader.

The ingest pipeline already proves an EPUB is a readable ZIP of XHTML; this
module re-opens the stored original and hands the *renderable* pieces to the
frontend:

    GET /api/v1/sources/{id}/epub            -> chapter list (spine order + TOC titles)
    GET /api/v1/sources/{id}/epub/chapter?i=0 -> one chapter, sanitised XHTML
    GET /api/v1/sources/{id}/epub/asset?path=images/fig1-1.png -> one image

Why re-parse instead of reusing stored chunks: chunks are for retrieval (plain
text, no layout, no images) and lose exactly what a reader needs -- inline
figures, styled tables, links between chapters.

Security: the chapter XHTML comes from an untrusted upload, so it is scrubbed
before it can execute in the app's origin. `<script>`, `<iframe>`, event
handlers, `javascript:` URLs and external references are removed, and the
response is served as text/html with a locked-down CSP. Reading an uploaded
book must never be able to run code or phone home.
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from html import escape
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse

from app.core.db import get_db
from app.core.deps import get_current_user, get_current_user_allow_query_token
from app.models.source import Source
from app.models.user import User
from app.services.epub_reader import EpubReadError, open_epub
from app.services.workspace import read_source_bytes

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])

#: Content-Security-Policy for rendered chapters. `img-src` stays 'self' so a
#: book can show its own illustrations through /epub/asset and nothing else --
#: no third-party pixels, no data: smuggling.
CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline' 'self'; "
    "font-src 'self'; media-src 'self'; script-src 'none'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'self'"
)

_SCRIPTISH = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|form|noscript)\b.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SELF_CLOSING_BAD = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|form|base)\b[^>]*/?>",
    re.IGNORECASE,
)
_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src|xlink:href)\s*=\s*([\"']?)\s*(javascript|vbscript|data:text/html)[^\"'>]*\2", re.IGNORECASE)
_STYLE_BLOCK = re.compile(r"<\s*style\b[^>]*>.*?<\s*/\s*style\s*>", re.IGNORECASE | re.DOTALL)

#: Only these raster types are proxied; SVG is excluded because it can carry
#: script and does not need the same treatment as a photo.
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
_ASSET_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


@dataclass
class Chapter:
    index: int
    href: str
    title: str


async def load_source(db: AsyncSession, source_id, user_id) -> Source:
    """Ownership-checked source lookup (404 for unknown, 403 for someone else's)."""
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="资料不存在")
    if str(source.user_id) != str(user_id):
        raise HTTPException(status_code=403, detail="这不是你的资料")
    return source


def _rewrite(
    html: str,
    chapter_href: str,
    source_id,
    chapter_index: dict[str, int] | None = None,
    *,
    link_mode: str = "page",
) -> str:
    """Point the chapter's own links/assets at this API and strip anything active.

    `href` and `src` are handled differently on purpose. An image must come
    from the archive (`src` -> /epub/asset), while a link to another chapter is
    navigation and has to land on the reader (`href` -> /epub/chapter?i=N) --
    resolving it relatively would request /epub/ch2.xhtml and 404, since the
    chapter document's own URL is /epub/chapter.
    """
    base = posixpath.dirname(chapter_href)
    index_map = {posixpath.normpath(k): v for k, v in (chapter_index or {}).items()}

    def resolve(raw: str) -> str:
        path = raw.split("#")[0]
        if not path:
            return ""
        return posixpath.normpath(posixpath.join(base, urllib.parse.unquote(path)))

    def fix_url(match: re.Match) -> str:
        attr = match.group(1).lower()
        quote = match.group(2) or '"'
        url = (match.group(3) or "").strip()
        if not url or url.startswith(("#", "data:", "mailto:")):
            return match.group(0)
        if url.lower().startswith(("http://", "https://")):
            # A remote reference is a tracking pixel / beacon: do not keep it.
            return f'{attr}=""'
        target = resolve(url)
        fragment = url[len(url.split("#")[0]):] if "#" in url else ""
        if attr == "href":
            index = index_map.get(target)
            if index is None:
                return f'{attr}="#"'          # dead but harmless
            if link_mode == "anchor":
                # Inline reader: a fragment the frontend intercepts, so an
                # in-book link switches chapters without navigating away.
                return f'{attr}="#epub-chapter-{index}"'
            return f'{attr}="/api/v1/sources/{source_id}/epub/chapter?i={index}{fragment}"'
        return (
            f'{attr}="/api/v1/sources/{source_id}/epub/asset?'
            f"path={urllib.parse.quote(target)}{fragment}\""
        )

    html = re.sub(
        r"""(href|src|xlink:href)\s*=\s*(["']?)([^"'\s>]+)\2""",
        fix_url,
        html,
        flags=re.IGNORECASE,
    )
    html = _SCRIPTISH.sub("", html)
    html = _SELF_CLOSING_BAD.sub("", html)
    html = _STYLE_BLOCK.sub("", html)  # remote @import / url() in CSS
    html = _EVENT_ATTR.sub("", html)
    html = _JS_URL.sub(r'\1="about:blank"', html)
    return html


def _reader_shell(body: str, chapter: Chapter, total: int, source_id, font_scale: float = 1.0) -> str:
    """Wrap the sanitised chapter so the reader gets sane typography for free."""
    scale = max(0.7, min(1.8, font_scale or 1.0))
    prev_link = (
        f'<a class="nav prev" href="?i={chapter.index - 1}">← 上一章</a>'
        if chapter.index > 0
        else '<span class="nav disabled">← 上一章</span>'
    )
    next_link = (
        f'<a class="nav next" href="?i={chapter.index + 1}">下一章 →</a>'
        if chapter.index < total - 1
        else '<span class="nav disabled">下一章 →</span>'
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(chapter.title or "正文")}</title>
<style>
  html, body {{ margin:0; padding:0; background:#fffdf8; color:#1f2328; }}
  body {{ font-family: Georgia, "Songti SC", "Noto Serif CJK SC", serif;
         font-size: 17px; line-height: 1.85; padding: 28px 30px 96px; }}
  img {{ max-width: 100%; height: auto; display:block; margin: 1em auto; }}
  figure {{ margin: 1.4em 0; }}
  figcaption {{ font-size: 13px; color:#6b7280; text-align:center; }}
  table {{ border-collapse: collapse; width:100%; margin:1.2em 0; font-size:15px;
           display:block; overflow-x:auto; }}
  td, th {{ border:1px solid #d8d2c4; padding:6px 9px; }}
  th {{ background:#f4efe3; }}
  h1, h2, h3, h4 {{ line-height:1.35; margin:1.4em 0 .6em; }}
  h1, h2 {{ text-align:center; }}
  p {{ margin: .85em 0; text-align: justify; }}
  blockquote {{ margin:1em 1.4em; color:#555; border-left:3px solid #d8d2c4; padding-left:12px; }}
  a {{ color:#2f6f4f; }}
  .nav {{ position:fixed; bottom:0; width:50%; padding:12px 0; text-align:center;
          font: 13px/1.2 system-ui, sans-serif; text-decoration:none;
          background:rgba(255,253,248,.94); border-top:1px solid #e8e2d4; color:#2f6f4f; }}
  .nav.prev {{ left:0; border-right:1px solid #e8e2d4; }}
  .nav.next {{ right:0; }}
  .nav.disabled {{ color:#b9b3a5; }}
  .chrome {{ position:sticky; top:0; background:#fffdf8; padding:6px 0 10px;
             font:12px/1.4 system-ui, sans-serif; color:#8a8577; border-bottom:1px solid #eee7d8; }}
</style></head>
<body style="font-size:{17 * scale:.1f}px">
<div class="chrome">第 {chapter.index + 1} / {total} 章 · {escape(chapter.title or "未命名")}</div>
{body}
{prev_link}{next_link}
</body></html>"""


def resolve_asset_path(raw: str) -> str:
    """Validate an asset request and return the archive-relative path.

    Two independent guards, because either one alone is bypassable: the
    normalised path may not escape the archive root, and it must carry a
    whitelisted raster extension (so `.xhtml`, `.opf` and friends can never be
    read out through the image proxy).
    """
    wanted = posixpath.normpath(urllib.parse.unquote(raw or "")).lstrip("/")
    if not wanted or wanted.startswith("..") or wanted.startswith("/") or ".." in wanted:
        raise HTTPException(status_code=400, detail="非法资源路径")
    suffix = posixpath.splitext(wanted)[1].lower()
    if suffix not in _ASSET_SUFFIXES:
        raise HTTPException(status_code=415, detail=f"不支持的资源类型：{suffix or '未知'}")
    return wanted


async def _epub_bytes(source: Source) -> bytes:
    """Bytes of the stored book, tolerating the pre-rename storage key."""
    try:
        data = await read_source_bytes(source, source.user_id)
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="原始文件已不在存储中") from None
    if not data:
        raise HTTPException(status_code=410, detail="原始文件已不在存储中")
    return data


@router.get("/{source_id}/epub")
async def epub_contents(
    source_id,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Chapter list in spine order, with the book's own TOC titles when it has them."""
    source = await load_source(db, source_id, user.id)
    if source.kind != "epub":
        raise HTTPException(status_code=400, detail="该资料不是 EPUB")
    data = await _epub_bytes(source)
    try:
        book = open_epub(data)
    except EpubReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "title": book.title,
        "authors": book.authors,
        "cover": bool(book.cover_href),
        "chapters": [
            {"index": c.index, "title": c.title, "chars": len(c.text)}
            for c in book.chapters
        ],
    }


@router.get("/{source_id}/epub/chapter", response_model=None)
async def epub_chapter(
    source_id,
    i: int = Query(default=0, ge=0, description="chapter index in spine order"),
    fs: float = Query(default=1.0, ge=0.7, le=1.8, description="font scale"),
    format: str = Query(default="html", pattern="^(html|json)$"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_allow_query_token),
):
    """One chapter as a standalone, sanitised HTML document."""
    source = await load_source(db, source_id, user.id)
    if source.kind != "epub":
        raise HTTPException(status_code=400, detail="该资料不是 EPUB")
    data = await _epub_bytes(source)
    try:
        book = open_epub(data)
    except EpubReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not book.chapters:
        raise HTTPException(status_code=422, detail="EPUB 里没有可渲染的章节")
    index = min(i, len(book.chapters) - 1)
    chapter = book.chapters[index]
    chapter_index = {c.href: c.index for c in book.chapters}
    if format == "json":
        # What the in-app reader consumes: a sanitised fragment, not a document,
        # so it can be rendered inline (no iframe, so nothing to "refuse to
        # connect" and no download dialog in the way).
        fragment = _rewrite(
            chapter.html, chapter.href, source_id, chapter_index, link_mode="anchor"
        )
        return {
            "index": index,
            "total": len(book.chapters),
            "title": chapter.title,
            "html": fragment,
            "next": index + 1 if index + 1 < len(book.chapters) else None,
        }
    html = _rewrite(chapter.html, chapter.href, source_id, chapter_index)
    return HTMLResponse(
        content=_reader_shell(html, chapter, len(book.chapters), source_id, font_scale=fs),
        headers={
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=0, no-store",
        },
    )


@router.get("/{source_id}/epub/cover")
async def epub_cover(
    source_id,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user_allow_query_token),
):
    """The book's cover image, or 404 when it has none.

    Separate from /epub/asset because the cover is what the reader shows before
    any chapter exists: it must be reachable without knowing an archive path.
    """
    source = await load_source(db, source_id, user.id)
    if source.kind != "epub":
        raise HTTPException(status_code=400, detail="该资料不是 EPUB")
    data = await _epub_bytes(source)
    try:
        book = open_epub(data)
    except EpubReadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not book.cover_href:
        raise HTTPException(status_code=404, detail="这本书没有封面图")
    wanted = resolve_asset_path(book.cover_href)
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            raw = zf.read(wanted)
    except KeyError:
        raise HTTPException(status_code=404, detail="封面图不在文件里") from None
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="EPUB 文件已损坏") from None
    return Response(
        content=raw,
        media_type=_ASSET_TYPES.get(posixpath.splitext(wanted)[1].lower(), "image/jpeg"),
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/{source_id}/epub/asset")
async def epub_asset(
    source_id,
    path: str = Query(description="path inside the EPUB archive"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Proxy one image out of the archive.

    Path traversal is refused: the resolved name must stay inside the ZIP and
    carry a whitelisted raster extension.
    """
    source = await load_source(db, source_id, user.id)
    if source.kind != "epub":
        raise HTTPException(status_code=400, detail="该资料不是 EPUB")
    wanted = resolve_asset_path(path)

    data = await _epub_bytes(source)
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            raw = zf.read(wanted)
    except KeyError:
        raise HTTPException(status_code=404, detail="书中没有这个图片") from None
    except zipfile.BadZipFile:
        raise HTTPException(status_code=422, detail="EPUB 文件已损坏") from None
    return Response(
        content=raw,
        media_type=_ASSET_TYPES.get(posixpath.splitext(wanted)[1].lower(), "application/octet-stream"),
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


__all__ = ["router"]