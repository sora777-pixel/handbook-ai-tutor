from __future__ import annotations

import logging
import os

import httpx

from app.core.config import get_settings
from app.services.llm.base import (
    ChatMessage,
    EmbedResult,
    ImagePart,
    LLMProvider,
    LLMResult,
    ProviderHTTPError,
    TextPart,
)
from app.utils.text import normalise_text

logger = logging.getLogger("app.llm")


def _vendor_error(resp: httpx.Response) -> tuple[str | int | None, str]:
    """Pull (code, message) out of a vendor's JSON error body.

    SiliconFlow answers `{"code":30003,"message":"Model disabled.","data":null}`;
    OpenAI-compatible vendors mostly use `{"error":{"message":...}}`. Anything
    unparseable comes back as an empty message and the caller keeps the raw
    excerpt, because "403 Forbidden" on its own is never a diagnosis.
    """
    try:
        data = resp.json()
    except Exception:
        return None, ""
    if isinstance(data, dict):
        code = data.get("code")
        message = data.get("message") or data.get("error_msg") or ""
        err = data.get("error")
        if isinstance(err, dict) and not message:
            message = err.get("message") or ""
            code = code or err.get("code") or err.get("type")
        if not message and isinstance(err, str):
            message = err
        return (code if code not in (None, "") else None), str(message or "")
    return None, ""


def _content_parts(content) -> str | list[dict]:
    """Render one message body in OpenAI chat-completions format.

    Multimodal messages become the `[{type: text}, {type: image_url}]` array that
    every OpenAI-compatible vendor (DashScope, SiliconFlow, OpenAI, …) accepts.
    """
    if isinstance(content, str):
        return content
    parts: list[dict] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.data_url, "detail": part.detail},
                }
            )
    return parts or ""


class OpenAICompatibleProvider(LLMProvider):
    """Config-driven OpenAI-compatible HTTP API (DashScope, DeepSeek, SiliconFlow, OpenAI, …)."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        models: dict[str, str],
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.models = models
        self.timeout = timeout
        self.dim = get_settings().embedding_dim

    def _model_for(self, task: str) -> str:
        return self.models.get(task) or self.models.get("default") or "unknown"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self, *, trust_env: bool | None = None) -> httpx.AsyncClient:
        """HTTP client with a short connect timeout and optional proxy support.

        `connect=5` matters: an unreachable or black-holed endpoint used to hold
        the ingest for the full 120s read timeout. A dead host now fails in ~5s
        so the router can try another provider immediately.

        `trust_env` decides whether HTTP(S)_PROXY is honoured. It stays on by
        default (that is how corporate networks work), and `_post` retries once
        *without* it when the proxied attempt fails -- a stale or half-dead
        local proxy exported into the environment otherwise makes every vendor
        look offline ("All connection attempts failed").
        """
        if trust_env is None:
            configured = os.environ.get("LLM_TRUST_ENV", "1").strip().lower()
            trust_env = configured not in ("0", "false", "no", "off")
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=self.timeout, write=30.0, pool=5.0),
            trust_env=trust_env,
        )

    async def _post(self, url: str, payload: dict, model: str, task: str) -> httpx.Response:
        """POST with one proxy-less retry, mapping every transport failure."""
        attempts = [None]
        env_flag = os.environ.get("LLM_TRUST_ENV", "1").strip().lower()
        if env_flag not in ("0", "false", "no", "off"):
            attempts.append(False)          # second try: ignore proxy settings
        last: Exception | None = None
        for trust_env in attempts:
            try:
                async with self._client(trust_env=trust_env) as client:
                    return await client.post(url, headers=self._headers(), json=payload)
            except httpx.TransportError as exc:
                last = exc
                if trust_env is None:
                    logger.info(
                        "%s %s unreachable via the environment proxy configuration; retrying direct",
                        self.name,
                        task,
                    )
        raise ProviderHTTPError(
            status_code=0,
            provider=self.name,
            model=model,
            endpoint=url,
            vendor_message=f"{type(last).__name__}: {last}",
            task=task,
        ) from last

    async def complete(
        self,
        *,
        task: str,
        messages: list[ChatMessage],
        json_mode: bool = False,
    ) -> LLMResult:
        model = self._model_for(task)
        payload: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": _content_parts(m.content)} for m in messages],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # trust_env=True (default) honors HTTP(S)_PROXY — never hardcode a proxy
        endpoint = f"{self.base_url}/chat/completions"
        # Transport failures (DNS, refused, TLS, timeout) come back in the same
        # shape as HTTP errors so the router can fall back to another provider;
        # an unreachable endpoint used to abort the whole ingest instead.
        resp = await self._post(endpoint, payload, model, task)
        if resp.status_code >= 400:
                code, message = _vendor_error(resp)
                raise ProviderHTTPError(
                    status_code=resp.status_code,
                    provider=self.name,
                    model=model,
                    endpoint=f"{self.base_url}/chat/completions",
                    vendor_code=code,
                    vendor_message=message,
                    body_excerpt=resp.text or "",
                    task=task,
                )
        data = resp.json()
        # Most vendors return a plain string, but some vision models reply with a
        # content array. Accept both so an OCR call never dies on shape.
        raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(raw, list):
            choice = "".join(
                p.get("text", "") for p in raw if isinstance(p, dict) and p.get("type") == "text"
            )
        else:
            choice = raw or ""
        usage = data.get("usage") or {}
        return LLMResult(
            content=normalise_text(choice),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            provider=self.name,
            model=model,
        )

    async def embed(self, *, task: str, texts: list[str]) -> EmbedResult:
        from app.services.llm.models import looks_like_chat_model

        model = self.models.get("embed") or ""
        if not model or looks_like_chat_model(model):
            raise RuntimeError(
                f'Provider "{self.name}" refused to call {self.base_url}/embeddings '
                f'with chat model "{model or self._model_for(task)}". '
                "Set LLM_PROVIDER_EMBED to siliconflow (BAAI/bge-m3) or dashscope "
                "(text-embedding-v3), or fill in the Embedding API in 设置."
            )
        endpoint = f"{self.base_url}/embeddings"
        resp = await self._post(endpoint, {"model": model, "input": texts}, model, task)
        if resp.status_code >= 400:
                code, message = _vendor_error(resp)
                raise ProviderHTTPError(
                    status_code=resp.status_code,
                    provider=self.name,
                    model=model,
                    endpoint=f"{self.base_url}/embeddings",
                    vendor_code=code,
                    vendor_message=message,
                    body_excerpt=resp.text or "",
                    task=task,
                )
        data = resp.json()
        items = sorted(data.get("data") or [], key=lambda x: x.get("index", 0))
        vectors = [item["embedding"] for item in items]
        usage = data.get("usage") or {}
        return EmbedResult(
            vectors=vectors,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            provider=self.name,
            model=model,
            dim=len(vectors[0]) if vectors else self.dim,
        )


def env_or_override(name: str, fallback: str = "") -> str:
    override = os.environ.get("OPENAI_COMPAT_API_KEY")
    if name == "OPENAI_COMPAT_API_KEY" or not os.environ.get(name):
        if os.environ.get("OPENAI_COMPAT_BASE_URL") and override:
            return override
    return os.environ.get(name, fallback)
