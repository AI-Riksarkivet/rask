"""The anti-join reads a GOVERNED table, so it must ask the catalog for a credential.

`enumerate_chunks` reads every `id` already in bronze to work out which source objects are new. It
opened the dataset as `lance.dataset(uri)` — no storage options at all — so the read depended entirely
on the process's ambient credential chain.

MEASURED ON THE DEPLOYED ESTATE 2026-09-10, and it is not hypothetical: the ingest pod carries
`AWS_REGION`, `AWS_ENDPOINT_URL` and `AWS_ALLOW_HTTP` and NO `AWS_ACCESS_KEY_ID` or
`AWS_SECRET_ACCESS_KEY` — the long-lived pair was taken out of it, which is the direction zero trust
asks for. This read was never moved with it, so a real run failed at Activity #10 with
`Failed to get AWS credentials: CredentialsNotLoaded` after successfully creating and registering its
table. Not a 403: no credential was found at all, so every incremental run that must dedupe is dead on
the estate as shipped.

TWO THINGS ARE WRONG AND ONLY ONE IS VISIBLE. The read fails today, and it would ALSO be wrong if it
succeeded — a governed table read under a deployment-wide key is the posture the vending door exists to
end. The write half of this same plane already vends per chunk (`runtime.write_options_for`); the read
half was simply never given the same treatment.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from ingest import runtime


class _Vendor:
    """A catalog that vends, recording what it was asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    def vend_storage_options(self, namespace: str, dataset: str, *, tier: str = "write") -> Any:
        from service_kit.lakehouse.vended_credentials import VendedCredential

        self.asked.append((namespace, dataset, tier))
        return VendedCredential(options={"aws_access_key_id": "scoped", "aws_secret_access_key": "s"})


def test_the_read_credential_is_asked_for_at_the_READ_tier() -> None:
    """A write-tier credential would work and would be wrong: the anti-join only reads, and asking for
    more than the operation needs is how a scoped credential stops being a bound."""
    vendor = _Vendor()
    options = runtime.read_options_for(vendor, namespace="lane-bronze", dataset="pages")

    assert options == {"aws_access_key_id": "scoped", "aws_secret_access_key": "s"}
    assert vendor.asked == [("lane-bronze", "pages", "read")]


def test_a_seam_that_cannot_vend_degrades_to_the_ambient_credential() -> None:
    """`LocalCatalog` is the no-catalog dev shape and has no vending door, so asking it would raise
    rather than degrade. Checked by CAPABILITY, never assumed — the same rule the write half applies."""

    class _NoVendor:
        pass

    assert runtime.read_options_for(_NoVendor(), namespace="lane-bronze", dataset="pages") is None


def test_a_namespace_that_is_empty_asks_for_nothing() -> None:
    """A pre-upgrade payload replayed by this build carries no namespace, and composing an object id
    from it would ask the catalog about a table that does not exist — 403-ing a run that was mid-flight
    at deploy rather than degrading to the behaviour it started with."""
    vendor = _Vendor()

    assert runtime.read_options_for(vendor, namespace="", dataset="pages") is None
    assert vendor.asked == [], "an empty namespace must not reach the vending door at all"


def test_the_anti_join_opens_the_dataset_WITH_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end this exists for. Asserting the helper alone would pass against a caller that never uses
    it — which is exactly the state this file was written to end."""
    import lance

    seen: dict[str, Any] = {}

    def _dataset(uri: str, storage_options: dict[str, str] | None = None, **_: object) -> Any:
        seen["uri"], seen["storage_options"] = uri, storage_options

        class _DS:
            def count_rows(self) -> int:
                return 0

            def to_table(self, columns: list[str] | None = None) -> pa.Table:
                return pa.table({"id": pa.array([], type=pa.int64())})

        return _DS()

    monkeypatch.setattr(lance, "dataset", _dataset)
    monkeypatch.setattr(runtime, "read_options_for", lambda *_a, **_k: {"aws_access_key_id": "scoped"})

    from ingest.workflow import _existing_ids_for_anti_join

    _existing_ids_for_anti_join("s3://lane-wh/x_lane-bronze$pages", namespace="lane-bronze", dataset="pages", ceiling=1000)

    assert seen["storage_options"] == {"aws_access_key_id": "scoped"}, "the anti-join opened the table on the ambient chain"
