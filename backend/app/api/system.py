"""Runtime path management — where uploads go, which providers.yaml is used.

Read-only visibility lives in /health; this router is the write side that backs
the "路径设置" card on /settings/llm. Every change is validated, persisted to
`backend/config/runtime.json`, and applied to the running process immediately
(no restart), because both settings are read through `get_settings()` per call.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import BACKEND_DIR, REPO_ROOT, env_var_is_external, get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.runtime_paths import (
    EMBED_KEYS,
    EMBED_META,
    OVERRIDABLE,
    PATHS_META,
    apply_overrides,
    describe_embed,
    describe_paths,
    load_overrides,
    project_defaults,
    save_overrides,
)
from app.models.user import User
from app.services.storage import reset_storage
from app.services.workspace import export_records, workspace_report

logger = logging.getLogger("app.paths")

router = APIRouter(prefix="/api/v1/system", tags=["system"])


class PathsIn(BaseModel):
    """Only the keys present are touched; null / "" restores the default.

    `extra="allow"` so an unexpected key produces our own explanatory 400 instead
    of a bare pydantic 422.
    """

    model_config = ConfigDict(extra="allow")

    local_storage_path: str | None = None
    providers_config_path: str | None = None


class PathsOut(BaseModel):
    paths: list[dict[str, Any]]
    overrides_file: str
    overrides: dict[str, str]
    overrides_applied: dict[str, str]
    changed: list[str] = []
    warnings: list[str] = []
    restart_required: bool = False


def _reject(message: str, *, field: str) -> HTTPException:
    return HTTPException(status_code=400, detail=f"{PATHS_META[field]['label']}：{message}")


def _check_common(path: Path, field: str) -> None:
    if not path.is_absolute():
        raise _reject("请填写绝对路径（例如 E:\\tutor-data\\storage）。", field=field)
    if path == path.anchor or path.parent == path:
        raise _reject("不能把磁盘根目录当作存储位置。", field=field)
    if path in {REPO_ROOT, BACKEND_DIR} or path in REPO_ROOT.parents:
        raise _reject(
            "该路径会覆盖项目自身目录，请选一个项目文件夹之外的独立目录。", field=field
        )


def _validate_directory(raw: str, field: str) -> str:
    """Create the directory and prove it is writable, or explain why not.

    Every filesystem call is wrapped: a mapped-but-offline drive, a permission
    wall or a path the OS refuses to stat must surface as a readable 400, not as
    an unhandled OSError that the browser renders as "Internal Server Error".
    """
    path = Path(raw).expanduser()
    _check_common(path, field)
    try:
        if path.exists() and not path.is_dir():
            raise _reject(f"{path} 已经是一个文件，不能作为目录使用。", field=field)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        resolved = str(path.resolve())
    except HTTPException:
        raise
    except OSError as exc:
        raise _reject(f"目录 {path} 不可用（{exc.strerror or exc}）。", field=field) from exc
    except Exception as exc:
        raise _reject(f"目录 {path} 校验失败（{type(exc).__name__}: {exc}）。", field=field) from exc
    return resolved


def _validate_providers_config(raw: str, field: str) -> str:
    path = Path(raw).expanduser()
    _check_common(path, field)
    try:
        is_file = path.is_file()
    except OSError as exc:
        raise _reject(f"无法访问 {path}（{exc.strerror or exc}）。", field=field) from exc
    if not is_file:
        raise _reject(f"{path} 不是一个存在的文件。", field=field)
    try:
        with path.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except OSError as exc:
        raise _reject(f"无法读取 {path}（{exc.strerror or exc}）。", field=field) from exc
    except Exception as exc:
        raise _reject(f"无法解析 YAML（{type(exc).__name__}: {exc}）。", field=field) from exc
    if not isinstance(config, dict) or not isinstance(config.get("providers"), dict):
        raise _reject("文件里没有 providers 段，不能作为供应商配置使用。", field=field)
    if "mock" not in config["providers"]:
        raise _reject("文件里必须至少定义 mock 供应商作为兜底。", field=field)
    try:
        return str(path.resolve())
    except OSError as exc:
        raise _reject(f"无法解析 {path}（{exc.strerror or exc}）。", field=field) from exc


_VALIDATORS: dict[str, Callable[[str, str], str]] = {
    "local_storage_path": _validate_directory,
    "providers_config_path": _validate_providers_config,
}


def _stored_names(root: Path) -> set[str]:
    """Relative names of everything already stored, for orphaning warnings."""
    if not root.is_dir():
        return set()
    try:
        return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
    except OSError:
        return set()


@router.get("/paths", response_model=PathsOut, response_model_exclude_none=True)
async def get_paths(user: User = Depends(get_current_user)) -> PathsOut:
    """Current vs project-default location for each configurable path."""
    return PathsOut(**describe_paths())


@router.put("/paths", response_model=PathsOut, response_model_exclude_none=True)
async def update_paths(
    payload: PathsIn, user: User = Depends(get_current_user)
) -> PathsOut:
    """Validate, persist and hot-apply new locations.

    `null` (or an empty string) means "go back to the project default". Omitting
    a field leaves it alone.
    """
    submitted = payload.model_dump(exclude_unset=True)
    if not submitted:
        raise HTTPException(status_code=400, detail="没有需要修改的路径。")

    unknown = [k for k in submitted if k not in OVERRIDABLE]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的路径字段：{', '.join(unknown)}")

    defaults = {k: str(Path(v).resolve()) for k, v in project_defaults().items()}
    settings_before = get_settings()
    before_effective = {k: str(getattr(settings_before, k) or "") for k in OVERRIDABLE}
    before_root = Path(before_effective["local_storage_path"])
    before_names = _stored_names(before_root) if "local_storage_path" in submitted else set()

    changed: list[str] = []
    pinned: list[str] = []
    overrides = load_overrides()

    for field, value in submitted.items():
        env_name = OVERRIDABLE[field]
        if env_var_is_external(env_name):
            # A value pinned by the process environment is not a reason to throw
            # the *whole* request away. The settings page submits both paths
            # together, so answering 409 here meant a pinned LOCAL_STORAGE_PATH
            # (which is what every launcher script that exports it does) also
            # blocked a perfectly valid providers.yaml change — the reported
            # "改了配置文件和存储地址就报错，改动没生效".
            pinned.append(field)
            continue
        raw = (value or "").strip()
        if not raw:
            overrides.pop(field, None)  # back to the project default
        else:
            resolved = _VALIDATORS[field](raw, field)
            if resolved == defaults[field]:
                overrides.pop(field, None)  # same as default -> don't store noise
            else:
                overrides[field] = resolved
        changed.append(field)

    if not changed:
        # Every submitted field is pinned, so there is genuinely nothing to do.
        # Say exactly which variable holds it and what to edit.
        names = "、".join(f"{PATHS_META[f]['label']}（{OVERRIDABLE[f]}）" for f in pinned)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{names} 由启动时的进程环境变量固定，页面改动不会生效。"
                "请修改启动脚本 / docker compose / .env 后重启后端。"
            ),
        )

    save_overrides(overrides)
    applied = apply_overrides()
    settings = get_settings()

    warnings: list[str] = []
    for field in pinned:
        warnings.append(
            f"{PATHS_META[field]['label']}（{OVERRIDABLE[field]}）由启动时的进程环境变量固定，"
            "本次没有修改；其余字段已正常保存并生效。如需改它，请修改启动脚本或 docker compose 后重启。"
        )
    # Only talk about a path that actually moved. Re-applying the same value is a
    # legitimate no-op, and claiming "switched" would be misleading.
    moved = {f for f in changed if str(getattr(settings, f) or "") != before_effective[f]}

    if "local_storage_path" in moved:
        # LocalStorage caches its root, so swap the instance out.
        reset_storage()
        new_root = Path(str(settings.local_storage_path)).resolve()
        if before_names and not _stored_names(new_root):
            warnings.append(
                f"新目录 {new_root} 目前是空的，而原目录 {before_root} 里还有 "
                f"{len(before_names)} 个文件。这些历史文件不会被搬移，"
                "对应条目的「原文件」将无法打开 —— 需要的话请手动复制过去。"
            )
        warnings.append("存储目录已切换：新上传写入新目录，已有文件保持原位。")
    elif "local_storage_path" in changed:
        warnings.append(f"存储目录未变化（仍为 {settings.local_storage_path}）。")

    if "providers_config_path" in moved:
        warnings.append(
            f"LLM 路由配置已切换到 {settings.providers_config_path}，下一个请求即生效（无需重启）。"
        )
    elif "providers_config_path" in changed:
        warnings.append(f"LLM 路由配置未变化（仍为 {settings.providers_config_path}）。")

    result = describe_paths()
    result.update(
        {
            "changed": sorted(set(changed)),
            "warnings": warnings,
            "restart_required": False,
        }
    )
    logger.info(
        "runtime paths updated by user=%s changed=%s applied=%s",
        user.id,
        sorted(set(changed)),
        applied,
    )
    return PathsOut(**result)


# --------------------------------------------------------------- embedding


class EmbedIn(BaseModel):
    """Fields present are touched; "" clears the override and restores auto mode."""

    model_config = ConfigDict(extra="allow")

    embed_api_base: str | None = None
    embed_api_key: str | None = None
    embed_model: str | None = None


class EmbedOut(BaseModel):
    fields: list[dict[str, Any]]
    configured: bool
    active: bool
    effective_provider: str
    effective_model: str = ""
    note: str | None = None
    overrides_file: str
    changed: list[str] = []
    warnings: list[str] = []
    restart_required: bool = False


class EmbedTestOut(BaseModel):
    ok: bool
    provider: str | None = None
    model: str | None = None
    dim: int = 0
    note: str | None = None
    error: str | None = None


@router.get("/embed", response_model=EmbedOut)
async def get_embed(user: User = Depends(get_current_user)) -> EmbedOut:
    """The Embedding API fields plus which provider is serving vectors right now."""
    return EmbedOut(**describe_embed())


@router.put("/embed", response_model=EmbedOut)
async def update_embed(payload: EmbedIn, user: User = Depends(get_current_user)) -> EmbedOut:
    """Validate, persist and hot-apply the Embedding API.

    Blank means "do not use a custom endpoint": the field is dropped and
    embedding falls back through the normal chain, which ends at the local mock
    vectors — so the app stays usable without any embedding key at all.
    """
    submitted = payload.model_dump(exclude_unset=True)
    if not submitted:
        raise HTTPException(status_code=400, detail="没有需要修改的 Embedding 配置。")

    unknown = [k for k in submitted if k not in EMBED_KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的字段：{', '.join(sorted(unknown))}")

    overrides = load_overrides()
    pinned: list[str] = []
    touched: list[str] = []
    for field, value in submitted.items():
        env_name = EMBED_KEYS[field]
        if env_var_is_external(env_name):
            # Skipped, not fatal: one pinned variable must not stop the other
            # two (and the rest of the page) from being saved.
            pinned.append(field)
            continue
        raw = (value or "").strip()
        if raw:
            overrides[field] = raw
        else:
            overrides.pop(field, None)
        touched.append(field)

    if not touched:
        names = "、".join(f"{EMBED_META[f]['label']}（{EMBED_KEYS[f]}）" for f in pinned)
        raise HTTPException(
            status_code=409,
            detail=(
                f"{names} 由启动时的进程环境变量固定，页面改动不会生效。"
                "请修改启动脚本或 .env 后重启后端。"
            ),
        )

    def _effective(field: str) -> str:
        """The value that will be in force once this request is applied.

        A pinned variable can legitimately supply one half of the pair, so the
        check below reads it from the process environment instead of assuming
        runtime.json is the only source.
        """
        env_name = EMBED_KEYS[field]
        if env_var_is_external(env_name):
            return (os.environ.get(env_name) or "").strip()
        return (overrides.get(field) or "").strip()

    base = _effective("embed_api_base")
    model = _effective("embed_model")
    if bool(base) != bool(model):
        raise HTTPException(
            status_code=400,
            detail="Embedding API 地址与模型名需要同时填写；两者都留空即走自动回退。",
        )
    if base and not base.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="Embedding API 地址需要以 http:// 或 https:// 开头，例如 https://api.openai.com/v1",
        )

    save_overrides(overrides)
    applied = apply_overrides()

    warnings: list[str] = []
    for field in pinned:
        warnings.append(
            f"{EMBED_META[field]['label']}（{EMBED_KEYS[field]}）由启动时的进程环境变量固定，"
            "本次没有修改；其余字段已保存并生效。"
        )
    if not (base and model):
        warnings.append("已清空自定义 Embedding API，向量化回到自动回退（最终兜底为本地 mock 向量）。")
    elif not (overrides.get("embed_api_key") or os.environ.get(EMBED_KEYS["embed_api_key"])):
        warnings.append(
            "未填写密钥：本地无鉴权服务可以正常使用；调用云端服务时会在供应商侧报鉴权失败。"
        )

    logger.info(
        "embedding api updated by user=%s set=%s applied=%s",
        user.id,
        sorted(submitted),
        sorted(applied),
    )
    result = describe_embed()
    return EmbedOut(
        **result,
        changed=sorted(submitted),
        warnings=warnings,
        restart_required=False,
    )


@router.post("/embed/test", response_model=EmbedTestOut)
async def test_embed(user: User = Depends(get_current_user)) -> EmbedTestOut:
    """Embed one short string and report the vector width.

    A wrong model name or a bad key is only visible at call time, so the card
    offers the same "prove it works" button as the chat routes.
    """
    from app.services.llm.router import ModelRouter

    router = ModelRouter()
    try:
        provider = router.provider_for("embed")
        result = await router.embed(
            texts=["embedding connectivity test"],
            user_id=user.id,
            source_id=None,
        )
    except Exception as exc:
        return EmbedTestOut(ok=False, error=f"{type(exc).__name__}: {exc}")

    return EmbedTestOut(
        ok=True,
        provider=result.provider,
        model=result.model,
        dim=result.dim or (len(result.vectors[0]) if result.vectors else 0),
        note=router.embed_note,
    )


# ---------------------------------------------------------------- workspace


@router.get("/workspace")
async def get_workspace(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Where the caller's own folder is, and what it currently holds.

    Read-only: the three sub-folders are created on registration and refreshed
    on every login, so this is a diagnostic rather than a control.
    """
    return workspace_report(user.id)


@router.post("/workspace/export")
async def export_workspace(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Refresh this account's ``records/`` from the database, on demand.

    Quiz grading already archives automatically; this is the explicit "write my
    current records out now" button, and it reports both the row counts that
    were mirrored and the resulting folder shape.
    """
    counts = await export_records(db, user.id)
    return {"exported": counts, "workspace": workspace_report(user.id)}
