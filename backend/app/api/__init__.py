from fastapi import APIRouter

from app.api import (
    auth,
    knowledge,
    llm,
    notes,
    quiz,
    sources,
    sources_epub,
    system,
    tasks,
    tutor,
    usage,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(llm.router)
api_router.include_router(system.router)
api_router.include_router(sources.router)
# EPUB reader endpoints (chapter list / sanitised chapter / image proxy).
api_router.include_router(sources_epub.router)
api_router.include_router(tasks.router)
api_router.include_router(knowledge.router)
api_router.include_router(notes.router)
api_router.include_router(quiz.router)
api_router.include_router(tutor.router)
api_router.include_router(usage.router)
