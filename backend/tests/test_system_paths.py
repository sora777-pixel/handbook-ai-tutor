"""Deterministic coverage of the user-editable runtime paths.

Backs the "存储与配置文件位置" card on /settings/llm. Two things must hold:

  * out of the box both locations are the pre-generated absolute paths inside the
    project folder, so nothing has to be configured by hand;
  * a value pinned by the real process environment (docker compose, start.bat)
    always wins over anything written from a browser, and the UI is told so
    instead of pretending the change took effect.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from httpx import AsyncClient

from app.core import config as config_module
from app.core import runtime_paths
from app.core.config import BACKEND_DIR, REPO_ROOT, get_settings
from app.services.storage import get_storage, reset_storage
from tests.conftest import auth_header

#: Derived from the code's own source of truth instead of duplicated here, so
#: moving a project default (e.g. the storage root) cannot silently desync the
#: tests from what the app actually does.
DEFAULTS = {
    key: str(Path(value).resolve()) for key, value in runtime_paths.project_defaults().items()
}


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    """Keep runtime.json in tmp, and make the project defaults genuinely apply.

    conftest pins LOCAL_STORAGE_PATH / PROVIDERS_CONFIG_PATH into os.environ, which
    is exactly the "launcher pinned it" case. Lifting only the lock is not enough:
    pydantic-settings still reads the environment, so the value has to go too.
    Tests that care about the pinned behaviour re-set it explicitly.
    """
    snapshot = dict(os.environ)
    monkeypatch.setattr(runtime_paths, "OVERRIDES_FILE", tmp_path / "runtime.json")
    monkeypatch.setattr(config_module, "_EXTERNAL_ENV_KEYS", frozenset())
    monkeypatch.delenv("LOCAL_STORAGE_PATH", raising=False)
    monkeypatch.delenv("PROVIDERS_CONFIG_PATH", raising=False)
    get_settings.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    get_settings.cache_clear()
    reset_storage()


# --------------------------------------------------------------- the defaults


def test_project_defaults_are_absolute_and_inside_the_project():
    defaults = runtime_paths.project_defaults()
    assert set(defaults) == set(runtime_paths.OVERRIDABLE)
    for field, value in defaults.items():
        path = Path(value)
        assert path.is_absolute(), field
        assert path == Path(DEFAULTS[field]), field
    # The storage root must not be the repo itself — uploads belong in a subdir.
    assert Path(defaults["local_storage_path"]).parent == REPO_ROOT / "data"


def test_nothing_overridden_means_the_project_defaults_apply():
    runtime_paths.save_overrides({})
    runtime_paths.apply_overrides()

    settings = get_settings()
    assert settings.local_storage_path == DEFAULTS["local_storage_path"]
    assert settings.providers_config_path == DEFAULTS["providers_config_path"]

    by_key = {p["key"]: p for p in runtime_paths.describe_paths()["paths"]}
    for key, entry in by_key.items():
        assert entry["source"] == "default", key
        assert entry["locked_by_env"] is False


def test_relative_storage_path_is_anchored_to_the_project_root(monkeypatch):
    """A Docker-style `/data/storage` must not become a cwd-relative folder."""
    monkeypatch.setenv("LOCAL_STORAGE_PATH", "/data/storage")
    get_settings.cache_clear()
    resolved = Path(get_settings().local_storage_path)
    assert resolved.is_absolute()
    assert resolved == REPO_ROOT / "data" / "storage"


# --------------------------------------------------------------- the overrides


def test_override_is_persisted_and_applied_immediately(tmp_path):
    custom = tmp_path / "moved-storage"
    custom.mkdir()

    runtime_paths.save_overrides({"local_storage_path": str(custom)})
    runtime_paths.apply_overrides()

    assert get_settings().local_storage_path == str(custom)
    assert json.loads(runtime_paths.OVERRIDES_FILE.read_text())["local_storage_path"] == str(custom)

    entry = next(
        p for p in runtime_paths.describe_paths()["paths"] if p["key"] == "local_storage_path"
    )
    assert entry["source"] == "custom"
    assert entry["custom_value"] == str(custom)
    assert entry["default"] == DEFAULTS["local_storage_path"]


def test_removing_an_override_restores_the_project_default(tmp_path):
    moved = str(tmp_path / "elsewhere")
    runtime_paths.save_overrides({"local_storage_path": moved})
    runtime_paths.apply_overrides()
    assert get_settings().local_storage_path == moved

    runtime_paths.save_overrides({})
    runtime_paths.apply_overrides()
    assert get_settings().local_storage_path == DEFAULTS["local_storage_path"]
    assert os.environ.get("LOCAL_STORAGE_PATH", DEFAULTS["local_storage_path"]) == DEFAULTS[
        "local_storage_path"
    ]


def test_process_environment_wins_over_runtime_json(monkeypatch):
    """A pinned value must survive a UI write, and be reported as pinned."""
    pinned = str(DEFAULTS["local_storage_path"])
    monkeypatch.setenv("LOCAL_STORAGE_PATH", pinned)
    monkeypatch.setattr(config_module, "_EXTERNAL_ENV_KEYS", frozenset({"LOCAL_STORAGE_PATH"}))
    get_settings.cache_clear()

    runtime_paths.save_overrides({"local_storage_path": "D:/should/be/ignored"})
    applied = runtime_paths.apply_overrides()

    assert "local_storage_path" not in applied
    assert get_settings().local_storage_path == pinned
    entry = next(
        p for p in runtime_paths.describe_paths()["paths"] if p["key"] == "local_storage_path"
    )
    assert entry["source"] == "env"
    assert entry["locked_by_env"] is True


def test_corrupt_overrides_file_is_ignored_not_fatal():
    runtime_paths.OVERRIDES_FILE.write_text("{ this is not json", encoding="utf-8")
    assert runtime_paths.load_overrides() == {}
    runtime_paths.apply_overrides()  # must not raise
    assert get_settings().local_storage_path == DEFAULTS["local_storage_path"]


def test_overrides_file_only_accepts_known_keys():
    runtime_paths.save_overrides({"database_url": "sqlite:///evil.db", "local_storage_path": "D:/x"})
    stored = json.loads(runtime_paths.OVERRIDES_FILE.read_text())
    assert stored == {"local_storage_path": "D:/x"}


# -------------------------------------------------------------------- the api


async def test_get_paths_reports_both_entries(client: AsyncClient):
    h = await auth_header(client)
    r = await client.get("/api/v1/system/paths", headers=h)
    assert r.status_code == 200, r.text

    body = r.json()
    by_key = {p["key"]: p for p in body["paths"]}
    assert set(by_key) == {"local_storage_path", "providers_config_path"}
    for key, entry in by_key.items():
        assert entry["default"] == DEFAULTS[key]
        assert entry["current"] == DEFAULTS[key]
        assert entry["source"] == "default"
        assert entry["locked_by_env"] is False
        assert entry["kind"] in {"directory", "file"}
    assert body["overrides"] == {}
    assert body["restart_required"] is False
    # The YAML is parsed so the page can show what it found.
    assert by_key["providers_config_path"]["detail"]["providers"]


async def test_put_requires_auth(client: AsyncClient):
    r = await client.put("/api/v1/system/paths", json={"local_storage_path": "D:/x"})
    assert r.status_code in {401, 403}


async def test_put_rejects_relative_path(client: AsyncClient, tmp_path):
    h = await auth_header(client)
    r = await client.put(
        "/api/v1/system/paths", headers=h, json={"local_storage_path": "relative/dir"}
    )
    assert r.status_code == 400
    assert "绝对路径" in r.json()["detail"]


async def test_put_rejects_the_repo_directory(client: AsyncClient):
    h = await auth_header(client)
    for target in (str(REPO_ROOT), str(BACKEND_DIR), str(REPO_ROOT.parent)):
        r = await client.put(
            "/api/v1/system/paths", headers=h, json={"local_storage_path": target}
        )
        assert r.status_code == 400, target
        assert "项目" in r.json()["detail"]


async def test_put_rejects_a_providers_config_that_is_not_usable(client: AsyncClient, tmp_path):
    h = await auth_header(client)

    missing = await client.put(
        "/api/v1/system/paths", headers=h, json={"providers_config_path": str(tmp_path / "nope.yaml")}
    )
    assert missing.status_code == 400
    assert "不是" in missing.json()["detail"]

    bad = tmp_path / "bad.yaml"
    bad.write_text("just: a string\n", encoding="utf-8")
    r = await client.put(
        "/api/v1/system/paths", headers=h, json={"providers_config_path": str(bad)}
    )
    assert r.status_code == 400
    assert "providers" in r.json()["detail"]


async def test_put_rejects_unknown_fields(client: AsyncClient):
    h = await auth_header(client)
    r = await client.put("/api/v1/system/paths", headers=h, json={"database_url": "x"})
    assert r.status_code == 400
    assert "database_url" in r.json()["detail"]


async def test_put_switches_storage_root_and_hot_swaps_the_backend(client: AsyncClient, tmp_path):
    h = await auth_header(client)
    custom = tmp_path / "elsewhere" / "storage"

    r = await client.put(
        "/api/v1/system/paths", headers=h, json={"local_storage_path": str(custom)}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == ["local_storage_path"]
    assert body["restart_required"] is False
    assert custom.is_dir(), "the target directory should have been created"

    entry = next(p for p in body["paths"] if p["key"] == "local_storage_path")
    assert entry["source"] == "custom"
    assert entry["current"] == str(custom.resolve())

    # No restart: the live storage backend already points at the new root.
    assert get_storage().root == custom.resolve()
    assert get_settings().local_storage_path == str(custom.resolve())


async def test_put_warns_that_existing_files_are_not_moved(client: AsyncClient, tmp_path):
    h = await auth_header(client)

    # Seed something in the *default* root, then move the root away from it.
    default_root = Path(get_settings().local_storage_path)
    default_root.mkdir(parents=True, exist_ok=True)
    (default_root / "already-here.txt").write_text("x", encoding="utf-8")

    r = await client.put(
        "/api/v1/system/paths",
        headers=h,
        json={"local_storage_path": str(tmp_path / "fresh-root")},
    )
    assert r.status_code == 200
    warnings = " ".join(r.json()["warnings"])
    assert "不会被搬移" in warnings
    # The old file is left exactly where it was.
    assert (default_root / "already-here.txt").exists()


async def test_put_empty_string_restores_the_default(client: AsyncClient, tmp_path):
    h = await auth_header(client)

    moved = await client.put(
        "/api/v1/system/paths", headers=h, json={"local_storage_path": str(tmp_path / "custom")}
    )
    assert moved.status_code == 200
    assert moved.json()["overrides"], "an override should have been stored"

    back = await client.put("/api/v1/system/paths", headers=h, json={"local_storage_path": ""})
    assert back.status_code == 200
    assert back.json()["overrides"] == {}
    entry = next(p for p in back.json()["paths"] if p["key"] == "local_storage_path")
    assert entry["source"] == "default"
    assert entry["current"] == DEFAULTS["local_storage_path"]


async def test_put_accepts_an_alternative_providers_config(client: AsyncClient, tmp_path):
    h = await auth_header(client)
    copy = tmp_path / "custom-providers.yaml"
    copy.write_text(
        yaml.safe_dump(
            {
                "default_provider": "mock",
                "task_routes": {},
                "providers": {"mock": {"type": "mock", "models": {"default": "mock-llm"}}},
            }
        ),
        encoding="utf-8",
    )

    r = await client.put(
        "/api/v1/system/paths", headers=h, json={"providers_config_path": str(copy)}
    )
    assert r.status_code == 200, r.text
    entry = next(p for p in r.json()["paths"] if p["key"] == "providers_config_path")
    assert entry["source"] == "custom"
    assert get_settings().providers_config_path == str(copy.resolve())
    # Routing really reads the new file: only mock is defined there now.
    from app.services.llm.router import ModelRouter

    assert set(ModelRouter().providers) == {"mock"}


async def test_put_refuses_when_the_launcher_pinned_the_path(
    client: AsyncClient, monkeypatch, tmp_path
):
    """docker compose / start.bat must stay authoritative over a browser click."""
    h = await auth_header(client)
    monkeypatch.setattr(config_module, "_EXTERNAL_ENV_KEYS", frozenset({"LOCAL_STORAGE_PATH"}))

    r = await client.put(
        "/api/v1/system/paths", headers=h, json={"local_storage_path": str(tmp_path / "nope")}
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "LOCAL_STORAGE_PATH" in detail
    assert "重启" in detail
    assert not (tmp_path / "nope").exists()


async def test_put_with_no_fields_is_a_400(client: AsyncClient):
    h = await auth_header(client)
    r = await client.put("/api/v1/system/paths", headers=h, json={})
    assert r.status_code == 400


async def test_reapplying_the_same_value_is_an_honest_no_op(client: AsyncClient):
    """Pressing 应用 without editing must not claim the location "switched"."""
    h = await auth_header(client)

    first = await client.put(
        "/api/v1/system/paths", headers=h, json={"local_storage_path": DEFAULTS["local_storage_path"]}
    )
    assert first.status_code == 200
    assert "未变化" in " ".join(first.json()["warnings"])
    assert "已切换" not in " ".join(first.json()["warnings"])

    again = await client.put(
        "/api/v1/system/paths", headers=h, json={"local_storage_path": DEFAULTS["local_storage_path"]}
    )
    assert again.status_code == 200
    assert "未变化" in " ".join(again.json()["warnings"])
    # Asking for the default explicitly must not leave a redundant override behind.
    assert again.json()["overrides"] == {}


async def test_local_dir_is_flagged_when_object_storage_is_in_use(
    client: AsyncClient, monkeypatch
):
    """With STORAGE_BACKEND=minio the local folder does nothing — say so."""
    monkeypatch.setenv("STORAGE_BACKEND", "minio")
    get_settings.cache_clear()

    h = await auth_header(client)
    r = await client.get("/api/v1/system/paths", headers=h)
    assert r.status_code == 200

    storage = next(p for p in r.json()["paths"] if p["key"] == "local_storage_path")
    assert storage["note"] and "STORAGE_BACKEND=minio" in storage["note"]
    # The providers config is unaffected by the storage backend.
    providers = next(p for p in r.json()["paths"] if p["key"] == "providers_config_path")
    assert providers["note"] is None
