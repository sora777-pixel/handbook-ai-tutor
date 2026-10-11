"""Turn a learner's question into structural retrieval constraints.

The four screenshots that prompted this work all came from the same gap: the
question contained an exact coordinate -- "第569页", "第三章", "这本书每章讲了
什么" -- and retrieval treated those words as bag-of-words weight instead of as
addresses. Page 569 was answered with page 269, and "every chapter" was answered
with five unrelated fragments.

Nothing here calls a model. A page number is a number; a chapter reference is a
number; "每章" is a fixed phrase. Parsing them locally is exact, instant, and
free, and it keeps the retriever honest when the LLM is a mock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.rag.base import RetrievalFilter

_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_PAGE_PATTERNS = (
    re.compile(r"第\s*([0-9]{1,4})\s*页"),
    re.compile(r"[pP]\s*[.．]?\s*([0-9]{1,4})\b"),
    re.compile(r"[pP]age\s*([0-9]{1,4})\b", re.I),
    re.compile(r"([0-9]{1,4})\s*页"),
)
_CHAPTER_PATTERNS = (
    re.compile(rf"第\s*([0-9]{{1,2}}|[一二三四五六七八九十]{{1,3}})\s*[章讲课]"),
    re.compile(r"(?:chapter|chap\.?|unit|lesson)\s*([0-9]{1,2})\b", re.I),
)
_SECTION_PATTERN = re.compile(r"(?<![\d.])([0-9]{1,2}(?:\.[0-9]{1,3}){1,3})(?![\d.])")

_OUTLINE_CUES = (
    "每章",
    "各章",
    "全书",
    "整本书",
    "整体结构",
    "目录",
    "大纲",
    "章节结构",
    "全书结构",
    "what is this book about",
    "overview of the book",
    "table of contents",
)
#: Phrases that ask for the whole document -- but only when the question did not
#: name a page or a chapter. "第 42 页讲了什么" is a page question, and treating
#: it as "summarise the book" answered something the learner never asked.
_WHOLE_BOOK_CUES = (
    "总结一下",
    "概括",
    "综述",
    "整体介绍",
    "内容概括",
    "简介",
    "讲了哪些",
    "讲了什么",
    "讲什么",
    "全书写了什么",
    "主要内容",
    "核心内容",
    "主要讲",
    "内容简介",
    "主旨",
    "这本书是",
)

#: Kept as an alias: older code (and prompts) refer to it by this name.
_SUMMARY_CUES = _WHOLE_BOOK_CUES

_FIGURE_CUES = (
    "插图",
    "示意图",
    "结构图",
    "流程图",
    "这张图",
    "这个图",
    "图里",
    "figure",
    "diagram",
)
_FIGURE_REF = re.compile(r"图\s*[0-9]{1,3}(?:[-–—][0-9]{1,3})?|见\s*图|看\s*图")
_TABLE_CUES = ("表格", "这张表", "这个表", "此表", "该表", "table")
_FORMULA_CUES = ("公式", "方程", "算式", "推导", "formula", "equation")

#: When a page or chapter is named, these types are furniture rather than
#: content: a figure label's word soup rarely answers the question, while the
#: prose beside it usually does.
_NOISE_WHEN_ADDRESSED = ("caption", "figure")


@dataclass
class QueryIntent:
    filter: RetrievalFilter = field(default_factory=RetrievalFilter)
    #: True when the learner is asking about the book's shape, not its content.
    wants_outline: bool = False
    #: Human-readable explanation, surfaced so the learner can see which page or
    #: chapter the system thought they meant.
    note: str = ""

    def describe(self) -> str:
        bits: list[str] = []
        if self.filter.pages:
            bits.append("页码 " + ", ".join(str(p) for p in self.filter.pages))
        if self.filter.section_terms:
            bits.append("章节 " + ", ".join(self.filter.section_terms))
        if self.filter.prefer_types:
            bits.append("偏好 " + ", ".join(self.filter.prefer_types))
        return " · ".join(bits)


def _cn_to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        units = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + units
    return _CN_DIGITS.get(token)


def parse_intent(message: str, *, page_hint: tuple[int, int] | None = None) -> QueryIntent:
    """Extract page / chapter / outline intent from one question.

    ``page_hint`` is the document's (min, max) printed page number when known,
    used to reject numbers that cannot be a page -- a year such as "2022" in
    "2022 年的版本" must not be read as page 2022.
    """
    text = (message or "").strip()
    intent = QueryIntent()
    if not text:
        return intent

    lowered = text.lower()

    pages: list[int] = []
    for pattern in _PAGE_PATTERNS:
        for match in pattern.finditer(text):
            value = int(match.group(1))
            if value <= 0:
                continue
            if page_hint and not (page_hint[0] <= value <= page_hint[1]):
                continue
            if not page_hint and value > 5000:
                continue
            if value not in pages:
                pages.append(value)

    section_terms: list[str] = []
    for pattern in _CHAPTER_PATTERNS:
        for match in pattern.finditer(text):
            value = _cn_to_int(match.group(1))
            if value:
                section_terms.append(f"第{value}章")
    for match in _SECTION_PATTERN.finditer(text):
        token = match.group(1)
        # "4.4" is a section reference; a bare number is handled as a page.
        if token not in section_terms:
            section_terms.append(token)

    prefer: list[str] = []
    if any(cue in text or cue in lowered for cue in _FORMULA_CUES):
        prefer.append("formula")
    if any(cue in text or cue in lowered for cue in _TABLE_CUES):
        prefer.append("table")
    wants_figure = any(cue in text or cue in lowered for cue in _FIGURE_CUES) or bool(
        _FIGURE_REF.search(text)
    )

    wants_outline = any(cue in text or cue in lowered for cue in _OUTLINE_CUES)
    if not wants_outline and any(cue in text or cue in lowered for cue in _WHOLE_BOOK_CUES):
        # "概括一下" / "主要内容" mean the whole document *only* when the question
        # named no page and no chapter -- and is short enough to be a request
        # rather than a paragraph that happens to contain the word.
        wants_outline = not pages and not section_terms and len(text) <= 24

    if wants_outline:
        prefer = [*prefer, "outline", "heading"]
    if wants_figure and not any(
        cue in text or cue in lowered for cue in ("不要图", "no figure")
    ):
        prefer = [*prefer, "figure"]

    avoid: list[str]
    if prefer:
        avoid = []
    else:
        # A prose question should not be answered out of a figure's label soup.
        avoid = list(_NOISE_WHEN_ADDRESSED)

    intent.filter = RetrievalFilter(
        pages=tuple(pages),
        section_terms=tuple(dict.fromkeys(section_terms)),
        prefer_types=tuple(dict.fromkeys(prefer)),
        avoid_types=tuple(avoid),
    )
    intent.wants_outline = wants_outline
    intent.note = intent.describe()
    return intent


__all__ = ["QueryIntent", "parse_intent"]
