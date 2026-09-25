from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: UUID
    email: EmailStr
    created_at: datetime

    model_config = {"from_attributes": True}


class SourceOut(BaseModel):
    id: UUID
    filename: str
    content_type: str
    kind: str
    status: str
    title: str | None
    byte_size: int
    created_at: datetime
    task_id: UUID | None = None
    error_message: str | None = None
    #: printed_page - physical page_number, resolved from the running heads.
    page_offset: int | None = None
    page_count: int | None = None
    #: Latest ingest task message: OCR / STT / mock provenance, LLM used, etc.
    extraction_note: str | None = None
    task_status: str | None = None
    task_progress: int | None = None
    task_step: str | None = None
    task_message: str | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _recordings_are_titled_by_their_file_name(self) -> SourceOut:
        """A recording is always shown under the name the user gave the file.

        The pipeline writes the file name at upload, but the regenerate pass used
        to overwrite it with the model's headline — which describes the content
        and belongs on the summary. Enforcing the rule here rather than only at
        write time means rows corrupted by the old behaviour heal on read, and no
        call site has to remember the special case.
        """
        if self.kind == "video" and self.filename:
            self.title = self.filename
        return self


class OutlineEntry(BaseModel):
    """One node of the detected chapter/section tree.

    A book node carries pages; a recording node carries seconds. Both are
    optional so the frontend can render whichever the source actually has and
    use it as a jump target.
    """

    level: int
    number: str = ""
    title: str = ""
    page_number: int = 0
    printed_page: int | None = None
    page_end: int | None = None
    #: Recording only: the position the player should seek to.
    start_time: float | None = None
    end_time: float | None = None


class StructureChunkOut(BaseModel):
    """A chunk with its structural labels, for the structure inspector."""

    id: UUID
    ordinal: int
    content_type: str
    section_title: str | None = None
    page_number: int | None = None
    printed_page: int | None = None
    locator: str | None = None
    heading_level: int | None = None
    start_time: float | None = None
    end_time: float | None = None
    preview: str = ""

    model_config = {"from_attributes": True}


class SourceStructureOut(BaseModel):
    """What the parser understood about the document.

    Exposed so the learner can verify the reading rather than trust it: if the
    section tree or the page offset is wrong, it is visible immediately instead
    of showing up as a strange answer three questions later.
    """

    source_id: UUID
    kind: str = "pdf"
    #: Recording only: total length in seconds, from ffprobe.
    duration: float | None = None
    page_offset: int | None = None
    page_count: int | None = None
    outline: list[OutlineEntry] = Field(default_factory=list)
    content_types: dict[str, int] = Field(default_factory=dict)
    chunks: list[StructureChunkOut] = Field(default_factory=list)
    chunk_total: int = 0
    #: section title -> where its content sits. A page index for a book, a
    #: timestamp in seconds for a recording.
    section_index: dict[str, list[float]] = Field(default_factory=dict)


class TaskOut(BaseModel):
    id: UUID
    source_id: UUID
    kind: str
    status: str
    progress: int
    step: str
    message: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class Citation(BaseModel):
    chunk_id: UUID
    source_id: UUID
    locator: str | None = None
    #: Physical page in the PDF.
    page_number: int | None = None
    #: Page as printed on the paper; what the learner sees and types.
    printed_page: int | None = None
    #: Chapter/section path this chunk sits in.
    section_title: str | None = None
    #: body | heading | caption | formula | table | figure | reference | outline
    content_type: str = "body"
    start_time: float | None = None
    end_time: float | None = None
    quote: str
    score: float | None = None


class KnowledgePointOut(BaseModel):
    id: UUID
    title: str
    summary: str
    key_terms: list[str]
    chunk_ids: list[UUID]

    model_config = {"from_attributes": True}


class SummaryOut(BaseModel):
    source_id: UUID
    title: str
    overview: str
    outline: list[str]
    prompt_version: str


class QuizQuestionPublic(BaseModel):
    id: UUID
    ordinal: int
    question: str
    options: list[str]
    question_type: str = "choice"
    section_title: str = ""
    instructions: str = ""
    #: Honest scoring label. Speaking items are graded from typed text + rubric,
    #: not from a speech-to-text vendor (this app does not add one).
    scoring_note: str = ""


class QuizOut(BaseModel):
    id: UUID
    source_id: UUID
    title: str
    prompt_version: str = "quiz_generate.v2"
    created_at: datetime | None = None
    sections: list[str] = Field(default_factory=list)
    questions: list[QuizQuestionPublic]


class QuizSummaryOut(BaseModel):
    """One generated quiz version. Regenerating creates a new row; history stays."""

    id: UUID
    source_id: UUID
    title: str
    prompt_version: str = "quiz_generate.v2"
    created_at: datetime | None = None
    question_count: int = 0
    sections: list[str] = Field(default_factory=list)


class QuizListOut(BaseModel):
    source_id: UUID
    quizzes: list[QuizSummaryOut] = Field(default_factory=list)


class QuizAnswer(BaseModel):
    """One submitted answer. Choice questions use selected_index; open-ended
    question types (translation / writing / speaking) use text_answer."""

    question_id: UUID
    selected_index: int | None = None
    text_answer: str | None = None


