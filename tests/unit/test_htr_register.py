"""The register seam (#88 step 5) — respx at the catalog boundary, failure posture pinned."""

from __future__ import annotations

import httpx
import pytest
import respx
from medallion.services.htr_register import RegisterError, register_gold_table, relative_location


CATALOG = "http://catalog.test"
ROOT = "s3://lance-catalog"


def test_relative_location_is_derived_or_refused() -> None:
    """The #75 lesson as a unit: the dir backend refuses absolute URIs, so a to_uri outside the
    catalog root cannot be registered — refuse HERE naming both, never let the catalog 400 opaquely."""
    assert relative_location(f"{ROOT}/medallion/gold-htr", ROOT) == "medallion/gold-htr"
    with pytest.raises(RegisterError, match="not under the catalog root"):
        relative_location("s3://other-bucket/medallion/gold-htr", ROOT)
    with pytest.raises(RegisterError, match="not under the catalog root"):
        relative_location(f"{ROOT}/medallion/gold-htr", "")


@respx.mock
def test_registration_sends_the_relative_location_and_segments() -> None:
    ns = respx.post(f"{CATALOG}/v1/namespace/gold/create").mock(return_value=httpx.Response(200, json={}))
    route = respx.post(f"{CATALOG}/v1/table/gold$htr/register").mock(return_value=httpx.Response(200, json={}))

    register_gold_table(catalog_url=CATALOG, catalog_root=ROOT, table_id="gold$htr", to_uri=f"{ROOT}/medallion/gold-htr")

    body = route.calls.last.request.read()
    assert b'"location": "medallion/gold-htr"' in body or b'"location":"medallion/gold-htr"' in body
    assert b'"gold"' in body and b'"htr"' in body  # id as SEGMENTS, split on the delimiter
    # Creates are top-down (live-verified: register into an absent namespace → 404 require_parent),
    # so the lane ensures its tier namespace FIRST — the cascade owns its tiers.
    assert ns.call_count == 1


@respx.mock
def test_a_409_is_already_governed_not_a_failure() -> None:
    """Every redelivery after the first lands here — an idempotent stage must not look failed."""
    respx.post(f"{CATALOG}/v1/namespace/gold/create").mock(return_value=httpx.Response(409, text="exists"))
    respx.post(f"{CATALOG}/v1/table/gold$htr/register").mock(return_value=httpx.Response(409, text="exists"))

    register_gold_table(catalog_url=CATALOG, catalog_root=ROOT, table_id="gold$htr", to_uri=f"{ROOT}/medallion/gold-htr")


@respx.mock
def test_any_other_refusal_fails_the_stage() -> None:
    respx.post(f"{CATALOG}/v1/namespace/gold/create").mock(return_value=httpx.Response(409, text="exists"))
    respx.post(f"{CATALOG}/v1/table/gold$htr/register").mock(return_value=httpx.Response(403, text="denied"))

    with pytest.raises(RegisterError, match="HTTP 403"):
        register_gold_table(catalog_url=CATALOG, catalog_root=ROOT, table_id="gold$htr", to_uri=f"{ROOT}/medallion/gold-htr")


def test_an_unset_catalog_url_names_the_env_var() -> None:
    with pytest.raises(RegisterError, match="MEDALLION_CATALOG_URL"):
        register_gold_table(catalog_url="", catalog_root=ROOT, table_id="gold$htr", to_uri=f"{ROOT}/x")


@respx.mock
def test_the_bearer_rides_when_given() -> None:
    respx.post(f"{CATALOG}/v1/namespace/gold/create").mock(return_value=httpx.Response(409, text="exists"))
    route = respx.post(f"{CATALOG}/v1/table/gold$htr/register").mock(return_value=httpx.Response(200, json={}))

    register_gold_table(catalog_url=CATALOG, catalog_root=ROOT, table_id="gold$htr", to_uri=f"{ROOT}/medallion/gold-htr", token="mover-jwt")

    assert route.calls.last.request.headers["authorization"] == "Bearer mover-jwt"
