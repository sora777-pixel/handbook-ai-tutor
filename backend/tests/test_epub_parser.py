"""EPUB parsing: structure mapping, guard rails, and pipeline glue.

The books are built in-memory with ``zipfile`` so the tests pin the *rules*
(spine order, heading levels, table flattening, empty-chapter filtering, DRM /
fixed-layout refusal) without depending on any particular e-book.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from app.services.chunking import OUTLINE, chunk_document
from app.services.epub_parser import (
    EpubCorrupt,
    EpubUnsupported,
    parse_epub,
    relabel_chapter_locators,
)
from app.services.layout import (
    BODY,
    CAPTION,
    HEADING,
    TABLE,
    PageAnalysis,
    finalise,
)

FAKE_IMAGE = b"\x89PNG-raster-data" + b"\x00" * 5000  # > MIN_IMAGE_BYTES

CHAPTER_1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>ch1</title></head>
<body>
  <h1>第1章 计算机系统概述</h1>
  <p>计算机系统由硬件和软件两大部分组成。硬件是躯干，软件是灵魂。</p>
  <p>本章介绍计算机的基本组成与工作原理。</p>
  <h2>1.1 计算机的组成</h2>
  <p>运算器、控制器、存储器、输入设备和输出设备合称五大部件。</p>
  <table>
    <tr><th>部件</th><th>作用</th></tr>
    <tr><td>运算器</td><td>算术与逻辑运算</td></tr>
  </table>
  <figure>
    <img src="images/fig1-1.png" alt="系统结构图"/>
    <figcaption>图 1-1 计算机系统层次结构</figcaption>
  </figure>
</body>
</html>
"""

CHAPTER_2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>ch2</title></head>
<body>
  <h1>第2章 数据的表示与运算</h1>
  <p>数据在计算机中以二进制形式表示，由数值数据和非数值数据两大类组成。</p>
  <p>本章首先讨论进位计数制，然后讨论定点数与浮点数的表示和运算方法。</p>
  <blockquote>一切皆比特。</blockquote>
  <ul>
    <li>原码：最高位为符号位</li>
    <li>补码：减法转化为加法</li>
  </ul>
</body>
</html>
"""

#: An image-only spread (empty shell) -- must be skipped, not embedded.
CHAPTER_EMPTY = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body><p>见图</p></body>
</html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>计算机组成原理</dc:title>
    <dc:creator>唐朔飞</dc:creator>
    <dc:language>zh</dc:language>
  </metadata>
  <manifest>
    <item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="c3" href="text/empty.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="img1" href="text/images/fig1-1.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="c1"/>
    <itemref idref="c2"/>
    <itemref idref="c3"/>
  </spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body>
  <nav epub:type="toc"><ol>
    <li><a href="text/ch1.xhtml">第1章 计算机系统概述</a></li>
    <li><a href="text/ch2.xhtml">第2章 数据的表示与运算</a></li>
  </ol></nav>
</body>
</html>
"""

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def build_epub(files: dict[str, str | bytes]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER)
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def sample_book() -> bytes:
    return build_epub(
        {
            "OEBPS/content.opf": OPF,
            "OEBPS/nav.xhtml": NAV,
            "OEBPS/text/ch1.xhtml": CHAPTER_1,
            "OEBPS/text/ch2.xhtml": CHAPTER_2,
            "OEBPS/text/empty.xhtml": CHAPTER_EMPTY,
            "OEBPS/text/images/fig1-1.png": FAKE_IMAGE,
        }
    )


# ----------------------------------------------------------------- parsing


def test_parse_basic_book():
    book = parse_epub(sample_book())

    assert book.title == "计算机组成原理"
    assert book.authors == ["唐朔飞"]
    # the empty shell chapter is skipped, kept chapters are renumbered 1..2
    assert len(book.chapters) == 2
    assert book.skipped_chapters == 1
    assert [ch.number for ch in book.chapters] == [1, 2]

    ch1 = book.chapters[0]
    assert ch1.title == "第1章 计算机系统概述"  # from the nav document
    kinds = [b.content_type for b in ch1.blocks]
    assert kinds.count(HEADING) == 2  # <h1> and <h2>
    assert TABLE in kinds
    assert CAPTION in kinds

    headings = [b for b in ch1.blocks if b.content_type == HEADING]
    assert headings[0].heading_level == 1
    assert headings[0].content == "第1章 计算机系统概述"
    assert headings[1].heading_level == 2

    table = next(b for b in ch1.blocks if b.content_type == TABLE)
    assert "部件 | 作用" in table.content
    assert "运算器 | 算术与逻辑运算" in table.content

    caption = next(b for b in ch1.blocks if b.content_type == CAPTION)
    assert caption.content.startswith("图 1-1")

    # the <img> was resolved through the manifest into raw bytes
    assert ch1.images == [FAKE_IMAGE]

    # every block addresses its chapter
    assert all(b.page_number == 1 for b in ch1.blocks)


def test_list_items_become_paragraphs():
    book = parse_epub(sample_book())
    ch2 = book.chapters[1]
    texts = [b.content for b in ch2.blocks if b.content_type == BODY]
    assert "原码：最高位为符号位" in texts
    assert "一切皆比特。" in texts  # blockquote text is kept


