"""validate — Pre-upload file validation for batch ingest workflows."""

from __future__ import annotations

from validate.images import (
    ValidationError,
    validate_by_extension,
    validate_bytes_by_extension,
    validate_jpg,
    validate_jpg_bytes,
    validate_png,
    validate_png_bytes,
    validate_tiff,
    validate_tiff_bytes,
)
from validate.rules import (
    Rule,
    allowed_extensions,
    image_dimensions,
    max_file_size,
    validate,
)


__all__ = [
    "Rule",
    "ValidationError",
    "allowed_extensions",
    "image_dimensions",
    "max_file_size",
    "validate",
    "validate_by_extension",
    "validate_bytes_by_extension",
    "validate_jpg",
    "validate_jpg_bytes",
    "validate_png",
    "validate_png_bytes",
    "validate_tiff",
    "validate_tiff_bytes",
]
