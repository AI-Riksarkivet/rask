"""Pluggable validation rules — size, dimensions, extensions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rahcp_validate.images import ValidationError


@dataclass
class Rule:
    """A named validation check."""

    name: str
    check: Callable[[Path], None]


def max_file_size(limit_bytes: int) -> Rule:
    """Reject files larger than ``limit_bytes``."""

    def check(path: Path) -> None:
        size = path.stat().st_size
        if size > limit_bytes:
            raise ValidationError(
                path,
                f"File too large: {size:,} bytes (limit: {limit_bytes:,})",
            )

    return Rule(name=f"max_file_size({limit_bytes:,})", check=check)


def image_dimensions(
    *,
    min_w: int = 1,
    min_h: int = 1,
    max_w: int = 65535,
    max_h: int = 65535,
) -> Rule:
    """Check image dimensions are within bounds."""

    def check(path: Path) -> None:
        from PIL import Image  # ty: ignore[unresolved-import]

        try:
            with Image.open(path) as img:
                w, h = img.size
        except Exception as exc:
            raise ValidationError(path, f"Cannot read image dimensions: {exc}") from exc

        if w < min_w or h < min_h:
            raise ValidationError(
                path, f"Image too small: {w}x{h} (minimum: {min_w}x{min_h})"
            )
        if w > max_w or h > max_h:
            raise ValidationError(
                path, f"Image too large: {w}x{h} (maximum: {max_w}x{max_h})"
            )

    return Rule(name="image_dimensions", check=check)


def allowed_extensions(*exts: str) -> Rule:
    """Only allow files with the given extensions (case-insensitive)."""
    normalized = {e.lower().lstrip(".") for e in exts}

    def check(path: Path) -> None:
        suffix = path.suffix.lower().lstrip(".")
        if suffix not in normalized:
            raise ValidationError(
                path,
                f"Extension '.{suffix}' not allowed (allowed: {', '.join(sorted(normalized))})",
            )

    return Rule(
        name=f"allowed_extensions({', '.join(sorted(normalized))})", check=check
    )


def validate(path: Path, rules: list[Rule]) -> list[ValidationError]:
    """Run all rules against a file, collecting all failures.

    Does not stop on the first failure — returns all validation errors.
    """
    errors: list[ValidationError] = []
    for rule in rules:
        try:
            rule.check(path)
        except ValidationError as exc:
            errors.append(exc)
    return errors
