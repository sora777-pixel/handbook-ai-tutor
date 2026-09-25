from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("app.migrate")

# Idempotent column additions for SQLite deployments.
#
# SQLite's `CREATE TABLE IF NOT EXISTS` (what Base.metadata.create_all does)
# never alters an existing table, so a database created before this release
# would be missing the new quiz columns. Alembic handles PostgreSQL; this keeps
# the docker-less SQLite path upgradeable without wiping learner data.
#
# Names are hardcoded constants, never user input.
SQLITE_COLUMN_UPGRADES: dict[str, dict[str, str]] = {
    # Tombstone for "deleted this version but kept its answer archive" — see
    # app/api/quiz.py::delete_quiz.
    "quizzes": {
        "deleted_at": "DATETIME",
    },
    "quiz_questions": {
        "question_type": "VARCHAR(32) DEFAULT 'choice'",
        "section_title": "VARCHAR(255) DEFAULT ''",
        "reference_answer": "TEXT DEFAULT ''",
        "rubric": "TEXT DEFAULT ''",
        "instructions": "TEXT DEFAULT ''",
    },
    "quiz_attempts": {
        "details_json": "TEXT DEFAULT '[]'",
        "graded_count": "INTEGER DEFAULT 0",
    },
    # Layout analysis: every chunk now records what it is and where it sits, so
    # "第569页" and "第三章讲了什么" can be answered from metadata instead of
    # hoping cosine similarity notices a number inside a question.
    "document_chunks": {
        "printed_page": "INTEGER",
        "section_title": "VARCHAR(512)",
        "content_type": "VARCHAR(32) DEFAULT 'body'",
        "heading_level": "INTEGER",
    },
    "sources": {
        "page_offset": "INTEGER",
        "section_outline_json": "TEXT",
        "page_count": "INTEGER",
    },
}

# Tables introduced in this release. create_all builds them; listed here so the
# upgrade report is explicit about what appeared.
NEW_TABLES = ("source_notes",)


async def ensure_sqlite_schema(engine: AsyncEngine) -> list[str]:
    """Add any missing columns on SQLite. Returns the applied changes."""
    if engine.dialect.name != "sqlite":
        return []
    applied: list[str] = []
    async with engine.begin() as conn:
        for table, columns in SQLITE_COLUMN_UPGRADES.items():
            rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            if not rows:
                continue  # not created yet -> create_all will build it fully
            existing = {row[1] for row in rows}
            for name, ddl in columns.items():
                if name in existing:
                    continue
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                applied.append(f"{table}.{name}")
                logger.info("sqlite schema upgrade: added column %s.%s", table, name)
    return applied


async def describe_schema(engine: AsyncEngine) -> dict[str, list[str]]:
    """Table -> columns. Used by the diagnostics endpoint."""
    out: dict[str, list[str]] = {}
    async with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            names = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                )
            ).fetchall()
            for (name,) in names:
                cols = (await conn.execute(text(f"PRAGMA table_info({name})"))).fetchall()
                out[name] = [c[1] for c in cols]
    return out
