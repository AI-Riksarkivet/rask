"""The download disposition must survive a hostile object key (VS-10).

open_python-audit VS-10 — the S3 object key is caller-supplied (`key` query param) and
`download_object` interpolated its basename RAW into `Content-Disposition:
attachment; filename="{filename}"`. A key whose basename contains a `"` breaks out of
the quoted-string, so the client parses trailing junk as extra disposition parameters —
filename spoofing at minimum. (CRLF is separately rejected by the ASGI server's
header-value validation, so the exposure is quote-breakout, not full header injection.)

The contract pinned here: the ASCII `filename=` fallback contains no `"`, `\\` or
control characters, and the real name rides an RFC 6266 `filename*=UTF-8''<pct-encoded>`
parameter so nothing is lost for names that needed cleaning.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, cast

import pytest
from viewer.api.v1.endpoints import objects as objects_ep


if TYPE_CHECKING:
    from fastapi.responses import Response
    from viewer.core.config import ViewerSettings

#: One quoted-string with no raw quote/backslash/control char inside, optionally followed by
#: exactly one RFC 6266 ext parameter. Anything else means the key broke out of the header.
_WELLFORMED = re.compile(r"^attachment; filename=\"[^\"\\\x00-\x1f\x7f]*\"(?:; filename\*=UTF-8''[A-Za-z0-9%._~!$&+\-^`|]*)?$")


class _Settings:
    """Only what `_require_browse` reads."""

    fga_root_object = "system:rask"


class _Body:
    def read(self) -> bytes:
        return b"payload"


class _Client:
    def get_object(self, **_kw: object) -> dict[str, object]:
        return {"ContentType": "image/jpeg", "Body": _Body()}


def _download(monkeypatch: pytest.MonkeyPatch, key: str) -> Response:
    monkeypatch.setattr(objects_ep, "_client_for", lambda _b: _Client())
    monkeypatch.setattr(objects_ep, "_registered_bucket", lambda b: b)

    async def _allow(**_kw: object) -> bool:
        return True

    return asyncio.run(
        objects_ep.download_object(
            checker=_allow,
            subject="gina",
            settings=cast("ViewerSettings", _Settings()),
            bucket="b",
            key=key,
        )
    )


def test_a_quote_in_the_key_cannot_break_out_of_the_disposition(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _download(monkeypatch, 'dir/a"b.jpg')
    header = resp.headers["content-disposition"]
    assert _WELLFORMED.match(header), (
        f"Content-Disposition is not a single well-formed disposition: {header!r} — the raw quote from the object key escaped the quoted filename"
    )


def test_the_real_name_survives_via_rfc6266_ext_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleaning the fallback must not LOSE the name: the pct-encoded form carries it whole."""
    resp = _download(monkeypatch, 'dir/a"b.jpg')
    header = resp.headers["content-disposition"]
    assert "filename*=UTF-8''a%22b.jpg" in header, header


def test_a_plain_key_still_gets_its_plain_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _download(monkeypatch, "dir/plain.jpg")
    header = resp.headers["content-disposition"]
    assert 'filename="plain.jpg"' in header, header
    assert _WELLFORMED.match(header), header


def test_a_key_that_cleans_to_nothing_falls_back_to_download(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = _download(monkeypatch, 'dir/"')
    header = resp.headers["content-disposition"]
    assert 'filename="download"' in header, header
    assert _WELLFORMED.match(header), header
