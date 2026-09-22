"""A warehouse reaches a SECOND object store by naming a secret, never by holding one.

[[LH-067]]. The endpoint half shipped: a warehouse record carries an optional `endpoint` and
`build_namespace_for_root` opens it there. The credential half cannot follow the same shape, and the
estate's standing rule is why — material never travels in a record, and a scoped static key is not a
fix. So the record names a REFERENCE and the catalog resolves it at the Dapr secret store door it
already uses for its own S3 secret (`dapr_secret_store` / `dapr_secret_key` / `dapr_secret_s3_field`).

RESOLVED ON THE REQUEST PATH, NOT AT BOOT, and that distinction is load-bearing rather than
incidental. `apply_dapr_secrets` splices the ESTATE's one secret into settings once inside the
lifespan, and its own docstring forbids reuse here: "never call this from a request handler or a
background task", because it mutates a shared `@lru_cache`d object that every later read resolves. A
warehouse's credential is per-RECORD and discovered when a request names that warehouse, so it needs a
holder of its own — cached by reference so a hot path does not re-fetch, and never written back onto
`Settings`.

FAILS CLOSED, on the same rule `fetch_required_secrets` already encodes: when a record names a
reference the store cannot satisfy, the answer is a refusal, never a silent fall-back to the estate's
own key. Falling back would be worse than failing: the write would SUCCEED, against the wrong store,
with the estate's credential — which is precisely the blast radius per-warehouse credentials exist to
contain.

NOTHING SECRET IS READABLE BACK. `WarehouseResponse` may carry the reference — an operator who set it
must be able to confirm what the record says, the `primary`/`endpoint` lesson — and must never carry
the resolved material.
"""

from __future__ import annotations

from typing import Any

import pytest


def test_the_record_field_is_a_reference_and_the_model_has_no_field_for_material() -> None:
    """`extra="forbid"` is what makes "a record cannot hold a credential" a property of the type."""
    from catalog.schemas import WarehouseResponse

    fields = set(WarehouseResponse.model_fields)

    assert "credential_ref" in fields, "a warehouse cannot name the secret that reaches its store"
    forbidden = {"credential", "secret", "secret_key", "aws_secret_access_key", "s3_secret_access_key"}
    assert not (fields & forbidden), f"a warehouse record exposes credential MATERIAL: {sorted(fields & forbidden)}"


def test_the_reference_resolves_through_the_dapr_store_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driven through the resolver, so a change that stops using the sanctioned door turns this red."""
    from catalog.services import warehouse_credentials

    calls: list[tuple[str, str, str]] = []

    def _fetch(store: str, key: str, *, require: str) -> dict[str, str]:
        calls.append((store, key, require))
        # The WHOLE bundle: a credential is a pair, and a bundle carrying only the secret leaves the
        # estate's key id signing with it — SignatureDoesNotMatch, measured live 2026-09-21.
        return {require: "the-second-stores-secret", "aws_access_key_id": "second-store-key-id"}

    monkeypatch.setattr(warehouse_credentials, "fetch_required_secrets", _fetch)
    warehouse_credentials.resolve.cache_clear()

    pair = warehouse_credentials.resolve(store="lance-secrets", ref="wh-eu", field="minio-secret-key")

    assert pair == {"aws_secret_access_key": "the-second-stores-secret", "aws_access_key_id": "second-store-key-id"}
    assert calls == [("lance-secrets", "wh-eu", "minio-secret-key")], f"the resolver did not go through the store door: {calls}"


def test_a_reference_the_store_cannot_satisfy_REFUSES_rather_than_falling_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure that would be worse than an error: succeeding against the wrong store.

    A fall-back to the estate's own key would write the caller's bytes into the estate bucket with the
    estate credential and report success — the exact blast radius per-warehouse credentials exist to
    contain. `fetch_required_secrets` already fails closed; this pins that the resolver does not undo it.
    """
    from catalog.services import warehouse_credentials

    def _fetch(_store: str, _key: str, *, require: str) -> dict[str, str]:
        raise RuntimeError(f"secret {require!r} unavailable — failing closed")

    monkeypatch.setattr(warehouse_credentials, "fetch_required_secrets", _fetch)
    warehouse_credentials.resolve.cache_clear()

    with pytest.raises(RuntimeError, match="failing closed"):
        warehouse_credentials.resolve(store="lance-secrets", ref="missing", field="minio-secret-key")


def test_the_resolution_is_CACHED_so_a_hot_path_does_not_refetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every table open under a warehouse names the same reference; the store is a network hop."""
    from catalog.services import warehouse_credentials

    fetches: list[str] = []

    def _fetch(_store: str, key: str, *, require: str) -> dict[str, str]:
        fetches.append(key)
        return {require: "s", "aws_access_key_id": "k"}

    monkeypatch.setattr(warehouse_credentials, "fetch_required_secrets", _fetch)
    warehouse_credentials.resolve.cache_clear()

    for _ in range(5):
        warehouse_credentials.resolve(store="lance-secrets", ref="wh-eu", field="minio-secret-key")

    assert fetches == ["wh-eu"], f"the reference was fetched {len(fetches)} times; it must be cached per reference"


def test_an_UNSET_reference_resolves_to_nothing_rather_than_to_the_estate_key() -> None:
    """Absent is not "use the estate's" — it is "this record names no second store".

    The caller decides what that means (today: the estate's own configured credential, because the
    warehouse is in the estate's store). Returning the estate secret HERE would make an empty
    reference indistinguishable from one that resolved, which is how a misconfigured record starts
    writing somewhere nobody intended.
    """
    from catalog.services import warehouse_credentials

    assert warehouse_credentials.resolve(store="lance-secrets", ref="", field="minio-secret-key") is None


def _unused(_: Any) -> None:  # pragma: no cover - keeps the import surface honest
    return None


def test_a_bundle_carrying_only_the_SECRET_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half a credential signs nothing, and defaulting the other half is the failure itself.

    Leaving the estate's `aws_access_key_id` in force beside a foreign secret is exactly what produced
    `SignatureDoesNotMatch` on every write to a referenced base (measured live 2026-09-21), and it is
    the same shape the Ray lane already paid for. So a bundle that names the secret and not the id is
    refused rather than half-applied.
    """
    from catalog.services import warehouse_credentials

    monkeypatch.setattr(warehouse_credentials, "fetch_required_secrets", lambda _s, _k, *, require: {require: "only-the-secret"})
    warehouse_credentials.resolve.cache_clear()

    with pytest.raises(RuntimeError, match="half a credential signs nothing"):
        warehouse_credentials.resolve(store="lance-secrets", ref="wh-eu", field="minio-secret-key")
