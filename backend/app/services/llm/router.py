from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import BACKEND_DIR, get_settings
from app.models.usage import TokenUsage
from app.services.llm.anthropic import AnthropicProvider
from app.services.llm.base import (
    ChatMessage,
    EmbedResult,
    LLMProvider,
    LLMResult,
    ProviderHTTPError,
)
from app.services.llm.mock import MockProvider
from app.services.llm.ollama import OllamaProvider
from app.services.llm.models import looks_like_chat_model
from app.services.llm.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger("app.llm")


class LLMConfigError(RuntimeError):
    """Raised when routing cannot be resolved to a usable provider.

    Deliberately loud: silently falling back to MockLLM hides a broken API key
    or a bad provider name behind plausible-looking fake results.
    """


# task -> modality. Text tasks read/write language; audio tasks consume
# transcripts of speech; vision tasks read pixels; embed produces vectors.
TEXT_TASKS = {
    "summarize",
    "extract_knowledge",
    "quiz_generate",
    "quiz_explain",
    "tutor",
    "notes_generate",
}
AUDIO_TASKS = {"segment_summarize"}
EMBED_TASKS = {"embed"}
VISION_TASKS = {"transcribe_image"}

MODALITY_ENV = {
    "text": "LLM_PROVIDER_TEXT",
    "audio": "LLM_PROVIDER_AUDIO",
    "embed": "LLM_PROVIDER_EMBED",
    "vision": "LLM_PROVIDER_VISION",
}

#: The Embedding API filled in on the settings page. It lives outside
#: providers.yaml because it is entered at runtime, and it is registered as a
#: synthetic provider spec so readiness checks and the diagnostics table treat
#: it exactly like a vendor from the file.
EMBED_OVERRIDE_NAME = "embed_override"
EMBED_OVERRIDE_ENV = {
    "base": "EMBED_API_BASE",
    "key": "EMBED_API_KEY",
    "model": "EMBED_MODEL",
}

# Order used by auto-detection when no provider is pinned explicitly.
AUTO_ORDER = ("dashscope", "deepseek", "siliconflow", "openai", "anthropic")

EMBED_MISSING_MESSAGE = (
    "当前没有可用的向量模型。DeepSeek 只有对话接口，不能调用 /embeddings，"
    "也绝不能把 deepseek-chat 发到 SiliconFlow / DashScope 的 embeddings 地址。"
    "请设置 LLM_PROVIDER_EMBED=siliconflow（需 SILICONFLOW_API_KEY，模型 BAAI/bge-m3）"
    "或 LLM_PROVIDER_EMBED=dashscope（需 DASHSCOPE_API_KEY，模型 text-embedding-v3），"
    "或在「设置」中填写 Embedding API。"
    " Embedding is unavailable: DeepSeek has no /embeddings endpoint. "
    "Set LLM_PROVIDER_EMBED to a vendor that declares an embed model "
    "(siliconflow / dashscope / openai), or configure the Embedding API."
)

# Providers that can read images, best-first. DeepSeek has no vision model, so it
# is deliberately absent: an OCR call must never land on it by accident.
VISION_AUTO_ORDER = ("dashscope", "openai", "siliconflow", "anthropic", "ollama")

# Obvious placeholders that must not count as "a key was configured".
_PLACEHOLDER_KEYS = {"-", "none", "null", "changeme", "your-key", "your_api_key", "xxx", "todo"}
_MIN_KEY_LEN = 8


def _usable_key(value: str | None) -> bool:
    raw = (value or "").strip()
    if len(raw) < _MIN_KEY_LEN:
        return False
    return raw.lower() not in _PLACEHOLDER_KEYS


def modality_for(task: str) -> str:
    if task in EMBED_TASKS:
        return "embed"
    if task in AUDIO_TASKS:
        return "audio"
    if task in VISION_TASKS:
        return "vision"
    return "text"


