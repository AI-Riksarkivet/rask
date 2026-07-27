"""Provider-agnostic SOURCE adapters + MIME sniffing — the media-agnostic ingest seam.

The lance-ns pattern (now ``service_kit.lakehouse.sources``): a *source* yields raw
objects as bytes plus a stable URI; the provider client stays behind the small
:class:`SourceAdapter` protocol so no provider code leaks into the pipeline.
Deterministic (sorted) iteration order keeps ingests reproducible — same
objects → same ``doc_id``s → same rows every run.

MIME is **sniffed from content** (magic numbers), never assumed from the file
extension — the blobs are agnostic of audio/mp4/image, so the type must come
from the bytes themselves. Extension mapping is only the fallback for formats
without a distinctive signature.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pyarrow.fs as pafs
from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Iterator


class SourceObject(BaseModel):
    """One raw object to ingest: its bytes plus the stable ``uri`` recorded as provenance."""

    model_config = {"frozen": True}

    uri: str
    data: bytes


class SourceAdapter(Protocol):
    """Yields the objects to ingest. Providers implement this outside the pipeline core."""

    def iter_objects(self) -> Iterator[SourceObject]: ...


class LocalDirSource:
    """A :class:`SourceAdapter` over a local directory tree (``file://`` URIs, sorted)."""

    def __init__(self, root: Path, pattern: str = "*") -> None:
        self._root = root
        self._pattern = pattern

    def iter_objects(self) -> Iterator[SourceObject]:
        for path in sorted(self._root.rglob(self._pattern)):
            if path.is_file():
                yield SourceObject(uri=path.resolve().as_uri(), data=path.read_bytes())


class S3Source:
    """A :class:`SourceAdapter` over an S3-compatible bucket prefix (``s3://`` URIs, sorted).

    ``fs`` is a configured ``pyarrow.fs.S3FileSystem`` — endpoint + creds live
    there, so RustFS/MinIO/HCP/AWS are interchangeable behind the protocol.
    """

    def __init__(self, fs: pafs.S3FileSystem, bucket: str, prefix: str = "") -> None:
        self._fs = fs
        self._bucket = bucket
        self._prefix = prefix

    def iter_objects(self) -> Iterator[SourceObject]:
        base = "/".join(part for part in (self._bucket, self._prefix) if part)
        listing = self._fs.get_file_info(pafs.FileSelector(base, recursive=True))
        for info in sorted(listing, key=lambda entry: entry.path):
            if info.type != pafs.FileType.File:
                continue
            try:
                with self._fs.open_input_stream(info.path) as stream:
                    data = stream.readall()
            except OSError as exc:
                raise RuntimeError(f"failed to read source object {info.path!r}") from exc
            yield SourceObject(uri=f"s3://{info.path}", data=data)


#: (signature-predicate, mime) — checked in order against the payload head.
_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"GIF8", 0, "image/gif"),
    (b"ftyp", 4, "video/mp4"),
    (b"OggS", 0, "audio/ogg"),
    (b"ID3", 0, "audio/mpeg"),
    (b"\xff\xfb", 0, "audio/mpeg"),
    (b"\x1a\x45\xdf\xa3", 0, "video/webm"),
    (b"fLaC", 0, "audio/flac"),
)


def sniff_mime(data: bytes, *, uri: str | None = None) -> str:
    """Content-sniffed MIME type; extension mapping only as fallback.

    RIFF containers disambiguate on the form type (WAVE → audio, AVI → video).
    Unknown signatures fall back to the URI's extension, then to
    ``application/octet-stream`` — never a crash, per the agnostic-blob contract.
    """
    head = data[:16]
    if head[:4] == b"RIFF" and len(data) >= 12:
        form = data[8:12]
        if form == b"WAVE":
            return "audio/wav"
        if form == b"AVI ":
            return "video/x-msvideo"
    for signature, offset, mime in _SIGNATURES:
        if data[offset : offset + len(signature)] == signature:
            return mime
    if uri:
        guessed, _ = mimetypes.guess_type(uri)
        if guessed:
            return guessed
    return "application/octet-stream"
