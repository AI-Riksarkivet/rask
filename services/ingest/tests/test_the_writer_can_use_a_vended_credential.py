"""The client-direct writer is what vending was BUILT for, and it used a static key.

`#2` in the catalog's own docs describes this exact flow: the client writes fragments straight to
object storage with scoped credentials and commits the metadata through `POST /{id}/commit`, so no data
byte transits the catalog. The vending door exists to hand out those credentials — and until now it had
no consumers at all, so every client-direct write signed with a long-lived key that reaches the whole
bucket.

`write_unit_fragments` passed NO storage options, so `write_fragments` fell through to the ambient
credential chain: whatever the pod happens to hold, for as long as it holds it. A vended credential is
scoped to one table prefix and expires in 900 seconds — proven enforced on RustFS 2026-09-03 (a
credential vended for one table read it, and was refused on another with 403 AccessDenied).

The seam must degrade rather than fail: `mode_b` vends nothing by design, and a deployment on it must
keep writing exactly as before. So absent options mean "use the ambient chain", which is the behaviour
this replaces — the change adds a capability without taking one away.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest


def _batch(ids: list[int]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "v": pa.array([str(i) for i in ids], pa.string())})


def test_the_writer_forwards_vended_storage_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The options must reach `write_fragments`, or the credential is vended and then ignored — which
    would read as working while every byte is still written with the ambient key."""
    import lance

    from ingest import lander

    seen: dict[str, Any] = {}
    real = lance.fragment.write_fragments

    def spy(batch: Any, uri: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        kwargs.pop("storage_options", None)  # tmp_path is local; the options are what is under test
        return real(batch, uri, **kwargs)

    monkeypatch.setattr(lance.fragment, "write_fragments", spy)
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(_batch([0]), uri, **lander.CREATION_FLAGS)

    vended = {"access_key_id": "AK", "secret_access_key": "SK", "session_token": "TOK", "allow_http": "true"}
    lander.write_unit_fragments(uri, _batch([1, 2]), storage_options=vended)

    assert seen.get("storage_options") == vended


def test_no_options_still_writes_exactly_as_before(tmp_path: Any) -> None:
    """`mode_b` vends nothing, and a deployment on it must be untouched by this. Absent options mean the
    ambient credential chain — the behaviour being replaced, kept as the fallback."""
    import lance

    from ingest import lander

    uri = str(tmp_path / "t.lance")
    lance.write_dataset(_batch([0]), uri, **lander.CREATION_FLAGS)
    written = lander.write_unit_fragments(uri, _batch([1, 2]))
    assert written and isinstance(written[0], str)


# --- the client half: asking the catalog for the credential --------------------------------------


def test_the_catalog_client_vends_scoped_options() -> None:
    """`mode="direct"` carries the credential; the client hands back its storage options verbatim."""
    import httpx
    import respx

    from ingest.catalog_service import CatalogServiceClient

    schema = pa.schema([("id", pa.int64())])
    client = CatalogServiceClient(schema, base_url="http://catalog:2333", token="t")
    with respx.mock:
        respx.post("http://catalog:2333/v1/table/ns$ds/credentials").mock(
            return_value=httpx.Response(
                200,
                json={
                    "mode": "direct",
                    "credentials": {
                        "storage_options": {"access_key_id": "AK", "session_token": "TOK", "allow_http": "true"},
                        "expires_at_millis": 1_788_462_943_000,
                    },
                },
            )
        )
        vended = client.vend_storage_options("ns", "ds", tier="write")
        assert vended is not None
        assert vended.options == {"access_key_id": "AK", "session_token": "TOK", "allow_http": "true"}
        # The EXPIRY rides with the credential: only the vend knows it, and a cache told to guess it
        # would either re-vend on every write or hold a credential past its death.
        assert vended.expires_at_millis == 1_788_462_943_000


def test_server_mediated_vends_nothing_and_that_is_not_an_error() -> None:
    """`mode_b` is a supported posture, not a failure: it answers `server_mediated` with no credential,
    and the writer must fall back to the ambient chain rather than refuse to write."""
    import httpx
    import respx

    from ingest.catalog_service import CatalogServiceClient

    client = CatalogServiceClient(pa.schema([("id", pa.int64())]), base_url="http://catalog:2333", token="t")
    with respx.mock:
        respx.post("http://catalog:2333/v1/table/ns$ds/credentials").mock(
            return_value=httpx.Response(200, json={"mode": "server_mediated", "credentials": None})
        )
        assert client.vend_storage_options("ns", "ds", tier="write") is None


def test_a_vending_failure_REFUSES_rather_than_signing_with_the_storage_root() -> None:
    """A vend that ERRORS is not the same as one that offers nothing, and this asserted that it was.

    "Falling back is strictly no worse" held only while the ambient credential was an unremarkable
    key. It is the RustFS ROOT pair — measured inside the running pod 2026-09-08,
    `AWS_ACCESS_KEY_ID=minioadmin` — so the degrade signed the run's bytes with the widest credential
    in the estate, at INFO, counted by nothing. The owner's standing rule is "never a fallback"; the
    escape stays reachable for an operator who names it (`allow_ambient_fallback`), so the refusal is
    about the AMBIGUITY, not about being open.
    """
    import httpx
    import pytest
    import respx

    from ingest.catalog_service import CatalogServiceClient, VendingUnavailableError

    client = CatalogServiceClient(pa.schema([("id", pa.int64())]), base_url="http://catalog:2333", token="t")
    with respx.mock:
        respx.post("http://catalog:2333/v1/table/ns$ds/credentials").mock(return_value=httpx.Response(503, json={"detail": "vendor down"}))
        with pytest.raises(VendingUnavailableError):
            client.vend_storage_options("ns", "ds", tier="write")
