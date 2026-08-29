"""``POST /v1/table/{id}/describe`` must accept the spec's request BODY.

The Lance Namespace spec (``lance_docs/ns_catalog/spec.yaml``, ``lance_docs/namespace.md`` §DescribeTable)
puts only ``with_table_uri`` / ``load_detailed_metadata`` / ``check_declared`` in the query; ``version``,
``tag`` and ``vend_credentials`` travel in a ``DescribeTableRequest`` BODY, and the generated client
(``lance_namespace_urllib3_client.api.table_api._describe_table_serialize``) sends them that way. The route
bound all six as bare scalars, so FastAPI made every one a QUERY param and the body was dropped on the
floor: ``lance_ray.utils`` calls ``namespace.describe_table(DescribeTableRequest(id=...))`` and merges
``describe_response.storage_options``, so a spec-conformant client got a location and NO credentials, and any
version pin read latest instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from catalog.api.dependencies import get_vendor
from catalog.core.vending import VendedCredentials
from catalog.services import dataplane
from fastapi.testclient import TestClient
from lance_namespace import DescribeTableResponse


_LOCATION = "s3://lance-catalog/db$t"


@pytest.fixture
def vendor(client: TestClient, fake_ns: MagicMock) -> Iterator[MagicMock]:
    fake_ns.describe_table.return_value = DescribeTableResponse(location=_LOCATION)
    vend = MagicMock()
    vend.vend.return_value = VendedCredentials(storage_options={"aws_access_key_id": "AK"})
    client.app.dependency_overrides[get_vendor] = lambda: vend
    yield vend
    client.app.dependency_overrides.pop(get_vendor, None)


def _sent(fake_ns: MagicMock) -> Any:
    """The DescribeTableRequest the ROUTE handed the backend — the FIRST call.

    Not ``call_args``: with ``load_detailed_metadata`` set, the route's metadata backfill re-enters the
    dataplane, which describes the table again to resolve its location, so the last call is not ours.
    """
    return fake_ns.describe_table.call_args_list[0][0][0]


def test_body_vend_credentials_and_version_are_honoured(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    # Exactly what a generic client sends: everything in the body, nothing in the query.
    resp = client.post("/v1/table/db$t/describe", json={"vend_credentials": True, "version": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["storage_options"] == {"aws_access_key_id": "AK"}
    assert vendor.vend.call_args.kwargs["tier"] == "read"  # describe is gated on the reader rung only
    assert _sent(fake_ns).version == 1


def test_body_flags_reach_the_backend(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    # The three fields the spec DOES put in the query are still legal in the body for a non-REST client.
    resp = client.post("/v1/table/db$t/describe", json={"with_table_uri": True, "load_detailed_metadata": True, "check_declared": True})
    assert resp.status_code == 200, resp.text
    sent = _sent(fake_ns)
    assert (sent.with_table_uri, sent.load_detailed_metadata, sent.check_declared) == (True, True, True)


def test_the_query_form_still_works(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    # rask's own callers (lakehouse's catalog.remote.ts, the e2e suites) send query params and no body.
    resp = client.post("/v1/table/db$t/describe?vend_credentials=true&version=3&with_table_uri=true")
    assert resp.status_code == 200, resp.text
    assert resp.json()["storage_options"] == {"aws_access_key_id": "AK"}
    sent = _sent(fake_ns)
    assert (sent.version, sent.with_table_uri) == (3, True)


def test_an_absent_body_field_does_not_clobber_its_query_value(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    # A body carrying only `version` must not reset `with_table_uri`/`check_declared` to the model's
    # False defaults — presence in the JSON is what decides, not the parsed model's value.
    resp = client.post("/v1/table/db$t/describe?with_table_uri=true", json={"version": 2})
    assert resp.status_code == 200, resp.text
    sent = _sent(fake_ns)
    assert (sent.version, sent.with_table_uri) == (2, True)


def test_the_body_wins_where_both_are_sent(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    resp = client.post("/v1/table/db$t/describe?version=9&vend_credentials=false", json={"version": 4, "vend_credentials": True})
    assert resp.status_code == 200, resp.text
    assert _sent(fake_ns).version == 4
    assert resp.json()["storage_options"] == {"aws_access_key_id": "AK"}


def test_a_body_tag_is_resolved_like_a_query_tag(client: TestClient, fake_ns: MagicMock, vendor: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    # The tag store is a real dataset read, which a MagicMock namespace cannot serve; the point here is
    # that a BODY tag reaches the same resolution the query tag has always gone through.
    monkeypatch.setattr(dataplane, "get_tag_version", lambda ns, so, req: MagicMock(version=7))
    resp = client.post("/v1/table/db$t/describe", json={"tag": "stable"})
    assert resp.status_code == 200, resp.text
    assert _sent(fake_ns).version == 7
    assert _sent(fake_ns).tag is None  # resolved HERE; the backend ignores a describe-request tag


def test_a_body_tag_and_a_query_version_still_conflict(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    resp = client.post("/v1/table/db$t/describe?version=2", json={"tag": "stable"})
    assert resp.status_code == 400, resp.text


def test_a_body_id_that_disagrees_with_the_path_is_refused(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    resp = client.post("/v1/table/db$t/describe", json={"id": ["other", "table"]})
    assert resp.status_code == 400, resp.text


def test_a_matching_body_id_is_accepted(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    resp = client.post("/v1/table/db$t/describe", json={"id": ["db", "t"]})
    assert resp.status_code == 200, resp.text


def test_a_non_main_branch_is_refused_rather_than_answered_off_main(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    # The body carries a `branch` this door cannot honour: the native describe takes no branch selector and
    # the branch-aware read lives in the dataplane. Answering off main anyway would hand a caller who pinned
    # a branch a confident, wrong answer — the failure mode the refusal exists to prevent — so this must stay
    # a 400 and not drift back into a silent success.
    resp = client.post("/v1/table/db$t/describe", json={"branch": "dev"})
    assert resp.status_code == 400, resp.text
    assert "branch" in resp.text


def test_naming_the_main_branch_explicitly_is_accepted(client: TestClient, fake_ns: MagicMock, vendor: MagicMock) -> None:
    # The spec says an unspecified branch means main, so a body naming main asks for exactly what this door
    # already does — refusing it would reject a spec-conformant client for agreeing with us.
    resp = client.post("/v1/table/db$t/describe", json={"branch": "main"})
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------------------------------
# Shape pin: the served contract must keep BOTH doors, so a future edit cannot silently drop the body
# again (that is the whole defect) or move the spec's three query fields out of the query.
# --------------------------------------------------------------------------------------------------


def test_the_served_route_declares_the_spec_body_and_the_spec_query_params(client: TestClient) -> None:
    op = client.app.openapi()["paths"]["/v1/table/{id}/describe"]["post"]
    body_schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert "DescribeTableRequest" in str(body_schema), body_schema
    query = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
    assert {"with_table_uri", "load_detailed_metadata", "check_declared"} <= query
