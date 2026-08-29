"""The 101st model was invisible, and nothing in the answer said so.

open_fastapi-audit — "`GET /v1/model` truncates the listing at `limit` with no cursor, no `total` and
no `capped` flag — the 101st model is invisible and unreachable".

THE FINDING CORRECTS ITSELF TWICE and both corrections are honoured. "Unreachable" is false — `limit`
accepts up to 1000, so the finding's own 101-150 case was always reachable with `?limit=1000`. And the
truncation is a RECORDED decision, not an oversight: the docstring says "``limit`` bounds the
per-request dataset opens (one per listed model); truncation is deterministic (name-sorted first
``limit``)", and that per-model open is the reason the ceiling exists at all.

What survives is the envelope: a slice that reports neither a total nor a continuation is truncation
wearing pagination's clothes, and three siblings in this service already do better.

FIXED WITH THE HELPER THAT ALREADY SHIPS HERE, per the Fix: `catalog.api.pagination.paginate` is a stateless
keyset over a sorted name list — the cursor is the previous page's last name — which is exactly this
listing's shape. Reusing it keeps ONE cursor implementation in the service rather than a second that
can drift from it.
"""

from __future__ import annotations

import inspect

from catalog.api.v1.endpoints import models as models_ep
from catalog.schemas import ModelsListResponse


def test_the_listing_accepts_a_page_token() -> None:
    """Without a cursor parameter, a caller has no way to ask for the next page at all."""
    assert "page_token" in inspect.signature(models_ep.list_models).parameters, (
        "`GET /v1/model` takes no page_token, so the only way past `limit` models is a bigger limit"
    )


def test_the_response_hands_back_the_next_cursor() -> None:
    """A cursor the caller cannot read is the same dead end as no cursor."""
    assert "page_token" in ModelsListResponse.model_fields, "the response carries no page_token, so a client cannot continue the listing"


def test_a_complete_listing_reports_no_continuation() -> None:
    """`None` must mean "that was everything" — otherwise a client pages forever."""
    page, token = models_ep.paginate(["a", "b", "c"], None, 10)
    assert (page, token) == (["a", "b", "c"], None)


def test_a_truncated_listing_hands_back_its_last_name() -> None:
    page, token = models_ep.paginate(["a", "b", "c"], None, 2)
    assert page == ["a", "b"]
    assert token == "b", "the cursor must be the last name served, or the next page overlaps or skips"


def test_the_cursor_resumes_after_it() -> None:
    """Strictly after, not at — a cursor that re-serves its own row duplicates a model per page."""
    page, token = models_ep.paginate(["a", "b", "c"], "b", 10)
    assert page == ["c"]
    assert token is None


def test_the_helper_is_the_one_the_table_listing_uses() -> None:
    """A second cursor implementation in one service is a second thing to keep in step.

    Asserted on identity rather than behaviour: two functions can agree today and drift tomorrow.
    """
    from catalog.api.v1.endpoints import tables as tables_ep

    assert models_ep.paginate is tables_ep.paginate, "the models listing has its own cursor implementation"
