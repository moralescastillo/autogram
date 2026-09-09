"""S3-compatible object storage — the serving backend.

One implementation covers both Amazon S3 and Google Cloud Storage, because GCS
exposes an S3-compatible XML API. The only differences are the endpoint and a
GCS quirk handled below.

    gs://<access_key>:<secret>@<bucket>/<prefix>
    s3://<access_key>:<secret>@<bucket>/<prefix>

GCS credentials are **HMAC keys** (Cloud Storage → Settings → Interoperability),
not service-account JSON — which is what keeps the connection string to one
line.

This is the backend that hands Instagram a URL. Objects stay private and are
served through short-lived presigned URLs.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from autogram.storage.base import Storage, StorageEntry
from autogram.storage.dsn import parse

log = logging.getLogger(__name__)

ENDPOINTS = {
    "gs": "https://storage.googleapis.com",
    "s3": None,  # boto3's default, derived from the region
}

#: Instagram fetches media while a container processes, and a video container
#: can take several minutes. An hour is comfortably clear of that.
DEFAULT_URL_LIFETIME = timedelta(hours=1)


class ObjectStore(Storage):
    can_serve = True

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str = "",
        *,
        endpoint_url: str | None = None,
        region: str = "auto",
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.prefix = prefix.strip("/")

        self._client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint_url,
            region_name=region,
            # SigV4 is required for GCS presigned URLs to validate, and is the
            # current default for S3 anyway.
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

        if endpoint_url and "googleapis" in endpoint_url:
            self._disable_encoding_type()

    def _disable_encoding_type(self) -> None:
        """Stop botocore sending ``EncodingType=url`` on list calls.

        botocore adds this parameter automatically for S3, but the GCS XML API
        rejects it with ``InvalidArgument``. Removing the handler is the
        documented workaround and affects nothing else.
        """
        from botocore import handlers

        self._client.meta.events.unregister(
            "before-parameter-build.s3.ListObjects",
            handlers.set_list_objects_encoding_type_url,
        )
        self._client.meta.events.unregister(
            "before-parameter-build.s3.ListObjectsV2",
            handlers.set_list_objects_encoding_type_url,
        )

    @classmethod
    def from_dsn(cls, dsn: str) -> "ObjectStore":
        parsed = parse(dsn, expected_credentials=2)
        access_key, secret_key = parsed.credentials
        return cls(
            access_key=access_key,
            secret_key=secret_key,
            bucket=parsed.bucket,
            prefix=parsed.prefix,
            endpoint_url=ENDPOINTS.get(parsed.scheme),
        )

    # --- key mapping ---------------------------------------------------

    def _key(self, path: str) -> str:
        path = path.strip("/")
        return f"{self.prefix}/{path}" if self.prefix else path

    def _unkey(self, key: str) -> str:
        if self.prefix and key.startswith(f"{self.prefix}/"):
            return key[len(self.prefix) + 1 :]
        return key

    # --- interface -----------------------------------------------------

    def list(self, path: str) -> list[StorageEntry]:
        """List direct children of ``path``.

        Object stores have no directories, only key prefixes. Using ``/`` as a
        delimiter makes them behave like folders: ``CommonPrefixes`` are the
        subfolders and ``Contents`` the files.
        """
        prefix = self._key(path)
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        entries: list[StorageEntry] = []
        paginator = self._client.get_paginator("list_objects_v2")

        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=prefix, Delimiter="/"
        ):
            for folder in page.get("CommonPrefixes", []):
                entries.append(
                    StorageEntry(path=self._unkey(folder["Prefix"].rstrip("/")), is_dir=True)
                )
            for obj in page.get("Contents", []):
                if obj["Key"] == prefix:
                    continue  # the folder marker itself, if one exists
                entries.append(
                    StorageEntry(
                        path=self._unkey(obj["Key"]),
                        is_dir=False,
                        size=obj.get("Size"),
                    )
                )

        return sorted(entries, key=lambda e: e.path)

    def read(self, path: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._key(path))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                raise FileNotFoundError(path) from exc
            raise
        return response["Body"].read()

    def write(self, path: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._key(path), Body=data)

    def move(self, src: str, dst: str) -> None:
        """Move a file or a whole prefix. Object stores have no native move."""
        moved = False
        for entry in self._walk(src):
            relative = entry[len(self._key(src)) :].lstrip("/")
            target = f"{self._key(dst)}/{relative}" if relative else self._key(dst)
            self._client.copy_object(
                Bucket=self.bucket,
                CopySource={"Bucket": self.bucket, "Key": entry},
                Key=target,
            )
            self._client.delete_object(Bucket=self.bucket, Key=entry)
            moved = True

        if not moved:
            raise FileNotFoundError(src)

    def delete(self, path: str) -> None:
        for key in self._walk(path):
            self._client.delete_object(Bucket=self.bucket, Key=key)

    def _walk(self, path: str) -> list[str]:
        """Every key at ``path``, whether it is one object or a prefix."""
        key = self._key(path)
        paginator = self._client.get_paginator("list_objects_v2")

        exact: list[str] = []
        nested: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            for obj in page.get("Contents", []):
                if obj["Key"] == key:
                    exact.append(obj["Key"])
                elif obj["Key"].startswith(f"{key}/"):
                    nested.append(obj["Key"])

        return exact + nested

    def fetchable_url(
        self, path: str, expires_in: timedelta = DEFAULT_URL_LIFETIME
    ) -> str:
        """A presigned HTTPS URL Instagram can fetch, leaving the object private."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(path)},
            ExpiresIn=int(expires_in.total_seconds()),
        )
