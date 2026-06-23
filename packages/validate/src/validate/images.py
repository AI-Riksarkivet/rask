"""Image integrity checks — TIFF, JPEG and PNG corruption detection.

The checks are defined on **bytes** (magic markers + a full Pillow decode); the
``Path`` validators simply read the file and delegate, so disk and in-memory
(streaming) callers share exactly one implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from PIL import Image


class ValidationError(Exception):
    """Raised when an image fails validation."""

    def __init__(self, source: Path | str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.path = source
        self.reason = reason


def _pil_decode(data: bytes, source: Path | str, fmt: str) -> None:
    """Fully decode image bytes with Pillow to detect truncation/corruption."""
    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
    except Exception as exc:
        raise ValidationError(source, f"{fmt} load failed: {exc}") from exc


# ── Bytes validators (the real implementation) ─────────────────────


def validate_tiff_bytes(data: bytes, *, source: Path | str = "<bytes>") -> None:
    """Verify TIFF bytes: byte-order marker, version 42, and a full Pillow decode."""
    if len(data) < 4:
        raise ValidationError(source, "File too small to be a valid TIFF")
    if data[:2] not in (b"II", b"MM"):
        raise ValidationError(source, f"Invalid TIFF byte order marker: {data[:2]!r}")
    version = int.from_bytes(data[2:4], "little" if data[:2] == b"II" else "big")
    if version != 42:
        raise ValidationError(source, f"Invalid TIFF version: {version} (expected 42)")
    _pil_decode(data, source, "TIFF")


def validate_jpg_bytes(data: bytes, *, source: Path | str = "<bytes>") -> None:
    """Verify JPEG bytes: SOI/EOI markers and a full Pillow decode."""
    if data[:2] != b"\xff\xd8":
        raise ValidationError(source, "Missing JPEG SOI marker")
    if data[-2:] != b"\xff\xd9":
        raise ValidationError(source, "Missing JPEG EOI marker (possibly truncated)")
    _pil_decode(data, source, "JPEG")


def validate_png_bytes(data: bytes, *, source: Path | str = "<bytes>") -> None:
    """Verify PNG bytes: signature and a full Pillow decode."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValidationError(source, "Invalid PNG signature")
    _pil_decode(data, source, "PNG")


# ── Path validators (read file, delegate to the bytes validators) ──


def validate_tiff(path: Path) -> None:
    """Verify a TIFF file is not corrupt. Raises ``ValidationError`` on failure."""
    _check_exists(path)
    validate_tiff_bytes(path.read_bytes(), source=path)


def validate_jpg(path: Path) -> None:
    """Verify a JPEG file is not corrupt. Raises ``ValidationError`` on failure."""
    _check_exists(path)
    validate_jpg_bytes(path.read_bytes(), source=path)


def validate_png(path: Path) -> None:
    """Verify a PNG file is not corrupt. Raises ``ValidationError`` on failure."""
    _check_exists(path)
    validate_png_bytes(path.read_bytes(), source=path)


# ── Extension dispatch ─────────────────────────────────────────────

_PATH_VALIDATORS: dict[str, Callable[[Path], None]] = {
    ".jpg": validate_jpg,
    ".jpeg": validate_jpg,
    ".tif": validate_tiff,
    ".tiff": validate_tiff,
    ".png": validate_png,
}

_BYTES_VALIDATORS: dict[str, Callable[..., None]] = {
    ".jpg": validate_jpg_bytes,
    ".jpeg": validate_jpg_bytes,
    ".tif": validate_tiff_bytes,
    ".tiff": validate_tiff_bytes,
    ".png": validate_png_bytes,
}


def validate_by_extension(path: Path) -> None:
    """Validate a file by its extension. Unknown extensions are skipped.

    Supported: .jpg, .jpeg, .tif, .tiff, .png. Raises ``ValidationError`` if corrupt.
    """
    validator = _PATH_VALIDATORS.get(path.suffix.lower())
    if validator:
        validator(path)


def validate_bytes_by_extension(data: bytes, ext: str, *, source: Path | str = "<bytes>") -> None:
    """Validate in-memory image bytes by extension (e.g. ``".jpg"``).

    Unknown extensions are skipped. Raises ``ValidationError`` if corrupt.
    """
    validator = _BYTES_VALIDATORS.get(ext.lower())
    if validator:
        validator(data, source=source)


def _check_exists(path: Path) -> None:
    if not path.exists():
        raise ValidationError(path, "File does not exist")
    if not path.is_file():
        raise ValidationError(path, "Path is not a file")
