"""A read from the Dapr secret store fails fast on a refusal and retries a sidecar that is not up yet."""

from __future__ import annotations

import httpx
import pytest
import respx

from lakehouse.governed.secrets import fetch_dapr_secret


SECRET = "http://localhost:3500/v1.0/secrets/store/key"


@pytest.mark.parametrize(
    ("answers", "bundle", "attempts"),
    [
        pytest.param([httpx.Response(403)], {}, 1, id="a-refusal-is-asked-once"),
        pytest.param([httpx.ConnectError, httpx.ConnectError, httpx.Response(200, json={"k": "v"})], {"k": "v"}, 3, id="a-sidecar-not-up-is-asked-again"),
    ],
)
def test_fetch_dapr_secret_asks_again_only_what_waiting_can_heal(
    answers: list[httpx.Response | type[Exception]], bundle: dict[str, str], attempts: int
) -> None:
    """A 4xx is misconfiguration (a bad store name, a denied scope) that waiting cannot heal, so it is asked once rather
    than spending the boot's retry budget; a refused connection is a sidecar still starting, asked again until it answers."""
    with respx.mock:
        route = respx.get(SECRET).mock(side_effect=answers)

        fetched = fetch_dapr_secret("store", "key", dapr_http_port=3500, retries=5, backoff=0.001)

    assert (fetched, route.call_count) == (bundle, attempts)
