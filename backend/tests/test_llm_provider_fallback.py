"""Provider-level fallback: one broken vendor must not kill an ingest.

The incident this encodes: providers.yaml pointed SiliconFlow's
`transcribe_image` at `Qwen/Qwen2.5-VL-72B-Instruct`, which SiliconFlow retired
upstream. Every image / scanned-PDF ingest died with a bare
`403 Forbidden for url .../chat/completions` -- the key was fine, the model was
gone. Two behaviours are pinned here:

1. `ProviderHTTPError` keeps the vendor's own words (code 30003 / "Model
   disabled.") so the message is a diagnosis, not a status code.
2. `ModelRouter.complete` walks to another keyed vendor instead of failing, and
   never to mock (a mock summary is a fake lesson).
"""

from __future__ import annotations

import pytest

from app.services.llm.base import (
    ChatMessage,
    EmbedResult,
    LLMProvider,
    LLMResult,
    ProviderHTTPError,
)
from app.services.llm.router import ModelRouter

VISION_MODEL = "Qwen/Qwen3-VL-32B-Instruct"


def _disabled(model: str = "Qwen/Qwen2.5-VL-72B-Instruct", code: str = "30003") -> ProviderHTTPError:
    return ProviderHTTPError(
        status_code=403,
        provider="siliconflow",
        model=model,
        endpoint="https://api.siliconflow.cn/v1/chat/completions",
        vendor_code=code,
        vendor_message="Model disabled.",
        body_excerpt='{"code":30003,"message":"Model disabled.","data":null}',
    )


class _FlakyVision(LLMProvider):
    """Stands in for siliconflow: always refuses, like a retired model."""

    name = "siliconflow"
    models = {"transcribe_image": "Qwen/Qwen2.5-VL-72B-Instruct", "default": "x"}

    def model_for(self, task: str) -> str:
        return self.models.get(task, "x")

    def supports_vision(self) -> bool:
        return True

    async def complete(self, *, task, messages, json_mode=False) -> LLMResult:
        raise _disabled()

    async def embed(self, *, task, texts) -> EmbedResult:  # pragma: no cover
        raise _disabled()


class _WorkingVision(LLMProvider):
    name = "dashscope"
    models = {"transcribe_image": "qwen-vl-max", "default": "qwen-plus"}

    def model_for(self, task: str) -> str:
        return self.models.get(task, "qwen-plus")

    def supports_vision(self) -> bool:
        return True

    async def complete(self, *, task, messages, json_mode=False) -> LLMResult:
        return LLMResult(content='{"text":"ocr"}', provider=self.name, model="qwen-vl-max")

    async def embed(self, *, task, texts) -> EmbedResult:  # pragma: no cover
        raise AssertionError("embed must not be called")


# ------------------------------------------------------------ error payload


def test_provider_error_keeps_vendor_words():
    err = _disabled()
    text = str(err)
    assert "Model disabled." in text
    assert "30003" in text
    assert "403" in text
    assert err.worth_another_provider is True


def test_plain_400_is_not_retried_elsewhere():
    err = ProviderHTTPError(
        status_code=400,
        provider="openai",
        model="gpt-4o-mini",
        endpoint="https://api.openai.com/v1/chat/completions",
        vendor_code=None,
        vendor_message="invalid request: messages must not be empty",
    )
    assert err.worth_another_provider is False


def test_model_does_not_exist_400_is_retried():
    err = ProviderHTTPError(
        status_code=400,
        provider="siliconflow",
        model="deepseek/deepseek-chat",
        endpoint="https://api.siliconflow.cn/v1/chat/completions",
        vendor_code=20012,
        vendor_message="Model does not exist. Please check it carefully.",
    )
    assert err.worth_another_provider is True


# ----------------------------------------------------------------- fallback


@pytest.mark.asyncio
async def test_complete_falls_back_to_another_vendor(monkeypatch):
    router = ModelRouter()
    router.providers = {"mock": router.providers["mock"], "siliconflow": _FlakyVision(), "dashscope": _WorkingVision()}
    monkeypatch.setattr(router, "provider_for", lambda task: router.providers["siliconflow"])
    monkeypatch.setattr(router, "_fallback_candidates", lambda task, failed: ["dashscope"])

    result = await router.complete(
        task="transcribe_image",
        messages=[ChatMessage(role="user", content="read this page")],
    )

    assert result.provider == "dashscope"
    assert result.model == "qwen-vl-max"
    # the switch must be visible, never silent
    assert router.fallback_note and "siliconflow" in router.fallback_note


