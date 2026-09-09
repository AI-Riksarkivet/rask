"""A create replayed with the same `Idempotency-Key` answers once and executes once.

§ Q17-32. `dapr-resiliency.yaml` retries a write door's failure, and until now the catalog's 91 write
routes carried no key on any of them — so a replay of `POST /v1/table/{id}/create` re-entered
`create_governed_table`: under `mode=Overwrite` that DROPS AND REWRITES the dataset, strips and
re-seeds the ACL, and emits a second `table_created`; under `mode=Create` it answers AlreadyExists, so
a fully successful create surfaces as a 409 nobody can tell from a name collision.

THE KEY IS OPTIONAL AND THE FIRST TEST HERE IS THAT IT STAYS SO. The Lance Namespace spec defines no
such header and a stock client must work with no rask SDK, so a door that required it would fail
conformance on 54 routed operations — a worse defect than the one being fixed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest


if TYPE_CHECKING:
    from fastapi.testclient import TestClient

ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue().to_pybytes())


def _rows(n: int = 3) -> bytes:
    return _ipc(pa.table({"id": pa.array(range(n), pa.int64())}))


def _versions(client: TestClient, tid: str) -> Any:
    return client.post(f"/v1/table/{tid}/describe", json={}).json()


def _keyed_post_paths(app: Any) -> tuple[set[str], set[str]]:
    """`(paths declaring Idempotency-Key, paths REQUIRING it)`, read off the generated OpenAPI.

    NOT off `app.routes`: the catalog mounts its versioned routers, so a walk over that finds ONE
    APIRoute and a roster gate built on it would pass by finding nothing — which is precisely the
    failure a roster gate exists to prevent. `app.openapi()` is the same document a stock Lance client
    reads, which also makes this the right surface for a claim about spec conformance.
    """
    spec = app.openapi()
    declared: set[str] = set()
    required: set[str] = set()
    for path, ops in spec.get("paths", {}).items():
        for param in (ops.get("post") or {}).get("parameters", []):
            if param.get("in") == "header" and param.get("name") == "Idempotency-Key":
                declared.add(path)
                if param.get("required"):
                    required.add(path)
    return declared, required


class TestTheKeyIsOptional:
    def test_a_client_that_sends_no_key_creates_exactly_as_before(self, real_ns_client: TestClient) -> None:
        """The spec conformance guard. A stock Lance client sends no `Idempotency-Key`."""
        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200

        response = real_ns_client.post("/v1/table/db$plain/create", content=_rows(), headers=ARROW_STREAM)

        assert response.status_code == 200, response.text


class TestAReplayIsAnsweredNotReExecuted:
    def test_the_same_key_returns_the_first_answer_and_leaves_the_table_alone(self, real_ns_client: TestClient) -> None:
        """The defect this closes: the second call must not re-enter the create path at all."""
        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        headers = {**ARROW_STREAM, "Idempotency-Key": "retry-1"}

        first = real_ns_client.post("/v1/table/db$keyed/create", content=_rows(3), headers=headers)
        assert first.status_code == 200, first.text
        after_first = _versions(real_ns_client, "db$keyed")

        # A REPLAY CARRYING DIFFERENT BYTES. The key says "this is the same request"; honouring the
        # body instead would let a retry silently overwrite the table with something else.
        second = real_ns_client.post("/v1/table/db$keyed/create?mode=Overwrite", content=_rows(99), headers=headers)

        assert second.status_code == 200, second.text
        assert second.json() == first.json(), "a replay must answer with the first attempt's response"
        assert _versions(real_ns_client, "db$keyed") == after_first, "the replay re-executed the create"

    def test_a_DIFFERENT_key_is_a_different_request(self, real_ns_client: TestClient) -> None:
        """Convergence must not become deduplication of unrelated calls."""
        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        first = real_ns_client.post("/v1/table/db$two/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": "a"})
        assert first.status_code == 200, first.text

        second = real_ns_client.post("/v1/table/db$two/create?mode=Overwrite", content=_rows(5), headers={**ARROW_STREAM, "Idempotency-Key": "b"})

        assert second.status_code == 200, second.text


class TestTheKeyIsBoundToTheOperation:
    def test_a_key_already_used_on_ANOTHER_endpoint_is_a_400_not_a_replay(self, real_ns_client: TestClient) -> None:
        """Replaying across endpoints would hand this caller another operation's response body. The
        spec's own `InvalidInput` says the caller made a correctable mistake."""
        from catalog.api.idempotency import _scope  # noqa: PLC0415 — the scope derivation under test
        from service_kit.lakehouse import idempotency

        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        settings = real_ns_client.app.state.settings  # type: ignore[attr-defined]
        idempotency.claim(
            settings.registry_root,
            settings.storage_options(),
            scope=_scope(None),
            key="crossed",
            endpoint="POST /v1/table/{id}/drop",
            now=1000.0,
        )
        idempotency.record_outcome(settings.registry_root, settings.storage_options(), scope=_scope(None), key="crossed", status=200, body={})

        response = real_ns_client.post("/v1/table/db$x/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": "crossed"})

        assert response.status_code == 400, response.text


