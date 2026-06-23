from pydantic import BaseModel


class PageEntry(BaseModel):
    key: str
    hasAlto: bool


class S3Object(BaseModel):
    """A single object under a prefix (mirrors `S3Object` in storage-frontend's `storage.ts`)."""

    key: str
    size: int
    last_modified: str | None


class S3Listing(BaseModel):
    """A delimiter-listed page of a bucket (mirrors `S3Listing` in storage-frontend's `storage.ts`).

    `prefixes` are the "folder" common-prefixes directly under `prefix`; `objects` are the
    leaf keys at this level.
    """

    bucket: str
    prefix: str
    prefixes: list[str]
    objects: list[S3Object]
