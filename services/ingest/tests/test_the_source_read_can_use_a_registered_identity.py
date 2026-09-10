"""An estate-internal source read can be signed by a registered identity, not only the ambient one.

the lakehouse register, row H8 (drained 2026-09-10; in git history). MEASURED INSIDE THE RUNNING POD 2026-09-08: `rask-ingest` holds
`AWS_ACCESS_KEY_ID=rustfsadmin` — the RustFS ROOT credential — and a pyarrow filesystem built from its
ambient environment listed 106 buckets, the whole estate. Its governed WRITES already sign with a
catalog-vended STS credential, so the root pair's only standing use is the SOURCE READ.

WHY NO REGISTRATION COULD FIX THAT. `resolve_source_connection` short-circuits before it ever consults
the registry:

    if not declared or declared == normalise_endpoint(configured_endpoint()):
        return SourceConnection()      # ambient env; `store_for_endpoint` never called

and every measured read target is an ESTATE bucket on the deployment's own endpoint
(`s3://lance-catalog/media-src/batch` 126, `s3://images-batch` 15, `s3://acme-bucket` 14 …). So case 1
took every read, case 2's Dapr-secret-store path was unreachable for them, and the operator had no way
to give ingest a narrower identity for its own estate — the machinery existed and could not be aimed.

The short-circuit's REASON is sound and is kept: refusing a run that names the deployment's own
endpoint would be "a refusal with no hazard behind it". What changes is that naming the default is no
longer the same as declining a registered identity for it.

THIS CHANGES NOTHING UNTIL A STORE IS REGISTERED — with an empty registry the ambient default is
returned exactly as before, which the second test pins.
"""

from __future__ import annotations

import json

import pytest

from ingest.objectstore import resolve_source_connection


_ENDPOINT = "http://rustfs.example:9000"


def _register(monkeypatch: pytest.MonkeyPatch, *stores: dict[str, object]) -> None:
    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", _ENDPOINT)
    monkeypatch.setenv("RASK_STORES", json.dumps(list(stores)))


def test_a_store_registered_for_the_OWN_endpoint_supplies_its_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the row: an operator can aim a narrower identity at an estate bucket.

    The credential comes from the Dapr secret store (`_store_credentials`, fail-closed), which is a
    sanctioned path; the ambient ROOT pair is not.
    """
    _register(monkeypatch, {"name": "media-src", "bucket": "lance-catalog", "role": "raw", "secret": "media-src-creds"})
    monkeypatch.setattr("ingest.objectstore._store_credentials", lambda secret: (f"ak-{secret}", f"sk-{secret}"))

    conn = resolve_source_connection(None, "lance-catalog")

    assert conn.access_key == "ak-media-src-creds", "a registered store's identity must be used for an estate bucket"
    assert conn.secret_key == "sk-media-src-creds"


def test_an_EMPTY_registry_still_yields_the_ambient_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The half that makes this safe to land before anything is registered: with no store, the answer
    is byte-identical to before — no endpoint, no credentials, the env chain."""
    _register(monkeypatch)

    conn = resolve_source_connection(None, "lance-catalog")

    assert conn.is_estate_default and conn.access_key is None and conn.secret_key is None


def test_a_store_for_a_DIFFERENT_bucket_does_not_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both halves must match. A bucket name alone is exactly the collision the registry exists to
    disambiguate, and borrowing another bucket's identity would read the wrong bytes or none."""
    _register(monkeypatch, {"name": "other", "bucket": "images-batch", "role": "raw", "secret": "other-creds"})
    monkeypatch.setattr("ingest.objectstore._store_credentials", lambda secret: (f"ak-{secret}", f"sk-{secret}"))

    conn = resolve_source_connection(None, "lance-catalog")

    assert conn.is_estate_default and conn.access_key is None
