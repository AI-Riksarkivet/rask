"""Tests for image validation — bytes validators (streaming) and Path validators."""

import io

import pytest
from PIL import Image

from validate import (
    ValidationError,
    validate_bytes_by_extension,
    validate_jpg,
    validate_jpg_bytes,
    validate_png_bytes,
    validate_tiff_bytes,
)


def _img_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, fmt)
    return buf.getvalue()


def test_valid_jpg_bytes_pass():
    validate_jpg_bytes(_img_bytes("JPEG"))


def test_truncated_jpg_bytes_raises():
    with pytest.raises(ValidationError, match="EOI"):
        validate_jpg_bytes(_img_bytes("JPEG")[:-2])  # strip EOI marker


def test_wrong_soi_marker_raises():
    with pytest.raises(ValidationError, match="SOI"):
        validate_jpg_bytes(b"not a jpeg at all")


def test_valid_magic_but_corrupt_body_fails_decode():
    # SOI + EOI present, garbage between -> Pillow decode must fail.
    with pytest.raises(ValidationError, match="load failed"):
        validate_jpg_bytes(b"\xff\xd8" + b"garbage" + b"\xff\xd9")


def test_valid_png_bytes_pass():
    validate_png_bytes(_img_bytes("PNG"))


def test_bad_png_signature_raises():
    with pytest.raises(ValidationError, match="signature"):
        validate_png_bytes(b"\x89PNGbad!" + b"\x00" * 16)


def test_valid_tiff_bytes_pass():
    validate_tiff_bytes(_img_bytes("TIFF"))


def test_bad_tiff_marker_raises():
    with pytest.raises(ValidationError, match="byte order marker"):
        validate_tiff_bytes(b"XX\x00\x2a" + b"\x00" * 20)


def test_validate_bytes_by_extension_dispatches():
    validate_bytes_by_extension(_img_bytes("JPEG"), ".jpg")  # ok
    with pytest.raises(ValidationError):
        validate_bytes_by_extension(b"\xff\xd8garbage\xff\xd9", ".jpeg")


def test_validate_bytes_unknown_extension_is_skipped():
    validate_bytes_by_extension(b"whatever", ".txt")  # no error


def test_path_validator_reuses_bytes_check(tmp_path):
    good = tmp_path / "ok.jpg"
    good.write_bytes(_img_bytes("JPEG"))
    validate_jpg(good)


def test_path_validator_missing_file_raises(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        validate_jpg(tmp_path / "nope.jpg")
