from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import GUID

# choice      -> multiple choice, graded automatically
# translation -> learner types a translation, LLM grades against reference_answer
# writing     -> short paragraph writing, LLM grades against key points
# speaking    -> spoken practice typed out by the learner, LLM grades the script
QUESTION_TYPES = ("choice", "translation", "writing", "speaking")


class Quiz(Base):
    __tablename__ = "quizzes"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(GUID(), ForeignKey("sources.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(GUID(), ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    prompt_version: Mapped[str] = mapped_column(String(64), default="quiz_generate.v2")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    #: Tombstone for "deleted this version but kept its answer archive".
    #: quiz_attempts.quiz_id is a NOT NULL foreign key, so a version that still has
    #: submission records cannot simply be dropped. When set, the version is hidden
    #: from the picker (and from "latest") but keeps resolving titles in the records
    #: list. Deleting the records too removes the row for good.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    quiz_id: Mapped[UUID] = mapped_column(GUID(), ForeignKey("quizzes.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    question: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text)
    correct_index: Mapped[int] = mapped_column(Integer)
    explanation: Mapped[str] = mapped_column(Text, default="")
    chunk_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    # --- v2 ---
    question_type: Mapped[str] = mapped_column(String(32), default="choice")
    # chapter / lesson this question belongs to, e.g. "Chapter 2 — Photosynthesis"
    section_title: Mapped[str] = mapped_column(String(255), default="")
    # model answer used for open-ended grading and for the answer sheet
    reference_answer: Mapped[str] = mapped_column(Text, default="")
    # grading rubric hints produced at generation time
    rubric: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    quiz_id: Mapped[UUID] = mapped_column(GUID(), ForeignKey("quizzes.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(GUID(), ForeignKey("users.id"), index=True)
    answers_json: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    # full per-question record: answer, verdict, AI explanation, reference answer
    details_json: Mapped[str] = mapped_column(Text, default="[]")
    # how many questions the LLM actually graded (0 when everything was multiple choice)
    graded_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