class QuizAttemptRequest(BaseModel):
    answers: list[QuizAnswer]
    #: When set, only these questions are graded (wrong-question retry).
    #: Score / pass are computed over this subset. Omitted = grade the whole quiz.
    question_ids: list[UUID] | None = None


class QuizQuestionResult(BaseModel):
    question_id: UUID
    ordinal: int = 0
    section_title: str = ""
    question_type: str = "choice"
    question: str = ""
    selected_index: int | None = None
    text_answer: str = ""
    correct_index: int | None = None
    correct: bool = False
    verdict: str = ""
    score: float | None = None
    reference_answer: str = ""
    ai_explanation: str = ""
    # Backwards-compatible alias kept for the original API contract / older
    # clients & tests. Always mirrors ``ai_explanation``.
    explanation: str = ""
    chunk_ids: list[UUID] = Field(default_factory=list)
    scoring_note: str = ""


class QuizSectionResult(BaseModel):
    section_title: str
    total: int
    correct: int
    score: float


class QuizAttemptOut(BaseModel):
    id: UUID
    quiz_id: UUID
    score: float
    passed: bool
    submitted_at: datetime
    graded_count: int = 0
    results: list[QuizQuestionResult]
    sections: list[QuizSectionResult] = Field(default_factory=list)


class QuizRecordSummary(BaseModel):
    id: UUID
    quiz_id: UUID
    quiz_title: str
    source_id: UUID
    score: float
    passed: bool
    submitted_at: datetime
    graded_count: int = 0
    sections: list[QuizSectionResult] = Field(default_factory=list)


class QuizRecordsOut(BaseModel):
    source_id: UUID
    records: list[QuizRecordSummary]


class QuizDeleteOut(BaseModel):
    """Report for removing one generated quiz version.

    With `with_attempts` the version, its questions and its records are gone for
    good. Without it the version is tombstoned instead, so `deleted_questions` is
    cleared from the bank while the submission archive keeps working. Either way
    the other generated versions and the source itself are untouched.
    """

    quiz_id: UUID
    source_id: UUID
    deleted_questions: int = 0
    deleted_attempts: int = 0
    with_attempts: bool = False


# --------------------------------------------------------------------------
# Notes
# --------------------------------------------------------------------------


class NoteOut(BaseModel):
    id: UUID
    source_id: UUID
    title: str
    content: str
    origin: str = "manual"
    anchor: str | None = None
    ordinal: int = 0
    created_at: datetime
    updated_at: datetime | None = None


class NoteCreate(BaseModel):
    title: str = Field(default="", max_length=255)
    content: str = ""
    anchor: str | None = None
    origin: str = "manual"


class NoteUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    content: str | None = None
    anchor: str | None = None


# --------------------------------------------------------------------------
# Export / import
# --------------------------------------------------------------------------


class SourceBundle(BaseModel):
    """Portable snapshot of everything the learner produced for one source."""

    format: str = "handbook-ai-tutor/bundle@1"
    exported_at: datetime
    source: dict
    summary: dict | None = None
    knowledge: list[dict] = Field(default_factory=list)
    notes: list[dict] = Field(default_factory=list)
    quizzes: list[dict] = Field(default_factory=list)
    records: list[dict] = Field(default_factory=list)
    tutor: list[dict] = Field(default_factory=list)


class ImportRequest(BaseModel):
    bundle: SourceBundle
    mode: str = Field(
        default="merge",
        description="merge = add missing notes/quizzes, replace = delete existing derived data first",
    )


class ImportResult(BaseModel):
    notes_created: int = 0
    notes_skipped: int = 0
    quizzes_created: int = 0
    records_created: int = 0
    summary_restored: bool = False
    knowledge_restored: int = 0
    tutor_created: int = 0
    tutor_skipped: int = 0
    mode: str = "merge"


class RegenerateOut(BaseModel):
    source_id: UUID
    provider: str
    model: str
    title: str
    knowledge_points: int
    task: str = "regenerate"


class SourceDeleteOut(BaseModel):
    source_id: UUID
    filename: str
    storage_key: str
    storage_deleted: bool
    storage_error: str | None = None
    rows_deleted: dict[str, int] = Field(default_factory=dict)
    total_rows_deleted: int = 0


class TutorChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class TutorMessageOut(BaseModel):
    id: UUID
    role: str
    content: str
    citations: list[Citation]
    created_at: datetime


class RetrieveRequest(BaseModel):
    query: str
    source_id: UUID | None = None
    top_k: int = 5


class RetrieveResponse(BaseModel):
    citations: list[Citation]


# --------------------------------------------------------------------------
# Token usage (inspectable totals; tokens only, no invented dollar prices)
# --------------------------------------------------------------------------


class UsageTaskRow(BaseModel):
    task: str
    provider: str
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class UsageSourceRow(BaseModel):
    source_id: UUID | None = None
    source_title: str | None = None
    source_filename: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    by_task: list[UsageTaskRow] = Field(default_factory=list)


class UsageOut(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    by_source: list[UsageSourceRow] = Field(default_factory=list)
    by_task: list[UsageTaskRow] = Field(default_factory=list)
