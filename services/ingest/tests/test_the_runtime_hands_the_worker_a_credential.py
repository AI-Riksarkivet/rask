"""The last link: the runtime builds the credential cache and hands it to the worker.

Every piece exists — the vending door signs (01222590) and returns a usable credential (3cb13c88),
`write_unit_fragments` accepts storage options (8cbab947), the cache holds and refreshes one per table
(ffe9c732), and the chunk carries the identity vending is keyed by (0cb6812c). None of it reaches the
write until the runtime connects them, so until this lands every client-direct byte is still signed by
the ambient credential.

TWO CONDITIONS DECIDE WHETHER A CREDENTIAL IS EVEN AVAILABLE, and neither is a failure:

* The seam has two halves. `LocalCatalog` has no vending door at all — it is the no-catalog dev shape —
  so asking it would be a `AttributeError` rather than a degradation. Only the service half can vend.
* `mode_b` vends nothing by design, which the cache already handles by remembering the refusal.

So the provider is built only when the seam can answer, and the worker is handed `None` otherwise —
which resolves to the ambient credential, i.e. exactly today's behaviour.
"""

from __future__ import annotations

from typing import Any


def test_a_service_catalog_yields_a_provider_that_asks_for_THIS_table() -> None:
    """The credential is scoped to one table prefix, so the provider must name the chunk's own table —
    a shared or wrong id produces exactly the 403 the scoping is for."""
    import pyarrow as pa

    from ingest.catalog_service import CatalogServiceClient, VendedCredential
    from ingest.runtime import write_options_for

    asked: list[tuple[str, str]] = []

    class _Client(CatalogServiceClient):
        def vend_storage_options(self, namespace: str, dataset: str, *, tier: str = "write") -> Any:
            asked.append((namespace, dataset))
            return VendedCredential(options={"access_key_id": "AK"}, expires_at_millis=None)

    provider = write_options_for(_Client(pa.schema([("id", pa.int64())])), namespace="bronze", dataset="pages")
    assert provider is not None
    assert provider() == {"access_key_id": "AK"}
    assert asked == [("bronze", "pages")]


def test_a_chunk_with_no_namespace_gets_no_provider() -> None:
    """A pre-upgrade chunk replayed by this build carries an empty namespace (the field defaults for
    exactly that reason). Composing a table id from it would ask for `$dataset` — an object that does
    not exist — so the run falls back to the ambient credential rather than 403-ing every write."""
    import pyarrow as pa

    from ingest.catalog_service import CatalogServiceClient
    from ingest.runtime import write_options_for

    assert write_options_for(CatalogServiceClient(pa.schema([("id", pa.int64())])), namespace="", dataset="ds") is None
