from __future__ import annotations

import asyncio
from io import BytesIO

from minio import Minio

from .config import Settings


class ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.minio_enabled
        self.bucket = settings.minio_bucket
        self.client = (
            Minio(
                settings.minio_endpoint,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                secure=settings.minio_secure,
            )
            if self.enabled
            else None
        )

    async def ensure_bucket(self) -> None:
        if not self.client:
            return
        exists = await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        if not exists:
            await asyncio.to_thread(self.client.make_bucket, self.bucket)

    async def ping(self) -> bool:
        if not self.client:
            return True
        await asyncio.to_thread(self.client.bucket_exists, self.bucket)
        return True

    async def put_bytes(self, key: str, data: bytes, content_type: str) -> str | None:
        if not self.client:
            return None
        await asyncio.to_thread(
            self.client.put_object,
            self.bucket,
            key,
            BytesIO(data),
            len(data),
            content_type=content_type,
        )
        return key

    async def get_bytes(self, key: str) -> bytes:
        if not self.client:
            raise FileNotFoundError("Object storage is disabled")

        def _read() -> bytes:
            response = self.client.get_object(self.bucket, key)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_read)
