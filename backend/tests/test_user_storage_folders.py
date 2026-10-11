"""Username-named storage folders, and nothing lost on the way there.

The requirement: after logging in, a user's on-disk cache is rooted at a folder
named after the username (not a UUID), and every upload that predates that
decision still opens. What is pinned here:

* folder naming is filesystem-safe (no traversal, no reserved device names);
* an account with a UUID folder is migrated on provisioning, and the database
  keys are rewritten to match the new location;
* reads fall back across all three spellings (recorded / username / uuid), so a
  file is found whichever scheme its row was written under.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.workspace import (
    MAX_OBJECT_STEM,
    legacy_resource_key,
    safe_object_name,
    migrate_legacy_folder,
    read_source_bytes,
    resource_key,
    storage_candidates,
    user_folder_name,
    workspace_dir,
)


class _FakeSource:
    def __init__(self, source_id, filename, key):
        self.id = source_id
        self.filename = filename
        self.storage_key = key
        self.user_id = None


# ------------------------------------------------------------------- naming


@pytest.mark.parametrize(
    "email,expected",
    [
        ("ada@example.com", "ada@example.com"),
        ("Zoe.Q@Example.COM", "Zoe.Q@Example.COM"),
        ("a/b\\c:d*e?f", "a_b_c_d_e_f"),
        ("../escape", "_escape"),            # '/' -> '_', leading dots stripped
        ("trailing. ", "trailing"),
        ("", None),                           # empty -> UUID, checked below
    ],
)
def test_folder_name_is_filesystem_safe(email, expected):
    uid = uuid4()
    name = user_folder_name(uid, email)
    if expected is None:
        assert name == str(uid)
    else:
        assert name == expected
    # never a separator, never empty, never able to climb out
    assert "/" not in name and "\\" not in name and name
    assert not name.startswith(".")


def test_reserved_device_names_are_prefixed():
    """`con` / `lpt1.txt` are unusable on Windows; `con@example.com` is fine.

    Windows matches the name up to the first dot, so only an exact device name
    (optionally with an extension) is a problem.
    """
    uid = uuid4()
    assert user_folder_name(uid, "con").startswith("user_")
    assert user_folder_name(uid, "lpt1.txt").startswith("user_")
    assert user_folder_name(uid, "con@example.com") == "con@example.com"


def test_workspace_dir_uses_the_username(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        uid = uuid4()
        assert workspace_dir(uid, "ada@example.com").name == "ada@example.com"
        assert workspace_dir(uid).name == str(uid)
    finally:
        config.get_settings.cache_clear()


def test_resource_key_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        uid, sid = uuid4(), uuid4()
        key = resource_key(uid, sid, "book.epub", "ada@example.com")
        # the on-disk component is bounded and carries a short hash; the display
        # name (Source.filename) keeps the original
        assert key.startswith(f"ada@example.com/resources/{sid}/book-")
        assert key.endswith(".epub")
        assert legacy_resource_key(uid, sid, "book.epub") == f"{uid}/resources/{sid}/book.epub"
    finally:
        config.get_settings.cache_clear()


# --------------------------------------------------------------- candidates


def test_candidates_cover_all_three_spellings():
    uid, sid = uuid4(), uuid4()
    legacy_row = _FakeSource(sid, "book.epub", legacy_resource_key(uid, sid, "book.epub"))
    keys = storage_candidates(legacy_row, uid, "ada@example.com")
    # recorded key first, then the username location, then the raw uuid one
    assert keys[0] == f"{uid}/resources/{sid}/book.epub"
    assert any(k.startswith(f"ada@example.com/resources/{sid}/book-") for k in keys)
    assert len(keys) == len(set(keys))


def test_candidates_try_the_recorded_key_first_then_the_legacy_spelling():
    """A current row keeps the uuid fallback: its file may still sit there if the
    account has not logged in since the rename."""
    uid, sid = uuid4(), uuid4()
    current = resource_key(uid, sid, "book.epub", "ada@example.com")
    keys = storage_candidates(_FakeSource(sid, "book.epub", current), uid, "ada@example.com")
    assert keys[0] == current
    assert legacy_resource_key(uid, sid, "book.epub") in keys
    assert len(keys) == len(set(keys))      # no duplicates



# ---------------------------------------------------------------- migration


def test_migration_moves_uuid_folder_to_username(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        uid, sid = uuid4(), uuid4()
        old = tmp_path / str(uid)
        (old / "resources" / str(sid)).mkdir(parents=True)
        (old / "resources" / str(sid) / "book.epub").write_bytes(b"EPUBDATA")
        (old / "records").mkdir()
        (old / "records" / "overview.json").write_text("{}", encoding="utf-8")

        assert migrate_legacy_folder(uid, "ada@example.com") is True

        new = tmp_path / "ada@example.com"
        assert (new / "resources" / str(sid) / "book.epub").read_bytes() == b"EPUBDATA"
        assert (new / "records" / "overview.json").is_file()
        assert not old.exists()            # the uuid folder is gone once empty

        # idempotent: a second run is a no-op, and cannot duplicate the file
        assert migrate_legacy_folder(uid, "ada@example.com") is False
        assert (new / "resources" / str(sid) / "book.epub").read_bytes() == b"EPUBDATA"
    finally:
        config.get_settings.cache_clear()


def test_migration_merges_when_target_already_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(tmp_path))
    from app.core import config

    config.get_settings.cache_clear()
    try:
        uid = uuid4()
        old = tmp_path / str(uid)
        new = tmp_path / "ada@example.com"
        (old / "resources" / "a").mkdir(parents=True)
        (old / "resources" / "a" / "old.txt").write_bytes(b"OLD")
        (new / "resources" / "b").mkdir(parents=True)
        (new / "resources" / "b" / "new.txt").write_bytes(b"NEW")

        assert migrate_legacy_folder(uid, "ada@example.com") is True

        assert (new / "resources" / "a" / "old.txt").read_bytes() == b"OLD"
        assert (new / "resources" / "b" / "new.txt").read_bytes() == b"NEW"
    finally:
        config.get_settings.cache_clear()


# ------------------------------------------------------------- read fallback


class _RecordingStorage:
    """Minimal local-style storage that reports which key it was asked for."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.asked: list[str] = []

    async def get_bytes(self, key: str) -> bytes:
        self.asked.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