class TestTheHeaderIsConstrainedAtTheDoor:
    @pytest.mark.parametrize("bad", ["", "a" * 65, "has space", "../escape"])
    def test_a_key_that_is_not_a_plain_bounded_token_is_refused(self, real_ns_client: TestClient, bad: str) -> None:
        """It becomes part of an object key. Refused at the header AND at the seam — the door's 422 is
        the better error, and the seam's `ValueError` is what protects a second caller of the module."""
        response = real_ns_client.post("/v1/table/db$y/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": bad})

        assert response.status_code == 422, response.text


class TestTheDestructiveDoorsConvergeToo:
    """`drop` and `rename` carry the same amplifier as `create` and a worse consequence.

    A replayed drop re-enters a door that DELETES BYTES (or files a second trash record for an object
    already gone). A replayed rename re-enters one that retires the source id and re-registers the
    destination — and the second pass finds no source, so a rename that SUCCEEDED reports as a failure
    the caller cannot distinguish from a name that never existed.
    """

    def test_a_replayed_drop_answers_once_and_the_second_call_does_not_re_enter(self, real_ns_client: TestClient) -> None:
        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/db$doomed/create", content=_rows(), headers=ARROW_STREAM).status_code == 200
        headers = {"Idempotency-Key": "drop-1"}

        first = real_ns_client.post("/v1/table/db$doomed/drop", headers=headers)
        assert first.status_code == 200, first.text

        # Without convergence this re-enters the door and answers NotFound — a successful drop
        # reported as a failure, which is the create door's 409 problem wearing another status.
        second = real_ns_client.post("/v1/table/db$doomed/drop", headers=headers)

        assert second.status_code == 200, second.text
        assert second.json() == first.json()

    def test_a_replayed_rename_does_not_report_the_success_as_a_missing_source(self, real_ns_client: TestClient) -> None:
        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/db$before/create", content=_rows(), headers=ARROW_STREAM).status_code == 200
        headers = {"Idempotency-Key": "rename-1"}
        body = {"id": ["db", "before"], "new_table_name": "after"}

        first = real_ns_client.post("/v1/table/db$before/rename", json=body, headers=headers)
        assert first.status_code == 200, first.text

        second = real_ns_client.post("/v1/table/db$before/rename", json=body, headers=headers)

        assert second.status_code == 200, second.text
        assert real_ns_client.post("/v1/table/db$after/describe", json={}).status_code == 200, "the destination must still be there"

    def test_each_door_owns_its_key_namespace(self, real_ns_client: TestClient) -> None:
        """One key across create and drop is the cross-endpoint refusal, not a replay — proven here at
        the doors rather than only at the seam, because the endpoint string is what binds them and a
        typo in one would silently merge two operations' keys."""
        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/db$shared/create", content=_rows(), headers={**ARROW_STREAM, "Idempotency-Key": "shared-key"}).status_code == 200

        crossed = real_ns_client.post("/v1/table/db$shared/drop", headers={"Idempotency-Key": "shared-key"})

        assert crossed.status_code == 400, crossed.text


