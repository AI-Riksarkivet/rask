"""A medallion service authenticates to the catalog as the service account its projected token names ([[LH-220]], D1).

The kubelet projects a token for audience `rask-catalog` into this pod, and the catalog verifies it offline
and maps the account to the subject its grants name. That token is the whole credential: no header claims
a name, and the `dapr-api-token` daprd stamps on an invocation names nobody.

THE KUBELET REPLACES THAT TOKEN at 80% of its 600 s lifetime (measured 2026-10-02: rotated at age 515 s),
so a token held for the life of the process is refused after ten minutes. Each call reads its file afresh,
and one that cannot be read is not sent anonymously.

THE SEAM UNDER TEST IS `publish_stage_output`, the one catalog call whose route is a single request; the
credential is built by the one function every call in `catalog_register` shares.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from medallion.core.config import MedallionSettings
from medallion.services.catalog_register import RegisterError, publish_stage_output


CATALOG = "http://catalog.test"


def _route() -> respx.Route:
    return respx.post(f"{CATALOG}/management/v1/table/silver$features/publish").mock(return_value=httpx.Response(200, json={"published": True}))


def _publish() -> None:
    publish_stage_output(
        catalog_url=CATALOG,
        table_id="silver$features",
        version=2,
        key_column="id",
        identity_token_file=MedallionSettings().catalog_identity_token_file,
    )


@respx.mock
def test_the_projected_token_is_the_whole_credential() -> None:
    route = _route()

    _publish()

    sent = route.calls.last.request.headers
    assert sent["authorization"] == f"Bearer {Path(MedallionSettings().catalog_identity_token_file).read_text()}"
    assert "x-lance-service-identity" not in sent, "no header claims a name"
    assert "dapr-api-token" not in sent, "the app token proves a sidecar delivered the call and names nobody"


@respx.mock
def test_a_token_the_kubelet_rotated_is_the_one_sent_next() -> None:
    route = _route()
    token_file = Path(MedallionSettings().catalog_identity_token_file)
    before = f"Bearer {token_file.read_text()}"

    _publish()
    token_file.write_text("the-rotated-token")
    _publish()

    assert [call.request.headers["authorization"] for call in route.calls] == [before, "Bearer the-rotated-token"]


@respx.mock
def test_a_token_that_cannot_be_read_sends_nothing() -> None:
    """An anonymous call would be refused one service away, for a reason invisible from here. The stage
    gets the error and RETRYs exactly as it does for an unreachable catalog."""
    Path(MedallionSettings().catalog_identity_token_file).unlink()

    with pytest.raises(RegisterError, match="identity token"):
        _publish()

    assert respx.calls.call_count == 0
