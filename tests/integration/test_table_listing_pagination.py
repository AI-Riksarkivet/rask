"""A spec listing pages keyset-style, over the final list, and a `limit` past the cap, or none, is answered a page of the cap.

`paginate` cuts the final sorted and deduped list, never the native call (#141): a native-level limit would truncate
the root listing before the walk and the bound seeds merged in, dropping tables from every page.

The spec's `PageLimit` is "an inclusive upper bound of the number of results" with no maximum, and a server that cannot
return every item in one response answers a `page_token` (`lance_docs/ns_catalog/spec.yaml` PageToken, PageLimit), so a
`limit` above the catalog's cap is a valid request answered the cap and a token that continues it ([[LH-546]]). A request
naming no `limit` is answered the same page, so no listing answers the whole estate in one response ([[LH-576]] E10a).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import ListNamespacesResponse

from catalog.api.pagination import MAX_LIST_LIMIT, paginate


NAMES = ["lance_catalog$a$one", "lance_catalog$b$two", "lance_catalog$c$three", "lance_catalog$d$four", "lance_catalog$e$five"]


def test_exact_fit_ends_pagination() -> None:
    """limit == remaining ⇒ complete, so the cursor must be None — a token here loops forever."""
    page, token = paginate(list(NAMES), None, 5)
    assert page == NAMES
    assert token is None


@pytest.mark.parametrize("asked", [pytest.param({"limit": 5 * MAX_LIST_LIMIT}, id="past-the-cap"), pytest.param({}, id="no-limit")])
def test_a_limit_past_the_cap_or_none_is_answered_the_cap_and_a_token_that_continues_it(client: TestClient, fake_ns: MagicMock, asked: dict[str, int]) -> None:
    children = sorted(f"child{i:05d}" for i in range(MAX_LIST_LIMIT + 500))
    fake_ns.list_namespaces.side_effect = lambda _request: ListNamespacesResponse(namespaces=list(children))

    first = client.get("/v1/namespace/lance_catalog$a/list", params=asked)
    assert first.status_code == 200, first.text
    assert first.json()["namespaces"] == children[:MAX_LIST_LIMIT]

    rest = client.get("/v1/namespace/lance_catalog$a/list", params={**asked, "page_token": first.json()["page_token"]})
    assert rest.status_code == 200, rest.text
    assert rest.json()["namespaces"] == children[MAX_LIST_LIMIT:]
    assert rest.json().get("page_token") is None
