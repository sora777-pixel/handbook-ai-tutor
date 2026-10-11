"""EPUB reading experience: cover, navigation, and citation fidelity.

Four things the reading flow depends on, pinned here:

* the cover is found through all three conventions vendors use, and its absence
  is reported as absence rather than as "the first illustration is the cover";
* asking for the book's main content routes to a *structural* answer (TOC +
  opening pages), not to whichever passage embedded closest;
* citations keep the order the model numbered them, so 【2】clicking opens the
  second source rather than the second *retrieved* one;
* every chunk knows which section it belongs to, so a jump lands in the right
  place even when the layout engine attributed no heading to that paragraph.
"""

from __future__ import annotations

import io
import zipfile
from uuid import UUID, uuid4

import pytest

from app.domain.schemas import Citation
from app.services.chunking import chunk_document
from app.services.epub_reader import open_epub
from app.services.layout import BODY, HEADING, Block, DocumentLayout, PageAnalysis, finalise
from app.services.rag.intent import parse_intent
from app.services.tutor import order_citations

# --------------------------------------------------------------------- cover


def _epub(*, cover_style: str) -> bytes:
    container = (
        '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
        ' version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    if cover_style == "epub3":
        manifest = (
            '<item id="cover" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
        )
        metadata = ""
    elif cover_style == "epub2":
        manifest = '<item id="cov" href="images/cover.jpg" media-type="image/jpeg"/>'
        metadata = '<meta name="cover" content="cov"/>'
    elif cover_style == "by-name":
        manifest = '<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg"/>'
        metadata = ""
    else:
        manifest = '<item id="fig" href="images/fig1.jpg" media-type="image/jpeg"/>'
        metadata = ""

    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="u">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>封面测试</dc:title>{metadata}</metadata>'
        f'<manifest>{manifest}'
        '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
        "<head><title>c1</title></head><body><h1>第一章</h1><p>"
        + "正文段落。" * 40
        + "</p></body></html>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml", chapter)
        z.writestr("OEBPS/images/cover.jpg", b"\xff\xd8\xff" + b"\x00" * 500)
        z.writestr("OEBPS/images/fig1.jpg", b"\xff\xd8\xff" + b"\x00" * 500)
    return buf.getvalue()


@pytest.mark.parametrize("style", ["epub3", "epub2", "by-name"])
def test_cover_is_found_by_every_convention(style):
    book = open_epub(_epub(cover_style=style))
    assert book.cover_href == "OEBPS/images/cover.jpg"


def test_no_cover_declared_means_no_cover():
    """A first illustration must not be promoted to "the cover"."""
    book = open_epub(_epub(cover_style="none"))
    assert book.cover_href == ""


# --------------------------------------------------------------------- intent


@pytest.mark.parametrize(
    "question",
    [
        "这本书主要讲了什么？",
        "主要内容是什么",
        "简单概括一下全书",
        "内容简介",
        "这本书的主旨",
        "介绍一下各章结构",
    ],
)
def test_main_content_questions_are_structural(question):
    intent = parse_intent(question)
    assert intent.wants_outline is True


def test_a_specific_page_question_is_not_structural():
    assert parse_intent("第 42 页讲了什么").wants_outline is False


# ----------------------------------------------------------------- citations


#: Readable labels for the test citations; each maps to one fixed UUID.
KEYS = {key: UUID(int=index + 1) for index, key in enumerate("abcde")}


def _citation(chunk_key: str, page: int) -> Citation:
    return Citation(
        chunk_id=KEYS[chunk_key],
        source_id=uuid4(),
        locator=f"p.{page}",
        page_number=page,
        quote=f"passage on page {page}",
    )


def test_citations_follow_the_models_order_not_the_retrieval_order():
    retrieved = [_citation("a", 10), _citation("b", 20), _citation("c", 30)]
    # the model cited the third hit first
    used = order_citations(retrieved, [str(KEYS["c"]), str(KEYS["a"])])
    assert [c.chunk_id for c in used] == [KEYS["c"], KEYS["a"]]
    assert [c.page_number for c in used] == [30, 10]


def test_citations_drop_invented_ids_and_duplicates():
    retrieved = [_citation("a", 10)]
    invented = "11111111-2222-3333-4444-555555555555"
    used = order_citations(retrieved, [invented, str(KEYS["a"]), str(KEYS["a"])])
    assert [c.chunk_id for c in used] == [KEYS["a"]]


# ---------------------------------------------------------------- chunk sections


def _page(number: int, blocks: list[Block]) -> PageAnalysis:
    return PageAnalysis(page_number=number, blocks=blocks, source="pdf")


def test_chunks_inherit_the_section_of_the_heading_above_them():
    """A paragraph the layout engine left unattributed still belongs somewhere.

    Without inheritance its chunk had an empty section, so "跳到对应章节" had no
    target and the section list looked shuffled between pages.
    """
    heading = Block(content="3.2 分支预测", content_type=HEADING, page_number=12,
                    section_path="第3章 指令级并行 > 3.2 分支预测", heading_level=2)
    attributed = Block(content="流水线冲突" * 20, content_type=BODY, page_number=12,
                       section_path="第3章 指令级并行 > 3.2 分支预测")
    orphan = Block(content="继续讨论分支预测的历史" * 20, content_type=BODY, page_number=13)
    doc = finalise([_page(12, [heading, attributed]), _page(13, [orphan])])

    chunks = chunk_document(doc)
    body_chunks = [c for c in chunks if c.content_type == BODY]
    assert body_chunks, "the body paragraphs must survive"
    assert all(c.section_title for c in body_chunks), [c.section_title for c in body_chunks]
    # the page-13 paragraph carries the section it is actually inside
    late = [c for c in body_chunks if c.page_number == 13]
    assert late and all("3.2 分支预测" in c.section_title for c in late)
    # and its locator points at the page it lives on, for citation jumps
    assert all("13" in (c.locator or "") for c in late)