class ModelRouter:
    """Routes task="..." to a configured provider. Records TokenUsage on every call.

    Resolution order (first hit wins):
      1. env LLM_TASK_ROUTES (JSON map, task -> provider)
      2. providers.yaml  task_routes[task]
      3. env LLM_PROVIDER_TEXT / LLM_PROVIDER_AUDIO / LLM_PROVIDER_EMBED by modality
      4. env LLM_DEFAULT_PROVIDER
      5. providers.yaml  default_provider
      6. "mock"
    """

    def __init__(self, session: AsyncSession | None = None) -> None:
        self.session = session
        self.settings = get_settings()
        self.config = self._load_config()
        #: Specs that do not come from providers.yaml. Registered before the
        #: providers are built so every lookup below sees them like any vendor.
        self._synthetic_specs: dict[str, dict] = {}
        self._register_embed_override()
        self.providers = self._build_providers()
        self.task_routes: dict[str, str] = {
            str(k): str(v) for k, v in (self.config.get("task_routes") or {}).items()
        }
        self.env_task_routes = self._load_env_task_routes()
        # Empty LLM_DEFAULT_PROVIDER means "auto": use a vendor whose key is
        # actually present, instead of silently answering with MockLLM.
        self.default_name = (
            (self.settings.llm_default_provider or "").strip()
            or (self.config.get("default_provider") or "").strip()
            or "mock"
        )
        self.auto_detected = False
        self.embed_note: str | None = None
        #: Set when a call had to be served by a different vendor than the one
        #: configured (disabled model, revoked key, rate limit). Surfaced in the
        #: ingest note so a surprise provider is never invisible.
        self.fallback_note: str | None = None
        if self.default_name == "mock" and not (self.settings.llm_default_provider or "").strip():
            detected = self._first_provider_with_key()
            if detected:
                self.default_name = detected
                self.auto_detected = True
                logger.info(
                    "LLM_DEFAULT_PROVIDER is unset; auto-selected %r because its API key is present",
                    detected,
                )

    @property
    def settings_path(self) -> str:
        return str(self._resolve_config_path())

    # ---------- config loading ----------

    def _resolve_config_path(self) -> Path:
        """Prefer the configured path, but never degrade to mock just because it is wrong.

        .env.example ships PROVIDERS_CONFIG_PATH=/app/config/providers.yaml, which
        only exists inside Docker. Locally that used to make every task fall back
        to MockLLM with no warning. A blank value (the documented local setting)
        must also mean "use the bundled file" — `Path("")` is `.`, which exists,
        so an unguarded `configured.is_file()` would try to open a directory.
        """
        raw = (self.settings.providers_config_path or "").strip()
        configured = Path(raw)
        if raw and configured.is_file():
            return configured
        bundled = BACKEND_DIR / "config" / "providers.yaml"
        if bundled.is_file():
            if raw:
                logger.warning(
                    "PROVIDERS_CONFIG_PATH=%s is not a file; falling back to the bundled %s",
                    raw,
                    bundled,
                )
            return bundled
        return configured

    def _load_config(self) -> dict:
        path = self._resolve_config_path()
        if not path.exists():
            logger.warning("providers config not found at %s; only mock is available", path)
            return {"default_provider": "mock", "task_routes": {}, "providers": {"mock": {"type": "mock"}}}
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _first_provider_with_key(self) -> str | None:
        specs = self.config.get("providers") or {}
        for name in AUTO_ORDER:
            spec = specs.get(name) or {}
            key_env = spec.get("api_key_env") or ""
            if key_env and _usable_key(os.environ.get(key_env)):
                return name
        return None

    def _has_embed_model(self, name: str) -> bool:
        if name == "mock":
            return True
        spec = self._spec_for(name)
        models = spec.get("models") or {}
        embed = models.get("embed")
        return bool(embed) and not looks_like_chat_model(str(embed))

    def _allow_mock_embed(self) -> bool:
        """Mock vectors are for CI / explicit offline mode only.

        A machine that has a real chat key (DeepSeek, …) must not silently
        ingest a fake photosynthesis lesson just because embeddings were
        misconfigured.
        """
        pinned = (self.settings.llm_default_provider or "").strip().lower()
        if pinned == "mock":
            return True
        embed_pinned = (self.settings.llm_provider_embed or "").strip().lower()
        if embed_pinned == "mock":
            return True
        return self.default_name == "mock" and not self._first_provider_with_key()

    def _first_provider_with_embed(self) -> str | None:
        specs = self.config.get("providers") or {}
        for name in AUTO_ORDER:
            spec = specs.get(name) or {}
            key_env = spec.get("api_key_env") or ""
            if not key_env or not _usable_key(os.environ.get(key_env)):
                continue
            if (spec.get("models") or {}).get("embed"):
                return name
        return None

    def _first_provider_with_vision(self) -> str | None:
        """First keyed provider that declares a `transcribe_image` model."""
        specs = self.config.get("providers") or {}
        for name in VISION_AUTO_ORDER:
            spec = specs.get(name) or {}
            if not (spec.get("models") or {}).get("transcribe_image"):
                continue
            key_env = spec.get("api_key_env") or ""
            if spec.get("type") == "ollama" or not key_env:
                continue
            if _usable_key(os.environ.get(key_env)):
                return name
        return None

    def _load_env_task_routes(self) -> dict[str, str]:
        raw = (self.settings.llm_task_routes or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM_TASK_ROUTES is not valid JSON, ignoring: %r", raw[:200])
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _register_embed_override(self) -> None:
        """Turn the settings-page Embedding API into a provider spec.

        Only registered when both an address and a model are present: half a
        configuration would route embeddings at a URL that cannot answer, which
        is worse than falling back to mock. A blank key is allowed on purpose —
        a locally hosted embeddings server usually has no auth.
        """
        base = (os.environ.get(EMBED_OVERRIDE_ENV["base"]) or "").strip()
        model = (os.environ.get(EMBED_OVERRIDE_ENV["model"]) or "").strip()
        if not base or not model:
            return
        spec: dict = {
            "type": "openai_compatible",
            "base_url": base,
            "models": {"default": model, "embed": model},
            "source": "settings",
        }
        if (os.environ.get(EMBED_OVERRIDE_ENV["key"]) or "").strip():
            spec["api_key_env"] = EMBED_OVERRIDE_ENV["key"]
        self._synthetic_specs[EMBED_OVERRIDE_NAME] = spec

    def _build_providers(self) -> dict[str, LLMProvider]:
        built: dict[str, LLMProvider] = {"mock": MockProvider()}
        specs = {**(self.config.get("providers") or {}), **self._synthetic_specs}
        for name, spec in specs.items():
            spec = spec or {}
            ptype = spec.get("type", "mock")
            models = spec.get("models") or {}
            if ptype == "mock":
                built[name] = MockProvider(
                    model=models.get("default", "mock-llm"),
                    embed_model=models.get("embed", "mock-embed"),
                )
            elif ptype == "openai_compatible":
                key_env = spec.get("api_key_env") or ""
                api_key = os.environ.get(key_env, "") if key_env else ""
                # OPENAI_COMPAT_BASE_URL is only the catch-all "openai_compat"
                # vendor. Applying it to DeepSeek / SiliconFlow / DashScope used
                # to send deepseek-chat at https://api.siliconflow.cn/v1/embeddings.
                if name == "openai_compat":
                    base_url = os.environ.get("OPENAI_COMPAT_BASE_URL") or spec.get("base_url") or ""
                    if os.environ.get("OPENAI_COMPAT_MODEL"):
                        models = {**models, "default": os.environ["OPENAI_COMPAT_MODEL"]}
                else:
                    base_url = spec.get("base_url") or ""
                built[name] = OpenAICompatibleProvider(
                    name=name,
                    base_url=base_url,
                    api_key=api_key,
                    models=models,
                )
            elif ptype == "ollama":
                base = os.environ.get("OLLAMA_BASE_URL") or spec.get("base_url") or "http://127.0.0.1:11434"
                built[name] = OllamaProvider(base_url=base, models=models)
            elif ptype == "anthropic":
                key_env = spec.get("api_key_env") or "ANTHROPIC_API_KEY"
                built[name] = AnthropicProvider(
                    name=name,
                    base_url=spec.get("base_url") or "https://api.anthropic.com",
                    api_key=os.environ.get(key_env, ""),
                    models=models,
                )
        return built

    # ---------- resolution ----------

    def provider_name_for(self, task: str) -> str:
        if task in self.env_task_routes:
            return self.env_task_routes[task]
        if task in self.task_routes:
            return self.task_routes[task]
        modality = modality_for(task)
        env_value = getattr(self.settings, f"llm_provider_{modality}", "") or ""
        if env_value.strip():
            return env_value.strip()
        if modality == "vision":
            # An OCR call must never inherit a text-only default: DeepSeek has no
            # vision model, so borrowing it would fail at request time with a
            # confusing vendor error. Only reuse the default when it can actually
            # see, else find a real vision provider — and say so loudly when there
            # is none, because the offline OCR engine is the other way out.
            default_provider = self.providers.get(self.default_name)
            if default_provider is not None and default_provider.supports_vision():
                return self.default_name
            discovered = self._first_provider_with_vision()
            if discovered:
                return discovered
            raise LLMConfigError(
                'No vision-capable provider is configured for task="transcribe_image". '
                f'"{self.default_name}" cannot read images. Either install the offline '
                "engine (pip install rapidocr-onnxruntime, then OCR_PROVIDER=rapidocr / "
                "auto), or add a vision key (e.g. DASHSCOPE_API_KEY) and set "
                "LLM_PROVIDER_VISION=dashscope."
            )
        return self.default_name

    def provider_for(self, task: str) -> LLMProvider:
        name = self.provider_name_for(task)
        if modality_for(task) == "embed":
            name = self._resolve_embed_provider(name)
        provider = self.providers.get(name)
        if provider is None:
            known = ", ".join(sorted(self.providers))
            raise LLMConfigError(
                f'LLM provider "{name}" (task="{task}") is not defined in '
                f"{self._resolve_config_path()}. Known providers: {known}."
            )
        self._validate_ready(name, task)
        return provider

    def _resolve_embed_provider(self, requested: str) -> str:
        """Pick a vendor that actually has an embedding model.

        DeepSeek (and any other chat-only default) must never be used for
        task=embed: its default model is deepseek-chat, which vendors reject
        on /embeddings. Walk, in order:

          1. Embedding API entered in 设置 (must not look like a chat model)
          2. explicit LLM_PROVIDER_EMBED
          3. first keyed vendor that declares a real embed model
          4. local mock — only when the whole app is already in mock/offline mode

        Anything else fails loudly so ingest cannot produce a fake lesson.
        """
        if EMBED_OVERRIDE_NAME in self.providers:
            spec = self._synthetic_specs.get(EMBED_OVERRIDE_NAME) or {}
            model = str((spec.get("models") or {}).get("embed") or "")
            if looks_like_chat_model(model):
                raise LLMConfigError(
                    f'设置里的 Embedding 模型 "{model}" 是对话模型，不能发到 /embeddings。'
                    "请改成向量模型，例如 BAAI/bge-m3 或 text-embedding-v3。"
                    f' The Embedding API model "{model}" looks like a chat checkpoint; '
                    "set a real embedding model."
                )
            self.embed_note = "embeddings via the Embedding API configured in 设置"
            return EMBED_OVERRIDE_NAME

        explicit = (self.settings.llm_provider_embed or "").strip()
        if explicit:
            if explicit == "mock":
                if not self._allow_mock_embed() and self._first_provider_with_key():
                    # Explicit mock while a real chat key exists is allowed — the
                    # operator asked for it — but we still record the note.
                    self.embed_note = "LLM_PROVIDER_EMBED=mock"
                else:
                    self.embed_note = "LLM_PROVIDER_EMBED=mock"
                return "mock"
            if not self.providers.get(explicit):
                raise LLMConfigError(
                    f'LLM_PROVIDER_EMBED="{explicit}" is not defined in providers.yaml. '
                    + EMBED_MISSING_MESSAGE
                )
            if not self._has_embed_model(explicit):
                raise LLMConfigError(
                    f'LLM_PROVIDER_EMBED="{explicit}" 没有可用的 embed 模型。'
                    + EMBED_MISSING_MESSAGE
                )
            self.embed_note = None
            return explicit

        if requested == "mock" and self._allow_mock_embed():
            self.embed_note = None
            return "mock"

        if requested != "mock" and self._has_embed_model(requested):
            self.embed_note = None
            return requested

        candidate = self._first_provider_with_embed()
        if candidate:
            if requested != candidate:
                logger.warning(
                    "provider %r has no embed model; using %r for embeddings",
                    requested,
                    candidate,
                )
                self.embed_note = f"{requested} has no embed model; embeddings via {candidate}"
            else:
                self.embed_note = None
            return candidate

        if self._allow_mock_embed():
            logger.warning("no embed-capable provider configured; using local mock embeddings")
            self.embed_note = "no embed-capable provider configured; using local mock embeddings"
            return "mock"

        raise LLMConfigError(EMBED_MISSING_MESSAGE)

    def _spec_for(self, name: str) -> dict:
        if name == "mock":
            return {}
        if name in self._synthetic_specs:
            return self._synthetic_specs[name]
        return ((self.config.get("providers") or {}).get(name) or {})

    def _validate_ready(self, name: str, task: str) -> None:
        if name == "mock":
            return
        spec = self._spec_for(name)
        if spec.get("type") == "ollama":
            return
        key_env = spec.get("api_key_env") or ""
        if key_env and not _usable_key(os.environ.get(key_env)):
            raise LLMConfigError(
                f'Provider "{name}" is selected for task="{task}" but {key_env} is missing or '
                f"still a placeholder. Put a real key in .env ({key_env}=...) or set "
                f"{MODALITY_ENV.get(modality_for(task), 'LLM_DEFAULT_PROVIDER')}=mock for offline mode."
            )
        models = spec.get("models") or {}
        if modality_for(task) == "embed":
            embed_model = models.get("embed")
            if name != "mock" and (not embed_model or looks_like_chat_model(str(embed_model))):
                raise LLMConfigError(
                    f'Provider "{name}" cannot serve embeddings'
                    + (f' (model "{embed_model}" is a chat checkpoint). ' if embed_model else ". ")
                    + EMBED_MISSING_MESSAGE
                )
        elif not models.get("default") and not models.get(task):
            raise LLMConfigError(
                f'Provider "{name}" has no model configured for task="{task}".'
            )

    # ---------- diagnostics ----------

    def describe_routes(self) -> list[dict]:
        """Human-readable view of what each task will actually call."""
        rows: list[dict] = []
        for task in sorted(TEXT_TASKS | AUDIO_TASKS | EMBED_TASKS | VISION_TASKS):
            modality = modality_for(task)
            try:
                name = self.provider_name_for(task)
                if modality == "embed":
                    self.embed_note = None
                    name = self._resolve_embed_provider(name)
                provider = self.providers.get(name)
                if provider is None:
                    raise LLMConfigError(f'provider "{name}" is not defined in providers.yaml')
                self._validate_ready(name, task)
                spec = self._spec_for(name)
                key_env = spec.get("api_key_env") or ""
                rows.append(
                    {
                        "task": task,
                        "modality": modality,
                        "provider": name,
                        "model": provider.model_for(task),
                        "base_url": getattr(provider, "base_url", None),
                        "api_key_env": key_env or None,
                        "api_key_present": _usable_key(os.environ.get(key_env)) if key_env else None,
                        "ready": True,
                        "error": None,
                        "note": self.embed_note if modality == "embed" else None,
                    }
                )
            except LLMConfigError as exc:
                # provider_name_for can itself raise (a modality with no usable
                # provider at all). Never let that turn diagnostics into a 500.
                try:
                    fallback_name: str | None = self.provider_name_for(task)
                except LLMConfigError:
                    fallback_name = None
                rows.append(
                    {
                        "task": task,
                        "modality": modality,
                        "provider": fallback_name or "unavailable",
                        "model": None,
                        "base_url": None,
                        "api_key_env": None,
                        "api_key_present": None,
                        "ready": False,
                        "error": str(exc),
                        "note": None,
                    }
                )
        return rows

    # ---------- calls ----------

    def _fallback_candidates(self, task: str, failed: str) -> list[str]:
        """Other real vendors that could serve `task`, best first.

        Only vendors with a usable key and a model for this task's modality.
        `mock` is deliberately excluded: silently ingesting a mock summary is
        worse than an honest failure (see _allow_mock_embed). Order follows the
        existing AUTO_ORDER / VISION_AUTO_ORDER preferences so a fallback is
        predictable rather than "whoever happens to be in the yaml".
        """
        modality = modality_for(task)
        specs = self.config.get("providers") or {}
        specs = {**specs, **self._synthetic_specs}
        order = VISION_AUTO_ORDER if modality == "vision" else AUTO_ORDER
        out: list[str] = []
        for name in order:
            if name == failed or name == "mock" or name not in self.providers:
                continue
            spec = specs.get(name) or self._spec_for(name) or {}
            if spec.get("type") == "mock":
                continue
            key_env = spec.get("api_key_env") or ""
            if spec.get("type") != "ollama" and key_env and not _usable_key(
                os.environ.get(key_env)
            ):
                continue
            models = spec.get("models") or {}
            if modality == "embed":
                if not self._has_embed_model(name):
                    continue
            elif modality == "vision":
                if not models.get("transcribe_image"):
                    continue
            elif not (models.get(task) or models.get("default")):
                continue
            out.append(name)
        return out

    async def complete(
        self,
        *,
        task: str,
        messages: list[ChatMessage] | list[dict],
        json_mode: bool = True,
        user_id: UUID | None = None,
        source_id: UUID | None = None,
    ) -> LLMResult:
        normalized: list[ChatMessage] = [
            m if isinstance(m, ChatMessage) else ChatMessage.model_validate(m) for m in messages
        ]
        primary = self.provider_for(task)
        attempts: list[str] = []
        first_error: ProviderHTTPError | None = None

        for name in [primary.name, *self._fallback_candidates(task, primary.name)]:
            provider = primary if name == primary.name else self.providers[name]
            try:
                result = await provider.complete(
                    task=task, messages=normalized, json_mode=json_mode
                )
            except ProviderHTTPError as exc:
                attempts.append(exc.short)
                if first_error is None:
                    first_error = exc
                if not exc.worth_another_provider:
                    raise
                logger.warning(
                    "llm task=%s provider=%s failed (%s); trying another vendor",
                    task,
                    name,
                    exc.short,
                )
                continue
            if name != primary.name:
                # Surface the switch: the learner sees which vendor actually
                # answered, and the ingest note explains a surprise provider.
                self.fallback_note = (
                    f"{primary.name} 不可用（{first_error.short if first_error else 'failed'}），"
                    f"已回退到 {result.provider}/{result.model}"
                )
                logger.warning("llm task=%s fallback: %s", task, self.fallback_note)
            else:
                self.fallback_note = None
            logger.info(
                "llm task=%s provider=%s model=%s prompt_tokens=%s completion_tokens=%s",
                task,
                result.provider,
                result.model,
                result.prompt_tokens,
                result.completion_tokens,
            )
            await self._record(task, result, user_id=user_id, source_id=source_id)
            return result

        # Every vendor with a key refused. Surface the first (most likely root
        # cause) plus every attempt, so the task message is a diagnosis.
        assert first_error is not None
        raise ProviderHTTPError(
            status_code=first_error.status_code,
            provider=first_error.provider,
            model=first_error.model,
            endpoint=first_error.endpoint,
            vendor_code=first_error.vendor_code,
            vendor_message=first_error.vendor_message,
            body_excerpt=(first_error.body_excerpt or "")[:200],
            task=task,
        ) from RuntimeError(
            f"所有已配置密钥的供应商都调用失败（task={task}）。尝试记录： " + " || ".join(attempts)
        )

    async def embed(
        self,
        *,
        task: str = "embed",
        texts: list[str],
        user_id: UUID | None = None,
        source_id: UUID | None = None,
    ) -> EmbedResult:
        primary = self.provider_for(task)
        attempts: list[str] = []
        first_error: ProviderHTTPError | None = None

        for name in [primary.name, *self._fallback_candidates(task, primary.name)]:
            provider = primary if name == primary.name else self.providers[name]
            try:
                result = await provider.embed(task=task, texts=texts)
            except ProviderHTTPError as exc:
                attempts.append(exc.short)
                if first_error is None:
                    first_error = exc
                if not exc.worth_another_provider:
                    raise
                logger.warning(
                    "embed provider=%s failed (%s); trying another vendor", name, exc.short
                )
                continue
            if name != primary.name:
                self.fallback_note = (
                    f"{primary.name} 向量接口不可用（{first_error.short if first_error else 'failed'}），"
                    f"已回退到 {result.provider}/{result.model}"
                )
                logger.warning("embed fallback: %s", self.fallback_note)
            else:
                self.fallback_note = None
            logger.info(
                "llm task=%s provider=%s model=%s vectors=%s",
                task,
                result.provider,
                result.model,
                len(result.vectors),
            )
            await self._record(
                task,
                LLMResult(
                    content="",
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=0,
                    provider=result.provider,
                    model=result.model,
                ),
                user_id=user_id,
                source_id=source_id,
            )
            return result

        assert first_error is not None
        raise ProviderHTTPError(
            status_code=first_error.status_code,
            provider=first_error.provider,
            model=first_error.model,
            endpoint=first_error.endpoint,
            vendor_code=first_error.vendor_code,
            vendor_message=first_error.vendor_message,
            body_excerpt=(first_error.body_excerpt or "")[:200],
            task=task,
        ) from RuntimeError(
            f"所有已配置密钥的向量供应商都调用失败（task={task}）。尝试记录： " + " || ".join(attempts)
        )

    async def _record(
        self,
        task: str,
        result: LLMResult,
        *,
        user_id: UUID | None,
        source_id: UUID | None,
    ) -> None:
        if self.session is None:
            return
        self.session.add(
            TokenUsage(
                user_id=user_id,
                source_id=source_id,
                task=task,
                provider=result.provider,
                model=result.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
            )
        )
        await self.session.flush()