@pytest.mark.asyncio
async def test_no_fallback_to_mock_even_when_mock_is_the_only_one_left(monkeypatch):
    router = ModelRouter()
    router.providers = {"mock": router.providers["mock"], "siliconflow": _FlakyVision()}
    monkeypatch.setattr(router, "provider_for", lambda task: router.providers["siliconflow"])
    monkeypatch.setattr(router, "_fallback_candidates", lambda task, failed: [])

    with pytest.raises(ProviderHTTPError) as caught:
        await router.complete(task="transcribe_image", messages=[ChatMessage(role="user", content="x")])
    assert "Model disabled." in str(caught.value)


@pytest.mark.asyncio
async def test_all_vendors_failing_lists_every_attempt(monkeypatch):
    router = ModelRouter()
    router.providers = {"mock": router.providers["mock"], "siliconflow": _FlakyVision(), "dashscope": _WorkingVision()}
    monkeypatch.setattr(router, "provider_for", lambda task: router.providers["siliconflow"])
    monkeypatch.setattr(router, "_fallback_candidates", lambda task, failed: ["dashscope"])

    # make the second vendor fail too
    class _AlsoBroken(_WorkingVision):
        name = "dashscope"

        async def complete(self, *, task, messages, json_mode=False):
            raise ProviderHTTPError(
                status_code=401,
                provider="dashscope",
                model="qwen-vl-max",
                endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                vendor_code="InvalidApiKey",
                vendor_message="Invalid API-key provided.",
            )

    router.providers["dashscope"] = _AlsoBroken()

    with pytest.raises(ProviderHTTPError) as caught:
        await router.complete(task="transcribe_image", messages=[ChatMessage(role="user", content="x")])
    cause = caught.value.__cause__
    assert cause is not None
    assert "siliconflow" in str(cause) and "dashscope" in str(cause)


def test_fallback_candidates_excludes_mock_and_keyless_providers(monkeypatch):
    router = ModelRouter()
    monkeypatch.setattr("app.services.llm.router._usable_key", lambda v: bool(v))
    monkeypatch.setattr(
        "app.services.llm.router.os.environ",
        {"DASHSCOPE_API_KEY": "k", "DEEPSEEK_API_KEY": "", "SILICONFLOW_API_KEY": "k"},
    )
    router.config = {
        "providers": {
            "dashscope": {"api_key_env": "DASHSCOPE_API_KEY", "models": {"transcribe_image": "qwen-vl-max", "default": "q"}},
            "deepseek": {"api_key_env": "DEEPSEEK_API_KEY", "models": {"default": "deepseek-chat"}},
            "siliconflow": {"api_key_env": "SILICONFLOW_API_KEY", "models": {"transcribe_image": VISION_MODEL, "default": "Qwen/Qwen2.5-7B-Instruct", "embed": "BAAI/bge-m3"}},
            "mock": {"type": "mock", "models": {"default": "mock-llm"}},
        }
    }
    router.providers = {"mock": router.providers["mock"], "dashscope": object(), "siliconflow": object()}

    names = router._fallback_candidates("transcribe_image", failed="siliconflow")
    assert "dashscope" in names          # keyed + vision capable
    assert "siliconflow" not in names     # the failed one
    assert "mock" not in names            # never silently ingest mock output
    assert "deepseek" not in names        # no key, and no vision model

# --------------------------------------------------- transport-level failures


def test_connect_failure_is_retried_on_another_provider():
    """`All connection attempts failed` must not end the ingest.

    An unreachable endpoint (DNS, refused, proxy) produces no HTTP status at
    all; that case is marked retryable so the router walks to the next vendor
    instead of failing the whole import.
    """
    err = ProviderHTTPError(
        status_code=0,
        provider="deepseek",
        model="deepseek-chat",
        endpoint="https://api.deepseek.com/chat/completions",
        vendor_message="ConnectError: All connection attempts failed",
        task="summarize",
    )
    assert err.worth_another_provider is True
    assert err.task == "summarize"
    assert "All connection attempts failed" in str(err)


def test_the_error_names_the_task_that_actually_failed():
    """A failed vision OCR used to be reported as a deepseek text failure."""
    err = ProviderHTTPError(
        status_code=403,
        provider="siliconflow",
        model="Qwen/Qwen3-VL-32B-Instruct",
        endpoint="https://api.siliconflow.cn/v1/chat/completions",
        vendor_code=30003,
        vendor_message="Model disabled.",
        task="transcribe_image",
    )
    from app.services.pipeline import _failing_task

    assert _failing_task(err) == "transcribe_image"
