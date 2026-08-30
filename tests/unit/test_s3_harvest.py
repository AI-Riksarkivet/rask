"""The SECOND external-raw source family (R23) — object-storage harvest into bronze.

External raw is the external world: the IIIF Image API (``iiif_produce``) and external object storage
(this seam — the ra-hcp pattern, read through ``packages/storage``'s provider-agnostic S3 client). S3 is
doubled by moto (the project convention). Pinned here: the adapter yields deterministic, sorted
``SourceObject``\\ s with full ``s3://`` provenance URIs, it feeds ``ingest_to_bronze`` (the bronze
blob-v2 landing, stage-stamped at ingest), and ``s3_input`` names the external source the way the
bronze-write OpenLineage event records it (``(s3://<bucket>, <prefix>)``).
"""

from collections.abc import Iterator
from typing import Any

import lance
import pytest

from medallion.services.ingest import ingest_to_bronze
from medallion.services.s3_harvest import S3PrefixSource, s3_input
from storage import S3Source


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> Iterator[S3PrefixSource]:
    # Force every S3-endpoint alias empty (overriding the repo `.env`) so boto3 — and thus moto — uses
    # its default AWS endpoint instead of a real one.
    monkeypatch.setenv("RASK_S3_ENDPOINT_URL", "")
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    monkeypatch.setenv("HCP_ENDPOINT", "")

    import boto3
    from moto import mock_aws

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="external-drop")
        s3.put_object(Bucket="external-drop", Key="batch/b.png", Body=b"PNG-B")
        s3.put_object(Bucket="external-drop", Key="batch/a.png", Body=b"PNG-A")
        s3.put_object(Bucket="external-drop", Key="elsewhere/c.png", Body=b"PNG-C")
        yield S3PrefixSource(S3Source(bucket="external-drop", prefix="batch/", client=s3))


def test_s3_prefix_source_yields_sorted_objects_with_s3_uris(source: S3PrefixSource) -> None:
    objects = list(source.iter_objects())
    # Sorted key order (reproducible positional bronze ids), scoped to the prefix, full s3:// provenance.
    assert [o.uri for o in objects] == ["s3://external-drop/batch/a.png", "s3://external-drop/batch/b.png"]
    assert [o.data for o in objects] == [b"PNG-A", b"PNG-B"]


def test_s3_prefix_source_feeds_the_bronze_ingest(tmp_path: Any, source: S3PrefixSource) -> None:
    bronze_uri = str(tmp_path / "bronze")
    result = ingest_to_bronze(source, bronze_uri, {})
    table = lance.dataset(bronze_uri).to_table(columns=["id", "source_uri", "stage"])
    assert result.row_count == 2
    assert table.column("source_uri").to_pylist() == ["s3://external-drop/batch/a.png", "s3://external-drop/batch/b.png"]
    assert set(table.column("stage").to_pylist()) == {"bronze"}  # bronze is stage-stamped AT ingest (R23)
    assert result.source_uris == ["s3://external-drop/batch/a.png", "s3://external-drop/batch/b.png"]


def test_s3_input_names_the_external_source() -> None:
    assert s3_input("external-drop", "batch/") == ("s3://external-drop", "batch")
    assert s3_input("external-drop", "") == ("s3://external-drop", "/")