@pytest.mark.asyncio
async def test_read_falls_back_to_username_folder_after_migration(monkeypatch):
    uid, sid = uuid4(), uuid4()
    from app.services.workspace import safe_object_name

    moved_key = f"ada@example.com/resources/{sid}/{safe_object_name(sid, 'book.epub')}"
    storage = _RecordingStorage({moved_key: b"MOVED"})
    monkeypatch.setattr("app.services.storage.get_storage", lambda: storage)

    row = _FakeSource(sid, "book.epub", legacy_resource_key(uid, sid, "book.epub"))
    data = await read_source_bytes(row, uid, "ada@example.com")

    assert data == b"MOVED"
    # it tried the recorded (stale) key first, then found the migrated one
    assert storage.asked[0] == f"{uid}/resources/{sid}/book.epub"
    assert moved_key in storage.asked


@pytest.mark.asyncio
async def test_read_prefers_the_recorded_key(monkeypatch):
    uid, sid = uuid4(), uuid4()
    recorded = f"{uid}/resources/{sid}/book.epub"
    storage = _RecordingStorage({recorded: b"ORIGINAL"})
    monkeypatch.setattr("app.services.storage.get_storage", lambda: storage)

    row = _FakeSource(sid, "book.epub", recorded)
    assert await read_source_bytes(row, uid, "ada@example.com") == b"ORIGINAL"
    assert storage.asked == [recorded]


@pytest.mark.asyncio
async def test_read_reports_missing_file_clearly(monkeypatch):
    uid, sid = uuid4(), uuid4()
    monkeypatch.setattr("app.services.storage.get_storage", lambda: _RecordingStorage({}))
    row = _FakeSource(sid, "book.epub", legacy_resource_key(uid, sid, "book.epub"))
    with pytest.raises(FileNotFoundError):
        await read_source_bytes(row, uid, "ada@example.com")

# ------------------------------------------------------- object name bounds


def test_long_library_filename_is_bounded_but_keeps_its_extension():
    """The bug that started this: a 230-character export name blew past the
    Windows path limit, the write left an empty folder behind, and every later
    read reported the file as missing."""
    long_name = (
        "The Hermes Agent Handbook Expanded Edition Memory, Skills, Automations, "
        "Workflows, and Production Guardrails (Hermes Agents… (Lu, Xiao) "
        "(z-library.sk, 1lib.sk, z-lib.sk).epub"
    )
    name = safe_object_name("sid", long_name)
    assert len(name) <= MAX_OBJECT_STEM + 16           # bound + "-hash.ext"
    assert name.endswith(".epub")
    assert "/" not in name and "\\" not in name


def test_safe_object_name_replaces_unsafe_characters_and_keeps_uniqueness():
    a = safe_object_name("sid", "a/b:c*d?.pdf")
    b = safe_object_name("sid", "a_b_c_d_.pdf")
    assert all(ch not in a for ch in '/:*?"<>|')
    # two different originals must not collide even when the visible part matches
    assert a != safe_object_name("sid", "a/b:c*d?-2.pdf")


def test_safe_object_name_handles_extensionless_and_reserved_names():
    assert safe_object_name("sid", "README").startswith("README-")
    assert safe_object_name("sid", "con.txt").startswith("file_con")
    assert safe_object_name("sid", None).startswith("upload-")
