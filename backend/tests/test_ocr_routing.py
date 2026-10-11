"""Deterministic coverage of the OCR / vision routing contract.

Locks the bug that made an uploaded book screenshot produce "cannot read this":
`transcribe_image` used to inherit the text-only default (DeepSeek, which has no
vision model) and fail at request time with an opaque vendor error. The router
must never do that — it either finds a provider that can actually see, or says
loudly that it cannot, and `describe_routes()` must survive either way.
"""
from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services.llm.router import LLMConfigError, ModelRouter, modality_for
from app.services.ocr import NullOcr, RapidOcrEngine, get_ocr, vision_route_available

VISION_TASK = "transcribe_image"
VISION_KEY_ENVS = (
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "SILICONFLOW_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_PROVIDER_VISION",
)


@pytest.fixture(autouse=True)
def fresh_settings():
    """`get_settings` is lru_cached; env changes in a test must be visible."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _router_without_vision_keys(monkeypatch) -> ModelRouter:
    """A router whose only usable key belongs to a text-only vendor.

    `LLM_PROVIDER_VISION` is cleared explicitly: a real machine pins it in .env
    (siliconflow, so image OCR always lands on a vision model), and that setting
    would otherwise make these tests assert the opposite of what they describe.
    """
    for env in VISION_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("LLM_PROVIDER_VISION", raising=False)
    monkeypatch.setenv("LLM_PROVIDER_VISION", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key-0000000000")
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "deepseek")
    from app.core.config import get_settings

    get_settings.cache_clear()
    return ModelRouter()


def test_transcribe_image_is_routed_as_vision():
    assert modality_for(VISION_TASK) == "vision"


def test_vision_task_never_borrows_a_text_only_provider(monkeypatch):
    router = _router_without_vision_keys(monkeypatch)
    assert router.default_name == "deepseek"
    # DeepSeek really has no vision model — this is the premise of the guard.
    assert not router.providers["deepseek"].supports_vision()

    with pytest.raises(LLMConfigError) as exc:
        router.provider_name_for(VISION_TASK)
    message = str(exc.value)
    assert "vision" in message.lower()
    assert "rapidocr-onnxruntime" in message  # the offline escape hatch is offered


def test_describe_routes_reports_vision_unavailable_without_raising(monkeypatch):
    router = _router_without_vision_keys(monkeypatch)
    rows = router.describe_routes()  # must not blow up into a 500
    by_task = {row["task"]: row for row in rows}

    vision = by_task[VISION_TASK]
    assert vision["modality"] == "vision"
    assert vision["ready"] is False
    assert vision["provider"] == "unavailable"
    assert vision["error"]

    # The text tasks keep working — one unavailable modality must not poison them.
    assert by_task["summarize"]["ready"] is True


def test_vision_route_available_explains_instead_of_crashing(monkeypatch):
    router = _router_without_vision_keys(monkeypatch)
    ok, why = vision_route_available(router)
    assert ok is False
    assert why


def test_vision_provider_used_when_one_really_has_a_vision_model(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDER_VISION", "")   # let discovery pick
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-dashscope-key-0000000")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-key-0000000000")
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "deepseek")

    router = ModelRouter()
    assert router.provider_name_for(VISION_TASK) == "dashscope"
    assert router.provider_for(VISION_TASK).model_for(VISION_TASK) == "qwen-vl-max"


def test_get_ocr_auto_prefers_the_offline_engine(monkeypatch):
    """With no vision key configured, `auto` must still read images offline."""
    monkeypatch.setenv("OCR_PROVIDER", "auto")
    for env in VISION_KEY_ENVS:
        monkeypatch.delenv(env, raising=False)

    engine = get_ocr(None)
    if RapidOcrEngine.importable():
        assert engine.name == "rapidocr"
    else:
        # Not installed in this environment: it must explain, not silently no-op.
        assert isinstance(engine, NullOcr)
        assert "rapidocr-onnxruntime" in engine.reason


def test_ocr_provider_none_disables_with_instructions(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "none")
    engine = get_ocr(None)
    assert isinstance(engine, NullOcr)
    assert "OCR_PROVIDER=rapidocr" in engine.reason
