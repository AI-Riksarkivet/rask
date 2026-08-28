"""One home for the page parameters, and a bound on how deep a caller may go.

open_fastapi-audit — "No shared `PaginationParams` dependency or `Page[Item]` envelope" and "The
estate's only offset-paginated route has no MAX_OFFSET guard and runs an unconditional
`count_rows()`".

`grep -rn 'PaginationParams|Page\\[|build_page|get_pagination'` over `services/` and `packages/`
returned ZERO at HEAD: every paginated route reinvented the wire shape, across eight vocabularies and
seven ceilings. This is the home the finding names — `service-kit` already owns `make_service_app`,
`SettingsDep` and the middleware.

DELIBERATELY SMALL. The reference's `PaginationParams` is page/page_size over SQL, and this estate
paginates over Lance, over cursors and over spec-shaped `page_token`s — one model cannot serve all
three, and the reference itself says to pick the strategy per endpoint. So this ships the OFFSET
strategy only, which is the one the estate actually has a route for; a `KeysetParams` sibling belongs
here the day a second cursor route wants to share one.

WHAT IS NOT IMPLEMENTED, and why, since the finding asks for it: `total = ds.count_rows() if page == 1
else None`. The finding's own verifier withdraws the cost model behind it — an unfiltered Lance
`count_rows()` is answered from fragment metadata, not a table scan, so it does not carry the
`SELECT COUNT(*)` cost the reference warns about, and the "pins a threadpool worker for a full scan"
claim is "asserted, not demonstrated". Making `total` optional on every page but the first would
degrade the envelope for an unproven saving, so it is not done.
"""

from __future__ import annotations

import pytest

from service_kit.exceptions import ValidationError
from service_kit.pagination import MAX_OFFSET, PaginationParams, build_page, guard_offset


def test_the_offset_is_derived_not_restated() -> None:
    """Two copies of `(page - 1) * page_size` is how the two drift."""
    assert PaginationParams(page=3, page_size=24).offset == 48
    assert PaginationParams(page=1, page_size=24).offset == 0


def test_a_deep_page_is_REFUSED_not_served() -> None:
    """The reference's `get_pagination_guarded`: forbid deep pages so an adversarial query cannot
    walk the store. `page: Query(ge=1)` alone bounds nothing — `?page=1000000` was legal."""
    with pytest.raises(ValidationError) as caught:
        guard_offset(PaginationParams(page=1_000_000, page_size=100))
    assert "cursor" in str(caught.value).lower() or "keyset" in str(caught.value).lower(), (
        "the refusal must point at the alternative, or the caller has no way forward: " + str(caught.value)
    )


def test_the_boundary_page_is_still_served() -> None:
    """An off-by-one here silently amputates the last legal page."""
    page_size = 100
    last_legal = MAX_OFFSET // page_size
    guard_offset(PaginationParams(page=last_legal, page_size=page_size))
    with pytest.raises(ValidationError):
        guard_offset(PaginationParams(page=last_legal + 2, page_size=page_size))


def test_the_envelope_computes_what_clients_should_not() -> None:
    """`pages`, `has_next`, `has_prev` come from the server — the reference's anti-pattern is a
    client doing this arithmetic against incomplete data."""
    page = build_page([1, 2, 3], total=7, params=PaginationParams(page=1, page_size=3))
    assert (page.total, page.pages, page.has_next, page.has_prev) == (7, 3, True, False)

    last = build_page([7], total=7, params=PaginationParams(page=3, page_size=3))
    assert (last.pages, last.has_next, last.has_prev) == (3, False, True)


def test_an_empty_result_does_not_claim_a_page() -> None:
    """Ceil division on zero is where this kind of helper usually reports `pages=0, has_next=True`."""
    empty = build_page([], total=0, params=PaginationParams(page=1, page_size=20))
    assert (empty.pages, empty.has_next, empty.has_prev) == (0, False, False)
