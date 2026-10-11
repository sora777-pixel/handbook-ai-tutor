from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field


class TextPart(BaseModel):
    """A plain text block inside a multimodal message."""

    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    """An inline image carried as a data URL.

    Data URLs keep the whole pipeline self-contained: no public bucket, no signed
    URL that expires mid-request, no extra round trip to the storage backend.
    """

    type: Literal["image_url"] = "image_url"
    data_url: str
    mime: str = "image/png"
    detail: str = "auto"

    @property
    def base64_payload(self) -> str:
        """The raw base64 body, without the `data:<mime>;base64,` prefix."""
        _, _, payload = self.data_url.partition(",")
        return payload or self.data_url


ContentPart = TextPart | ImagePart


class ChatMessage(BaseModel):
    """One chat turn. `content` is either plain text or a list of content parts.

    Text-only callers keep passing a plain string; multimodal callers pass
    `[TextPart(...), ImagePart(...)]`. Every provider serialises this into its own
    wire format, so business code never touches a vendor schema.
    """

    role: str
    content: str | list[ContentPart] = ""

    # ---------------------------------------------------------------- helpers

    def text(self) -> str:
        """Flatten to plain text. Images become a short marker so logs stay readable."""
        if isinstance(self.content, str):
            return self.content
        chunks: list[str] = []
        for part in self.content:
            if isinstance(part, TextPart):
                chunks.append(part.text)
            else:
                chunks.append(f"[image {part.mime}]")
        return "\n".join(chunks)

    def images(self) -> list[ImagePart]:
        if isinstance(self.content, str):
            return []
        return [part for part in self.content if isinstance(part, ImagePart)]

    @classmethod
    def user_text(cls, text: str) -> "ChatMessage":
        return cls(role="user", content=text)

    @classmethod
    def user_with_image(cls, text: str, image: ImagePart) -> "ChatMessage":
        return cls(role="user", content=[TextPart(text=text), image])


def message_text(messages: list["ChatMessage"]) -> str:
    """All messages flattened to text — used for token estimates and logging."""
    return "\n".join(m.text() for m in messages)


class LLMResult(BaseModel):
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str
    model: str


class EmbedResult(BaseModel):
    vectors: list[list[float]]
    prompt_tokens: int = 0
    provider: str
    model: str
    dim: int = Field(default=1536)


class ProviderHTTPError(RuntimeError):
    """A vendor answered with an HTTP error, carrying what it actually said.

    `raise_for_status()` alone produced "Client error '403 Forbidden' for url
    ..." on the learner-facing task error, which says nothing actionable. The
    body is the diagnosis: SiliconFlow answers `{"code": 30003, "message":
    "Model disabled."}`, i.e. the configured checkpoint was switched off
    upstream -- nothing is wrong with the API key. Without this class nobody
    can tell "your key is invalid" (401) from "that model is gone" (403) from
    "that model name never existed" (400/code 20012).

    `worth_another_provider` marks the failures that another vendor can still
    answer: revoked key, disabled/renamed model, quota, rate limit, upstream
    outage. A malformed request (400 without a model-related vendor code) is
    not retried elsewhere -- it would fail identically everywhere.
    """

    def __init__(
        self,
        *,
        status_code: int,
        provider: str,
        model: str,
        endpoint: str,
        vendor_code: str | int | None = None,
        vendor_message: str = "",
        body_excerpt: str = "",
        task: str = "",
    ) -> None:
        self.status_code = status_code
        #: The routed task this call belonged to ("summarize", "embed",
        #: "transcribe_image", …). Carrying it means the ingest error can name
        #: the real culprit instead of guessing -- a failed vision OCR was once
        #: reported as a deepseek text failure.
        self.task = task
        self.provider = provider
        self.model = model
        self.endpoint = endpoint
        self.vendor_code = vendor_code
        self.vendor_message = vendor_message
        self.body_excerpt = body_excerpt

        bits = [f"{provider}/{model} -> HTTP {status_code}"]
        if vendor_code not in (None, ""):
            bits.append(f"code {vendor_code}")
        detail = vendor_message or body_excerpt
        if detail:
            bits.append(detail[:300])
        super().__init__(" | ".join(bits))
        self.short = " | ".join(bits)

    @property
    def worth_another_provider(self) -> bool:
        if self.status_code == 0:
            # No HTTP answer at all (DNS, refused, TLS, proxy, timeout). Another
            # vendor is very often reachable when one host is not.
            return True
        if self.status_code in (401, 403, 404, 408, 409, 425, 429):
            return True
        if self.status_code >= 500:
            return True
        # 400 with a vendor code that names the model / quota is the vendor
        # saying "I cannot serve this model", which another vendor can.
        code = str(self.vendor_code or "")
        if self.status_code == 400 and code in {
            "20012",  # SiliconFlow: Model does not exist
            "30003",  # SiliconFlow: Model disabled
            "20015",
            "10004",
        }:
            return True
        return False


class LLMProvider(ABC):
    """Vendor-agnostic LLM + embedding provider. Business code never imports a vendor SDK."""

    name: str

    def model_for(self, task: str) -> str:
        """Resolve the concrete model name this provider would use for a task."""
        models = getattr(self, "models", None) or {}
        return str(models.get(task) or models.get("default") or getattr(self, "model", "unknown"))

    def supports_vision(self) -> bool:
        """True when this provider has a model configured for `transcribe_image`."""
        models = getattr(self, "models", None) or {}
        return bool(models.get("transcribe_image"))

    @abstractmethod
    async def complete(
        self,
        *,
        task: str,
        messages: list[ChatMessage],
        json_mode: bool = False,
    ) -> LLMResult:
        raise NotImplementedError

    @abstractmethod
    async def embed(self, *, task: str, texts: list[str]) -> EmbedResult:
        raise NotImplementedError
