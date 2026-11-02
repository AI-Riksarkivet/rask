"""Where this runner reads page images from and writes its output to: a filesystem or an S3 prefix.

`build_source`/`build_sink` pick the adapter from a URI (`s3://bucket/prefix` or a path), and every adapter
satisfies the :class:`Source` or :class:`Sink` protocol. The S3 pair talks through `storage.s3_client` and
answers in storage's own error taxonomy (`storage.s3_errors`), so no caller here imports botocore.

The S3 adapters are picklable for Ray: built from a `client_factory`, they drop the client on pickle and each
worker builds its own on first use.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

from storage import S3Client, iter_keys, s3_client, s3_errors, split_s3_uri


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


@runtime_checkable
class Source(Protocol):
    """Something this runner reads bytes out of, keyed by an opaque string."""

    def keys(self) -> Iterable[str]:
        """The keys this source offers, materialised lazily."""
        ...

    def read(self, key: str) -> bytes:
        """The bytes at ``key``. Raises :class:`storage.ObjectNotFoundError` when absent."""
        ...


@runtime_checkable
class Sink(Protocol):
    """Somewhere this runner writes bytes, keyed the same way."""

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        """Keys already present (optionally filtered by suffix): what makes a run resumable."""
        ...

    def write(self, key: str, data: bytes) -> None:
        """Write ``data`` at ``key``, creating whatever intermediate structure the backend needs."""
        ...


class FSSource:
    """Filesystem source: yields file paths under `root` matching any of `suffixes`."""

    def __init__(self, root: Path, suffixes: tuple[str, ...] = IMAGE_SUFFIXES) -> None:
        self.root = Path(root)
        self.suffixes = tuple(s.lower() for s in suffixes)

    def keys(self) -> Iterable[str]:
        for path in self.root.rglob("*"):
            if path.is_file() and any(path.name.lower().endswith(s) for s in self.suffixes):
                yield str(path.relative_to(self.root))

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


class FSSink:
    """Filesystem sink: writes files under `root` (creates parent dirs as needed)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        for path in self.root.rglob("*"):
            if path.is_file() and (not suffix or path.name.lower().endswith(suffix.lower())):
                yield str(path.relative_to(self.root))

    def write(self, key: str, data: bytes) -> None:
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class _LazyClientAdapter:
    """The client an adapter talks through: given directly, or built on first use from a factory.

    The factory form is what makes an adapter picklable: a boto3 client is not, so a driver ships the RECIPE
    across the process boundary and each Ray worker builds its own.
    """

    def __init__(self, *, client: S3Client | None, client_factory: Callable[[], S3Client] | None) -> None:
        if client is None and client_factory is None:
            raise ValueError(f"{type(self).__name__} needs either `client` or `client_factory`")
        self._client = client
        self._client_factory = client_factory

    @property
    def client(self) -> S3Client:
        if self._client is None:
            if self._client_factory is None:
                # Unreachable via __init__; only a hand-rolled __setstate__ can get here.
                raise ValueError(f"{type(self).__name__} has neither `client` nor `client_factory`")
            self._client = self._client_factory()
        return self._client

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        if self._client_factory is not None:
            state["_client"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)


class S3Source(_LazyClientAdapter):
    """S3 source. Pass `client` for tests (with moto) or `client_factory` for a source shipped through pickle."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        suffixes: tuple[str, ...] = IMAGE_SUFFIXES,
        client: S3Client | None = None,
        client_factory: Callable[[], S3Client] | None = None,
    ) -> None:
        super().__init__(client=client, client_factory=client_factory)
        self.bucket = bucket
        self.prefix = prefix
        self.suffixes = tuple(s.lower() for s in suffixes)

    def keys(self) -> Iterable[str]:
        for key in iter_keys(self.client, self.bucket, self.prefix):
            if any(key.lower().endswith(s) for s in self.suffixes):
                yield key

    def read(self, key: str) -> bytes:
        with s3_errors(bucket=self.bucket, key=key):
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()


class S3Sink(_LazyClientAdapter):
    """S3 sink with the same lazy-client semantics as :class:`S3Source`. `prefix` scopes `existing_keys()`
    listings, which keeps a resumed run's listing to its own output in a large bucket."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        content_type: str = "application/xml",
        client: S3Client | None = None,
        client_factory: Callable[[], S3Client] | None = None,
    ) -> None:
        super().__init__(client=client, client_factory=client_factory)
        self.bucket = bucket
        self.prefix = prefix
        self.content_type = content_type

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        return iter_keys(self.client, self.bucket, self.prefix, suffix)

    def write(self, key: str, data: bytes) -> None:
        with s3_errors(bucket=self.bucket, key=key):
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=self.content_type)


def merge_prefix(uri_prefix: str, flag_prefix: str) -> str:
    """Concatenate the prefix from a URI's path and one from a flag (the URI's part comes first)."""
    parts = [p for p in (uri_prefix, flag_prefix) if p]
    if not parts:
        return ""
    merged = "/".join(p.strip("/") for p in parts)
    return merged + "/" if merged else ""


def build_source(uri: str, *, s3_endpoint: str | None = None, prefix: str = "", suffixes: tuple[str, ...] = IMAGE_SUFFIXES) -> Source:
    """An `s3://…` URI builds an :class:`S3Source`, anything else a filesystem one."""
    if uri.startswith("s3://"):
        bucket, uri_prefix = split_s3_uri(uri)
        factory = functools.partial(s3_client, s3_endpoint)
        return S3Source(bucket=bucket, prefix=merge_prefix(uri_prefix, prefix), suffixes=suffixes, client_factory=factory)
    root = Path(uri) / prefix if prefix else Path(uri)
    return FSSource(root=root, suffixes=suffixes)


def build_sink(uri: str, *, s3_endpoint: str | None = None, prefix: str = "") -> Sink:
    """An `s3://…` URI builds an :class:`S3Sink`, anything else a filesystem one."""
    if uri.startswith("s3://"):
        bucket, uri_prefix = split_s3_uri(uri)
        factory = functools.partial(s3_client, s3_endpoint)
        return S3Sink(bucket=bucket, prefix=merge_prefix(uri_prefix, prefix), client_factory=factory)
    root = Path(uri) / prefix if prefix else Path(uri)
    return FSSink(root=root)
