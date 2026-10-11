from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import GUID


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(GUID(), ForeignKey("users.id"), index=True)
    course_id: Mapped[UUID | None] = mapped_column(GUID(), ForeignKey("courses.id"), nullable=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # pdf | video | image | epub
    storage_key: Mapped[str] = mapped_column(String(1024))
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: printed_page - physical page_number, resolved from the running heads.
    page_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: JSON list of {level, number, title, page_number, printed_page, page_end}.
    section_outline_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
