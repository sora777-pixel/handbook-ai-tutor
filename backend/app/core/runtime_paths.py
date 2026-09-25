"""User-editable runtime paths, persisted inside the project.

Two locations used to be frozen at process start: where uploads are written
(`LOCAL_STORAGE_PATH`) and which `providers.yaml` governs LLM routing
(`PROVIDERS_CONFIG_PATH`). Changing either meant editing `.env` and restarting.

This module lets the settings page move them at runtime. A browser cannot
safely rewrite `.env` (it holds API keys and comments), so the overrides live in
a separate small JSON file:

    backend/config/runtime.json

Resolution order, highest first:

  1. a real process environment variable (docker compose, CI export, start.bat)
     -> never touched, the UI reports it as locked
  2. runtime.json written by the settings page
  3. .env values
  4. the pre-generated project-folder default below

Keeping (1) on top is deliberate: a container that pins LOCAL_STORAGE_PATH must
not be silently re-pointed by a click in a browser.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.core.config import BACKEND_DIR, REPO_ROOT, env_var_is_external, get_settings

logger = logging.getLogger("app.paths")

# Settings field -> environment variable that carries it.
OVERRIDABLE: dict[str, str] = {
    "local_storage_path": "LOCAL_STORAGE_PATH",
    "providers_config_path": "PROVIDERS_CONFIG_PATH",
}

#: The hand-entered Embedding API. Same store, different traffic: these values
#: are read by ModelRouter, not by pydantic-settings, and one of them is a
#: secret — so the API never echoes them back (see `describe_embed`).
EMBED_KEYS: dict[str, str] = {
    "embed_api_base": "EMBED_API_BASE",
    "embed_api_key": "EMBED_API_KEY",
    "embed_model": "EMBED_MODEL",
}

#: Everything runtime.json may hold. Used for load/save/apply filtering; the
#: paths UI separately reports only OVERRIDABLE.
ALL_OVERRIDABLE: dict[str, str] = {**OVERRIDABLE, **EMBED_KEYS}

#: Human-facing metadata, kept next to the routing table so the API, the tests
#: and the UI agree on what each entry means.
PATHS_META: dict[str, dict[str, str]] = {
    "local_storage_path": {
        "label": "用户数据根目录",
        "kind": "directory",
        "hint": (
            "每位用户在此目录下拥有一个以用户 ID 命名的专属文件夹，内含 "
            "profile（账号元数据）、records（学习记录存档）、resources（导入的学习资源原件）三层。"
            "改到新目录后，已有用户的旧文件夹不会被搬移。"
        ),
    },
    "providers_config_path": {
        "label": "LLM 供应商配置文件",
        "kind": "file",
        "hint": "providers.yaml 决定每个任务调用哪个供应商与模型。必须是一个能解析出 providers 段的 YAML 文件。",
    },
}

EMBED_META: dict[str, dict[str, str]] = {
    "embed_api_base": {
        "label": "Embedding API 地址",
        "placeholder": "https://api.openai.com/v1",
        "hint": "OpenAI 兼容的 /embeddings 前缀地址，末尾的 /v1 要保留。",
    },
    "embed_api_key": {
        "label": "Embedding API 密钥",
        "placeholder": "sk-...",
        "hint": "只保存在本机的 backend/config/runtime.json，页面不会回显；本地无鉴权服务可以留空。",
    },
    "embed_model": {
        "label": "Embedding 模型",
        "placeholder": "text-embedding-3-small",
        "hint": "必须是该地址支持的向量模型名。",
    },
}

OVERRIDES_FILE = BACKEND_DIR / "config" / "runtime.json"


# ---------------------------------------------------------------- defaults


def project_defaults() -> dict[str, str]:
    """The pre-generated, absolute, inside-the-project locations.

    Always computable without reading any config file, so the app is usable out
    of the box and the UI has something concrete to pre-fill.
    """
    return {
        "local_storage_path": str((REPO_ROOT / "data" / "storage").resolve()),
        "providers_config_path": str((BACKEND_DIR / "config" / "providers.yaml").resolve()),
    }


# ---------------------------------------------------------------- overrides


def load_overrides() -> dict[str, str]:
    """Read runtime.json. Never raises: a corrupt file must not stop boot."""
    if not OVERRIDES_FILE.exists():
        return {}
    try:
        with OVERRIDES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable %s: %s", OVERRIDES_FILE, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("ignoring %s: expected a JSON object", OVERRIDES_FILE)
        return {}
    return {
        key: str(value)
        for key, value in data.items()
        if key in ALL_OVERRIDABLE and isinstance(value, str) and value.strip()
    }


def save_overrides(overrides: dict[str, str]) -> None:
    """Write runtime.json atomically so a crash mid-write cannot corrupt it."""
    payload = {
        key: str(value).strip()
        for key, value in overrides.items()
        if key in ALL_OVERRIDABLE and str(value).strip()
    }
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, OVERRIDES_FILE)


def apply_overrides() -> dict[str, str]:
    """Push runtime.json into os.environ and drop the settings cache.

    Returns the overrides that were actually applied (externally pinned entries
    are skipped). Safe to call on every boot and after every save.
    """
    overrides = load_overrides()
    applied: dict[str, str] = {}
    for field, env_name in ALL_OVERRIDABLE.items():
        if env_var_is_external(env_name):
            logger.debug("%s is pinned by the process environment; override skipped", env_name)
            continue
        value = overrides.get(field)
        if value:
            os.environ[env_name] = value
            applied[field] = value
        else:
            # Drop a stale override so .env / the code default takes over again.
            os.environ.pop(env_name, None)
    get_settings.cache_clear()
    return applied


# ---------------------------------------------------------------- inspection


def _dir_stats(path: Path) -> dict[str, Any]:
    """Cheap size accounting, for "am I about to orphan my uploads?" decisions."""
    files = 0
    total = 0
    if path.is_dir():
        try:
            for child in path.rglob("*"):
                if child.is_file():
                    files += 1
                    try:
                        total += child.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return {"files": files, "bytes": total}


def _yaml_summary(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise ValueError("YAML 里没有 providers 段，不能作为供应商配置使用")
    return {
        "providers": sorted(config["providers"]),
        "default_provider": config.get("default_provider"),
    }


def describe_paths() -> dict[str, Any]:
    """Everything the settings page needs to render the two path fields."""
    settings = get_settings()
    defaults = project_defaults()
    overrides = load_overrides()
    current: dict[str, Any] = {}
    for field, env_name in OVERRIDABLE.items():
        value = str(getattr(settings, field) or "")
        pinned = env_var_is_external(env_name)
        if pinned:
            source = "env"
        elif overrides.get(field):
            source = "custom"
        else:
            source = "default"

        path = Path(value)
        entry: dict[str, Any] = {
            **PATHS_META[field],
            "key": field,
            "env_var": env_name,
            "current": value,
            "default": defaults[field],
            "custom_value": overrides.get(field),
            "source": source,
            "locked_by_env": pinned,
            "exists": path.is_file() if PATHS_META[field]["kind"] == "file" else path.is_dir(),
            "note": None,
            "detail": {},
        }
        if field == "local_storage_path" and settings.storage_backend != "local":
            # Don't let the page imply a local folder matters when uploads actually
            # go to object storage.
            entry["note"] = (
                f"当前 STORAGE_BACKEND={settings.storage_backend}，上传的文件进的是对象存储，"
                "这个本地目录不会被使用；切回 local 后才会生效。"
            )
        try:
            if PATHS_META[field]["kind"] == "directory":
                entry["detail"] = _dir_stats(path) if path.is_dir() else {"files": 0, "bytes": 0}
            elif path.is_file():
                entry["detail"] = _yaml_summary(path)
        except Exception as exc:  # diagnostics only — never fail the whole page
            entry["detail"] = {"error": f"{type(exc).__name__}: {exc}"}

        current[field] = entry
    return {
        "paths": list(current.values()),
        "overrides_file": str(OVERRIDES_FILE),
        # Paths only. runtime.json also holds the Embedding API key, and this
        # endpoint's response ends up in the browser.
        "overrides": {k: v for k, v in overrides.items() if k in OVERRIDABLE},
        "overrides_applied": {
            field: str(getattr(settings, field) or "") for field in OVERRIDABLE
        },
    }


def _mask(secret: str) -> str:
    """Enough of a key to recognise it, never enough to use it."""
    if len(secret) <= 4:
        return "已设置"
    return f"{secret[:3]}…{secret[-4:]}"


def describe_embed() -> dict[str, Any]:
    """State of the hand-entered Embedding API, with the key masked.

    Reports which provider embeddings will actually go through, so "留空" is
    never ambiguous: the card can say "现在用本地 mock 向量" before the user
    wonders why similarity search looks odd.
    """
    from app.services.llm.router import ModelRouter

    overrides = load_overrides()
    key = overrides.get("embed_api_key", "")
    base = overrides.get("embed_api_base", "")
    model = overrides.get("embed_model", "")
    active = bool(base and model)

    effective_provider = "mock"
    effective_model = ""
    note: str | None = None
    try:
        router = ModelRouter()
        provider = router.provider_for("embed")
        effective_provider = provider.name
        effective_model = provider.model_for("embed") or ""
        note = router.embed_note
    except Exception as exc:  # diagnostics only
        note = f"{type(exc).__name__}: {exc}"

    if not active:
        routed_error = bool(note and ("LLMConfigError" in note or "embeddings" in note.lower()))
        if routed_error:
            pass
        elif effective_provider == "mock":
            note = (
                "未填写 Embedding API。当前是离线 mock 向量（CI / LLM_DEFAULT_PROVIDER=mock）。"
                "接入真实对话模型后，必须再配置 siliconflow（BAAI/bge-m3）或 dashscope"
                "（text-embedding-v3），否则解析会失败而不是 silently 用假课文。"
            )
        else:
            note = (
                "未填写 Embedding API，向量化走已配置的供应商"
                f"（{effective_provider} / {effective_model or '—'}）。"
                " DeepSeek 不会被用于 /embeddings。"
            )
    elif effective_provider != "embed_override":
        note = note or "Embedding API 已保存，但当前没有被选用，请检查下方的路由表。"

    return {
        "fields": [
            {
                **EMBED_META["embed_api_base"],
                "key": "embed_api_base",
                "env_var": EMBED_KEYS["embed_api_base"],
                "value": base,
                "locked_by_env": env_var_is_external(EMBED_KEYS["embed_api_base"]),
            },
            {
                **EMBED_META["embed_api_key"],
                "key": "embed_api_key",
                "env_var": EMBED_KEYS["embed_api_key"],
                "value": "",  # never echo a secret back
                "hint_value": _mask(key) if key else None,
                "locked_by_env": env_var_is_external(EMBED_KEYS["embed_api_key"]),
            },
            {
                **EMBED_META["embed_model"],
                "key": "embed_model",
                "env_var": EMBED_KEYS["embed_model"],
                "value": model,
                "locked_by_env": env_var_is_external(EMBED_KEYS["embed_model"]),
            },
        ],
        "configured": active,
        "active": active,
        "effective_provider": effective_provider,
        "effective_model": effective_model,
        "note": note,
        "overrides_file": str(OVERRIDES_FILE),
    }