def test_spine_order_is_authoritative():
    # Same chapters, reversed spine: reading order must follow the spine.
    reversed_spine = OPF.replace(
        '<itemref idref="c1"/>\n    <itemref idref="c2"/>',
        '<itemref idref="c2"/>\n    <itemref idref="c1"/>',
    )
    book = parse_epub(
        build_epub(
            {
                "OEBPS/content.opf": reversed_spine,
                "OEBPS/nav.xhtml": NAV,
                "OEBPS/text/ch1.xhtml": CHAPTER_1,
                "OEBPS/text/ch2.xhtml": CHAPTER_2,
                "OEBPS/text/empty.xhtml": CHAPTER_EMPTY,
                "OEBPS/text/images/fig1-1.png": FAKE_IMAGE,
            }
        )
    )
    first_heading = next(
        b for b in book.chapters[0].blocks if b.content_type == HEADING
    )
    assert first_heading.content == "第2章 数据的表示与运算"


# ------------------------------------------------------------- guard rails


def test_drm_rejected():
    data = build_epub(
        {
            "META-INF/encryption.xml": "<encryption/>",
            "OEBPS/content.opf": OPF,
        }
    )
    with pytest.raises(EpubUnsupported, match="DRM"):
        parse_epub(data)


def test_fixed_layout_rejected():
    fixed_opf = OPF.replace(
        "</metadata>",
        '<meta property="rendition:layout">fixed</meta></metadata>',
    )
    data = build_epub(
        {
            "OEBPS/content.opf": fixed_opf,
            "OEBPS/text/ch1.xhtml": CHAPTER_1,
        }
    )
    with pytest.raises(EpubUnsupported, match="fixed-layout"):
        parse_epub(data)


def test_corrupt_container_rejected():
    with pytest.raises(EpubCorrupt):
        parse_epub(b"this is not a zip at all")
    with pytest.raises(EpubCorrupt):
        # a valid zip that is not an EPUB (no container.xml)
        parse_epub(build_epub({"OEBPS/content.opf": OPF}))


def test_epub2_ncx_toc():
    opf2 = OPF.replace('properties="nav"', "").replace('version="3.0"', 'version="2.0"')
    opf2 = opf2.replace(
        '<item id="img1"',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n    <item id="img1"',
    ).replace(
        "<spine>",
        '<spine toc="ncx">',
    )
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <navMap>
    <navPoint id="n1" playOrder="1">
      <navLabel><text>第一章 系统概述</text></navLabel>
      <content src="text/ch1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>
"""
    book = parse_epub(
        build_epub(
            {
                "OEBPS/content.opf": opf2,
                "OEBPS/toc.ncx": ncx,
                "OEBPS/text/ch1.xhtml": CHAPTER_1,
                "OEBPS/text/ch2.xhtml": CHAPTER_2,
                "OEBPS/text/images/fig1-1.png": FAKE_IMAGE,
            }
        )
    )
    assert book.chapters[0].title == "第一章 系统概述"


# ----------------------------------------------------------- pipeline glue


def test_chapters_flow_through_chunk_pipeline():
    """parse -> finalise -> chunk_document: the shared machinery must accept
    EPUB blocks without any special casing."""
    book = parse_epub(sample_book())
    pages = [
        PageAnalysis(page_number=ch.number, blocks=ch.blocks, source="epub")
        for ch in book.chapters
    ]
    doc = finalise(pages)

    assert doc.char_count > 0
    assert any(node.title == "第1章 计算机系统概述" for node in doc.outline)

    chunks = chunk_document(doc)
    assert chunks, "chunker produced nothing"
    # the outline chunk rides along, so "这本书讲了什么" is retrievable
    assert any(c.content_type == OUTLINE for c in chunks)
    # section paths from real <h1>/<h2> tags survive into chunk metadata
    sections = {c.section_title for c in chunks if c.section_title}
    assert any("第1章" in s for s in sections)
    assert any("1.1" in s for s in sections)
    # body chunks kept their prose
    assert any("五大部件" in c.content for c in chunks)


def test_relabel_chapter_locators():
    book = parse_epub(sample_book())
    pages = [
        PageAnalysis(page_number=ch.number, blocks=ch.blocks, source="epub")
        for ch in book.chapters
    ]
    chunks = chunk_document(finalise(pages))

    body_before = [
        c.locator
        for c in chunks
        if c.page_number == 1 and c.locator.startswith("p.1")
    ]
    assert body_before, "no page-flavoured locators to relabel"

    # the document outline chunk ("全书大纲") is not page-flavoured and must
    # survive the relabelling untouched
    outline = [c for c in chunks if c.content_type == OUTLINE]
    assert all(c.locator.startswith("全书大纲") for c in outline)

    relabel_chapter_locators(chunks, book)

    for chunk in chunks:
        if chunk.page_number == 1 and not chunk.locator.startswith("全书大纲"):
            assert chunk.locator.startswith("ch.1"), chunk.locator
    assert any("第1章 计算机系统概述" in (c.locator or "") for c in chunks)
    # type suffixes ("表格" etc.) are preserved, not clobbered
    table_chunks = [c for c in chunks if c.content_type == TABLE]
    for chunk in table_chunks:
        assert chunk.locator.startswith(f"ch.{chunk.page_number}")
