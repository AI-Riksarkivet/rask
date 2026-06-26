from pydantic import BaseModel


class PageEntry(BaseModel):
    key: str
    hasAlto: bool


class S3Object(BaseModel):
    """A single object under a prefix (mirrors `S3Object` in storage's `storage.ts`)."""

    key: str
    size: int
    last_modified: str | None


class S3Listing(BaseModel):
    """A delimiter-listed page of a bucket (mirrors `S3Listing` in storage's `storage.ts`).

    `prefixes` are the "folder" common-prefixes directly under `prefix`; `objects` are the
    leaf keys at this level.
    """

    bucket: str
    prefix: str
    prefixes: list[str]
    objects: list[S3Object]


class S3ObjectHead(BaseModel):
    """Metadata for a single object (S3 HEAD) — the storage browser's detail panel.

    Mirrors `S3ObjectHead` in storage's `storage.ts`.
    """

    key: str
    size: int
    content_type: str | None
    last_modified: str | None
    etag: str | None
