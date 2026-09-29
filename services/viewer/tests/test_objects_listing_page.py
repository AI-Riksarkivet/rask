"""The object browser must expose S3's cursor rather than swallowing it.

open_fastapi-audit — "`GET /objects` drains the whole boto3 `list_objects_v2` paginator into memory —
no `max_keys`, no continuation token on the wire".

The route's only query parameter was `prefix`. The body iterated `paginator.paginate(...)` to
EXHAUSTION, accumulating every common prefix and every key into two Python lists, and `S3Listing`
carried `bucket`, `prefix`, `prefixes`, `objects` — no cursor to hand back. So the caller could not
ask for less and could not ask for more: the route decided, and the decision was "all of it".

`Delimiter="/"` bounds the listing to one level, which is why this is survivable in dev — but the
levels that matter here are exactly the flat ones, and every growth driver is monotonic: a Lance
table's `data/` prefix holds one object per fragment (`services/maintenance` exists because fragments
accumulate), `_versions/` one manifest per commit, `_transactions/` one per commit, and the medallion
cascade commits per stage per run.

THE CURSOR ALREADY EXISTS — that is what makes this the cheap fix rather than a redesign.
`list_objects_v2` is natively cursor-paginated and its token is already opaque, so exposing it is
`pagination.md`'s cursor strategy with no cursor to invent. Swallowing a cursor the underlying API
hands you is the "unbounded fetch-all" the whole reference exists to prevent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from viewer.api.v1.endpoints import objects as objects_ep


if TYPE_CHECKING:
    from viewer.core.config import ViewerSettings


def test_the_handler_makes_ONE_call_not_a_drained_paginator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assertion that catches a fix applied to the signature but not the body.

    Adding `max_keys` and then slicing a fully-drained paginator would satisfy every check above while
    still listing the entire prefix into memory — the exact defect, wearing the fix's clothes.
    """
    calls: list[dict[str, object]] = []

    class _Settings:
        """Only what `_require_browse` reads. A real ViewerSettings would drag in env the browse gate
        does not need for a test about PAGING."""

        fga_root_object = "system:rask"

    class _Client:
        def get_paginator(self, _op: str) -> object:  # pragma: no cover - must not be used
            raise AssertionError("the handler still uses a paginator, so it still drains the whole prefix")

        def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "CommonPrefixes": [{"Prefix": "a/"}],
                "Contents": [{"Key": "a/one", "Size": 1, "LastModified": None}],
                "NextContinuationToken": "opaque-token",
                "IsTruncated": True,
            }

    monkeypatch.setattr(objects_ep, "_client_for", lambda _b: _Client())
    monkeypatch.setattr(objects_ep, "_registered_bucket", lambda b: b)

    import asyncio

    async def _allow(**_kw: object) -> bool:
        return True

    result = asyncio.run(
        objects_ep.list_objects(
            checker=_allow,
            subject="gina",
            settings=cast("ViewerSettings", _Settings()),
            bucket="b",
            prefix="a/",
            max_keys=10,
            continuation_token=None,
        )
    )

    assert len(calls) == 1, f"the handler made {len(calls)} S3 calls for one page"
    assert calls[0]["MaxKeys"] == 10, "the caller's bound never reached S3"
    assert result.next_continuation_token == "opaque-token", "the cursor was swallowed again"
