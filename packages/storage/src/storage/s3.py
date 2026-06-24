"""S3 source and sink — picklable for Ray actors via lazy client factory."""

from collections.abc import Callable, Iterable, Iterator
from typing import Any


def iter_keys(client: Any, bucket: str, prefix: str = "", suffix: str = "") -> Iterator[str]:  # noqa: ANN401 — boto3 client has no public stub
    """Yield keys under `bucket`/`prefix`, optionally filtered by case-insensitive `suffix`.

    Sync — wrap in `anyio.to_thread.run_sync` if called from an event loop.
    """
    paginator = client.get_paginator("list_objects_v2")
    suffix_lc = suffix.lower()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not suffix_lc or key.lower().endswith(suffix_lc):
                yield key


class S3Source:
    """S3 source. Pass `client` for tests (with moto) or `client_factory` for runs
    that need to ship the source through pickle (e.g. Ray Data driver scripts).
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        suffixes: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".tif", ".tiff"),
        client: Any | None = None,  # noqa: ANN401
        client_factory: Callable[[], Any] | None = None,
    ):
        if client is None and client_factory is None:
            raise ValueError("S3Source needs either `client` or `client_factory`")
        self._client = client
        self._client_factory = client_factory
        self.bucket = bucket
        self.prefix = prefix
        self.suffixes = tuple(s.lower() for s in suffixes)

    @property
    def client(self) -> Any:  # noqa: ANN401 — boto3 client has no public stub
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        if self._client_factory is not None:
            state["_client"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    def keys(self) -> Iterable[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if any(key.lower().endswith(s) for s in self.suffixes):
                    yield key

    def read(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()


class S3Sink:
    """S3 sink with same lazy-client semantics as S3Source. `prefix` scopes
    `existing_keys()` listings (important for resumability against large buckets)."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        content_type: str = "application/xml",
        client: Any | None = None,  # noqa: ANN401
        client_factory: Callable[[], Any] | None = None,
    ):
        if client is None and client_factory is None:
            raise ValueError("S3Sink needs either `client` or `client_factory`")
        self._client = client
        self._client_factory = client_factory
        self.bucket = bucket
        self.prefix = prefix
        self.content_type = content_type

    @property
    def client(self) -> Any:  # noqa: ANN401 — boto3 client has no public stub
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        if self._client_factory is not None:
            state["_client"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    def existing_keys(self, suffix: str = "") -> Iterable[str]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not suffix or key.lower().endswith(suffix.lower()):
                    yield key

    def write(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=self.content_type)