class TestEveryTableDoorThatMintsOrRetiresConverges:
    """`create`/`drop`/`rename` were the first three; these are the rest of the same amplifier.

    Pinned as a ROSTER rather than one test each, because the failure this guards is a door ADDED later
    without the key — and a per-door test cannot notice a door nobody wrote a test for.
    """

    def test_the_full_roster_declares_the_header(self, real_ns_client: TestClient) -> None:
        want = {
            "/v1/table/{id}/create",
            "/v1/table/{id}/drop",
            "/v1/table/{id}/rename",
            "/v1/table/{id}/deregister",
            "/v1/table/{id}/register",
            "/v1/table/{id}/restore",
        }
        keyed, _ = _keyed_post_paths(real_ns_client.app)

        assert want <= keyed, f"these doors mint or retire an id and take no idempotency key: {sorted(want - keyed)}"

    def test_the_key_stays_OPTIONAL_on_every_one_of_them(self, real_ns_client: TestClient) -> None:
        """Requiring it anywhere on the spec surface breaks a stock Lance client, which is a worse
        defect than the replay. Asserted for the whole roster so one door cannot drift alone."""
        _, required = _keyed_post_paths(real_ns_client.app)

        assert not required, f"these doors REQUIRE a header the Lance Namespace spec does not define: {required}"


class TestAVendedCredentialSaysWhenItExpires:
    """§ Q13-4 — the door must put the expiry where a stock client looks.

    Driven through the DOOR and not the vendor: `VendedCredentials` carries `expires_at_millis` as a
    sibling field, and the whole defect was that describe-vend forwarded `storage_options` without
    folding it in. A test over the vendor alone would have passed against the broken door — the exact
    pure-helper-beside-an-untested-shell shape this estate has now paid for twice.
    """

    def test_the_expiry_lands_INSIDE_storage_options(self, real_ns_client: TestClient) -> None:
        from catalog.api.dependencies import get_vendor
        from catalog.core.vending import VendedCredentials

        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/db$vend/create", content=_rows(), headers=ARROW_STREAM).status_code == 200

        class _Vendor:
            def vend(self, *, table_location: str, tier: str, **_: Any) -> VendedCredentials:
                return VendedCredentials(storage_options={"aws_access_key_id": "k"}, expires_at_millis=1_700_000_000_000)

        real_ns_client.app.dependency_overrides[get_vendor] = _Vendor
        try:
            body = real_ns_client.post("/v1/table/db$vend/describe?vend_credentials=true", json={}).json()
        finally:
            real_ns_client.app.dependency_overrides.pop(get_vendor, None)

        options = body.get("storage_options") or {}
        assert options.get("expires_at_millis") == "1700000000000", f"the door dropped the expiry: {options}"

    def test_a_PERMANENT_credential_adds_no_key(self, real_ns_client: TestClient) -> None:
        """The spec's sentence is conditional — *if* the credentials are temporary. Emitting the key
        for a static credential tells a client to refresh something that never expires."""
        from catalog.api.dependencies import get_vendor
        from catalog.core.vending import VendedCredentials

        assert real_ns_client.post("/v1/namespace/db/create", json={}).status_code == 200
        assert real_ns_client.post("/v1/table/db$perm/create", content=_rows(), headers=ARROW_STREAM).status_code == 200

        class _Static:
            def vend(self, *, table_location: str, tier: str, **_: Any) -> VendedCredentials:
                return VendedCredentials(storage_options={"aws_access_key_id": "k"})

        real_ns_client.app.dependency_overrides[get_vendor] = _Static
        try:
            body = real_ns_client.post("/v1/table/db$perm/describe?vend_credentials=true", json={}).json()
        finally:
            real_ns_client.app.dependency_overrides.pop(get_vendor, None)

        assert "expires_at_millis" not in (body.get("storage_options") or {})
