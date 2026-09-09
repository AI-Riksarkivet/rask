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
