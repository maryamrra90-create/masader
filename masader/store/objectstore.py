"""Object storage behind one interface (architecture ADR-13): a local folder in development, S3 in the
cloud. Raw source snapshots, report exports and backups go through `put`/`get`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..settings import settings


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalFSStore:
    """Development store: objects are files under one root folder. Keys may not escape the root."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError(f"object key escapes the store: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)                         # atomic on one filesystem
        return f"local://{key}"

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Store:
    """Production store. boto3 is an optional dependency (only the cloud image installs it); the client
    uses the instance IAM role, so no access keys are configured here."""

    def __init__(self, bucket: str, client=None):
        if client is None:
            import boto3  # noqa: PLC0415 - optional dependency, imported only when S3 is selected
            client = boto3.client("s3")
        self.bucket, self.client = bucket, client

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                               ServerSideEncryption="AES256")
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_store() -> ObjectStore:
    if settings.object_store == "s3":
        return S3Store(settings.s3_bucket)
    return LocalFSStore(settings.object_dir)
