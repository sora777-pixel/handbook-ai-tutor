from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path

from minio import Minio

from app.core.config import get_settings

#: Bytes handed to the client per write while streaming. 1 MiB keeps a 90-minute
#: lecture from being materialised in the process while the player scrubs.
STREAM_BLOCK = 1 << 20


class StorageProvider(ABC):
    @abstractmethod
    async def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        raise NotImplementedError

    @abstractmethod
    async def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    @abstractmethod
    async def ensure_ready(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_bytes(self, key: str) -> bool:
        """Remove one stored object. Returns True when something was removed."""
        raise NotImplementedError

    async def size(self, key: str) -> int | None:
        """Object size in bytes, or None when the backend cannot report it."""
        return None

    async def iter_range(
        self, key: str, start: int, length: int, block: int = STREAM_BLOCK
    ) -> AsyncIterator[bytes]:
        """Yield `length` bytes starting at `start`.

        Default implementation slices a full read; backends that can seek
        override it so a byte range never costs a whole-file read.
        """
        data = await self.get_bytes(key)
        end = min(len(data), start + length)
        for offset in range(start, end, block):
            yield data[offset : min(offset + block, end)]


class LocalStorage(StorageProvider):
    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_settings().local_storage_path).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def ensure_ready(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        """Resolve `key` inside the storage root, refusing traversal escapes."""
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"Refusing to touch a path outside the storage root: {key!r}")
        return candidate

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(data)
        except OSError as exc:
            # Say which path and how long it is: a silent failure here once left
            # an empty resources folder behind, and the next read reported the
            # file as missing with no hint that the name never made it to disk.
            raise OSError(
                f"无法写入 {path}（路径长度 {len(str(path))}，文件名长度 {len(path.name)}）：{exc}"
            ) from exc
        return str(path)

    async def get_bytes(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    async def size(self, key: str) -> int | None:
        try:
            return self._resolve(key).stat().st_size
        except FileNotFoundError:
            return None

    async def iter_range(
        self, key: str, start: int, length: int, block: int = STREAM_BLOCK
    ) -> AsyncIterator[bytes]:
        path = self._resolve(key)
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                piece = handle.read(min(block, remaining))
                if not piece:
                    break
                remaining -= len(piece)
                yield piece

    async def delete_bytes(self, key: str) -> bool:
        path = self._resolve(key)
        if not path.exists():
            return False
        path.unlink()
        # Prune now-empty parent folders (e.g. <root>/<user_id>/<source_id>/)
        # but never the storage root itself.
        parent = path.parent
        while parent != self.root and self.root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break  # not empty -> stop
            parent = parent.parent
        return True


class MinioStorage(StorageProvider):
    def __init__(self) -> None:
        s = get_settings()
        endpoint = s.s3_endpoint.replace("http://", "").replace("https://", "")
        self.client = Minio(
            endpoint,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key,
            secure=s.s3_secure or s.s3_endpoint.startswith("https://"),
            region=s.s3_region,
        )
        self.bucket = s.s3_bucket

    async def ensure_ready(self) -> None:
        from asyncio import to_thread

        def _ensure() -> None:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)

        await to_thread(_ensure)

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        from asyncio import to_thread
        from io import BytesIO

        def _put() -> None:
            self.client.put_object(
                self.bucket,
                key,
                BytesIO(data),
                length=len(data),
                content_type=content_type,
            )

        await to_thread(_put)
        return key

    async def get_bytes(self, key: str) -> bytes:
        from asyncio import to_thread

        def _get() -> bytes:
            resp = self.client.get_object(self.bucket, key)
            try:
                return resp.read()
            finally:
                resp.close()
                resp.release_conn()

        return await to_thread(_get)

    async def size(self, key: str) -> int | None:
        from asyncio import to_thread

        def _stat() -> int | None:
            from minio.error import S3Error

            try:
                return self.client.stat_object(self.bucket, key).size
            except S3Error:
                return None

        return await to_thread(_stat)

    async def iter_range(
        self, key: str, start: int, length: int, block: int = STREAM_BLOCK
    ) -> AsyncIterator[bytes]:
        """Ask S3 for exactly the byte range the browser requested."""
        from asyncio import to_thread

        resp = await to_thread(self.client.get_object, self.bucket, key, None, start, length)
        try:
            while True:
                piece = await to_thread(resp.read, block)
                if not piece:
                    break
                yield piece
        finally:
            await to_thread(resp.close)
            await to_thread(resp.release_conn)

    async def delete_bytes(self, key: str) -> bool:
        from asyncio import to_thread

        def _delete() -> bool:
            from minio.deleteobjects import DeleteObject
            from minio.error import S3Error

            try:
                errors = list(self.client.remove_objects(self.bucket, [DeleteObject(key)]))
            except S3Error:
                return False
            return not errors

        return await to_thread(_delete)


_storage: StorageProvider | None = None


def get_storage() -> StorageProvider:
    global _storage
    if _storage is None:
        backend = get_settings().storage_backend
        _storage = MinioStorage() if backend == "minio" else LocalStorage()
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
