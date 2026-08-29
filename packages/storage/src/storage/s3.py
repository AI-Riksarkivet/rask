"""S3 source and sink — picklable for Ray actors via lazy client factory.

Two things are deliberately written ONCE here. The lazy-client/pickle trio lives on
:class:`_LazyClientAdapter`, because it was duplicated byte-for-byte in both adapters, comment
included. And the ``list_objects_v2`` pagination lives in :func:`_paginate_keys`, because it was
written three times in this one file — which is also where the three copies applied the taxonomy
inconsistently.

Every S3 call is wrapped in :func:`~storage.errors.s3_errors`. That module exists precisely so no
caller of this package has to import botocore, and until now it wrapped nothing inside the package
that owns it: a missing bucket left here as a raw ``ClientError``, which is how it reached a UI as
"Storage service unreachable (HTTP 500)".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TYPE_CHECKING

from storage.errors import s3_errors


if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def _paginate_keys(client: S3Client, bucket: str, prefix: str = "") -> Iterator[str]:
    """Every key under ``bucket``/``prefix``, page by page, in listing order.

    Bucket-scoped (no ``key``), so a not-found inside the listing is reported against the bucket —
    which is the only thing it can be about: an empty bucket lists successfully.
    """
    with s3_errors(bucket=bucket):
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


def iter_keys(client: S3Client, bucket: str, prefix: str = "", suffix: str = "") -> Iterator[str]:
    """Yield keys under `bucket`/`prefix`, optionally filtered by case-insensitive `suffix`.

    Sync — wrap in `anyio.to_thread.run_sync` if called from an event loop.
    """
    suffix_lc = suffix.lower()
    for key in _paginate_keys(client, bucket, prefix):
        if not suffix_lc or key.lower().endswith(suffix_lc):
            yield key


class _LazyClientAdapter:
    """The client an adapter talks through: given directly, or built on first use from a factory.

    The factory form is what makes an adapter picklable — a boto3 client is not, so a driver ships
    the RECIPE across the process boundary and each Ray worker builds its own.
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
    """S3 source. Pass `client` for tests (with moto) or `client_factory` for runs
    that need to ship the source through pickle (e.g. Ray Data driver scripts).
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        suffixes: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".tif", ".tiff"),
        client: S3Client | None = None,
        client_factory: Callable[[], S3Client] | None = None,
    ):
        super().__init__(client=client, client_factory=client_factory)
        self.bucket = bucket
        self.prefix = prefix
        self.suffixes = tuple(s.lower() for s in suffixes)

    def keys(self) -> Iterable[str]:
        for key in _paginate_keys(self.client, self.bucket, self.prefix):
            if any(key.lower().endswith(s) for s in self.suffixes):
                yield key

    def read(self, key: str) -> bytes:
        with s3_errors(bucket=self.bucket, key=key):
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()


class S3Sink(_LazyClientAdapter):
    """S3 sink with same lazy-client semantics as S3Source. `prefix` scopes
    `existing_keys()` listings (important for resumability against large buckets)."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        content_type: str = "application/xml",
        client: S3Client | None = None,
        client_factory: Callable[[], S3Client] | None = None,
    ):
        super().__init__(client=client, client_factory=client_factory)
        self.bucket = bucket
        self.prefix = prefix
        self.content_type = content_type

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        suffix_lc = suffix.lower()
        for key in _paginate_keys(self.client, self.bucket, self.prefix):
            if not suffix_lc or key.lower().endswith(suffix_lc):
                yield key

    def write(self, key: str, data: bytes) -> None:
        with s3_errors(bucket=self.bucket, key=key):
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=self.content_type)
