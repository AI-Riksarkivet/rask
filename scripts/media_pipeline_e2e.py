"""End-to-end multimodal pipeline over the provider-agnostic ingest/egress seam.

Runs the whole chain against real object storage + the real lineage service, with NO provider lock-in
(a plain S3/MinIO adapter stands in for IIIF/GCS/HF/HCP):

    external S3 (source images)  --ingest-->  bronze blob (2.2)
    bronze  --media derive-->  silver (thumbnail + embedding, 2.2)
    silver  --curate-->  gold (2.2)
    gold  --egress-->  external S3 (sink)

Each hop emits an OpenLineage event so AGE records ``source-uri -> bronze -> silver -> gold -> sink``.

Env: S3_ENDPOINT S3_KEY S3_SECRET S3_REGION  LINEAGE_URL  SRC_BUCKET SINK_BUCKET LAKE_BUCKET  RUN
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from PIL import Image


# On disk this script lives in scripts/; add services/ so the lakehouse packages import. Run in-pod via
# stdin (no __file__) those packages are already on PYTHONPATH, so the bootstrap is best-effort.
with contextlib.suppress(NameError):  # NameError => run from stdin; services/ already on PYTHONPATH
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from common import schema
from common.sinks import S3Sink
from common.sources import S3Source
from medallion.schemas.events import build_run_event
from medallion.services import media
from medallion.services.ingest import ingest_to_bronze


RUN = os.environ["RUN"]
LINEAGE = os.environ["LINEAGE_URL"].rstrip("/")
SRC_BUCKET = os.environ["SRC_BUCKET"]
SINK_BUCKET = os.environ["SINK_BUCKET"]
LAKE = os.environ["LAKE_BUCKET"]
SO = {
    "endpoint": os.environ["S3_ENDPOINT"],
    "access_key_id": os.environ["S3_KEY"],
    "secret_access_key": os.environ["S3_SECRET"],
    "region": os.environ.get("S3_REGION", "us-east-1"),
    "allow_http": "true",
    "virtual_hosted_style_request": "false",
}
BRONZE, SILVER, GOLD = (f"s3://{LAKE}/mp3-{RUN}/{s}" for s in ("bronze", "silver", "gold"))
# Run-scoped prefixes standing in for the external source/sink (a real deployment points these at a
# separate HCP/GCS/S3 bucket — the S3Source/S3Sink adapters are identical; only the filesystem changes).
SRC_PREFIX = f"external-src-{RUN}/batch"
SINK_PREFIX = f"external-sink-{RUN}"


def _fs() -> pafs.S3FileSystem:
    scheme, _, host = SO["endpoint"].partition("://")
    return pafs.S3FileSystem(
        access_key=SO["access_key_id"],
        secret_key=SO["secret_access_key"],
        endpoint_override=host,
        scheme=scheme,
        region=SO["region"],
        allow_bucket_creation=True,
        allow_bucket_deletion=False,
    )


def _emit(*, inputs: list[tuple[str, str]], out_ns: str, out_name: str, fields: list[dict[str, str]] | None) -> None:
    event = build_run_event(
        operation=f"mp3-{out_ns}",
        author="pipeline",
        job_namespace="medallion",
        inputs=inputs,
        output_namespace=out_ns,
        output_name=out_name,
        version=1,
        schema_fields=fields,
        source_uri=out_name,
        token=f"{RUN}-{out_ns}",
    )
    request = urllib.request.Request(  # noqa: S310 - trusted in-cluster lineage URL
        f"{LINEAGE}/api/v1/lineage",
        data=json.dumps(event).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        response.read()


def _png(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _seed_source(fs: pafs.S3FileSystem) -> None:
    for name, color in (("img-a.png", (200, 40, 40)), ("img-b.png", (40, 40, 200))):
        with fs.open_output_stream(f"{SRC_BUCKET}/{SRC_PREFIX}/{name}") as stream:
            stream.write(_png(color))


def _silver() -> list[dict[str, str]]:
    bronze = lance.dataset(BRONZE, storage_options=SO)
    rows = bronze.count_rows()
    images = [payload for _addr, payload in bronze.read_blobs("payload", indices=list(range(rows)))]
    sources = bronze.to_table(columns=["source_uri"]).column("source_uri")
    table = pa.table(
        {
            "id": pa.array(range(rows), pa.int64()),
            "source_uri": sources,
            "thumbnail": pa.array([media.derive_thumbnail(i) for i in images], pa.large_binary()),
            "embedding": pa.array([media.derive_embedding(i) for i in images], type=pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
        }
    )
    ds = lance.write_dataset(
        table,
        SILVER,
        storage_options=SO,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return schema.facet_fields(ds.schema)


def _gold() -> list[dict[str, str]]:
    silver = lance.dataset(SILVER, storage_options=SO).to_table(columns=["id", "source_uri", "embedding"])
    ds = lance.write_dataset(
        silver,
        GOLD,
        storage_options=SO,
        mode="overwrite",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    return schema.facet_fields(ds.schema)


def _egress(fs: pafs.S3FileSystem) -> str:
    gold = lance.dataset(GOLD, storage_options=SO).to_table()
    buffer = io.BytesIO()  # the curated artifact as one Arrow-IPC object
    with pa.ipc.new_stream(buffer, gold.schema) as writer:
        writer.write_table(gold)
    return S3Sink(fs, SINK_BUCKET, SINK_PREFIX).put("gold.arrow", buffer.getvalue())


def main() -> None:
    fs = _fs()
    _seed_source(fs)

    result = ingest_to_bronze(S3Source(fs, SRC_BUCKET, SRC_PREFIX), BRONZE, SO)
    _emit(
        inputs=[("source", uri) for uri in result.source_uris],
        out_ns="bronze",
        out_name=f"mp3_{RUN}_bronze",
        fields=result.fields,
    )

    silver_fields = _silver()
    _emit(
        inputs=[("bronze", f"mp3_{RUN}_bronze")],
        out_ns="silver",
        out_name=f"mp3_{RUN}_silver",
        fields=silver_fields,
    )

    gold_fields = _gold()
    _emit(
        inputs=[("silver", f"mp3_{RUN}_silver")],
        out_ns="gold",
        out_name=f"mp3_{RUN}_gold",
        fields=gold_fields,
    )

    sink_uri = _egress(fs)
    _emit(inputs=[("gold", f"mp3_{RUN}_gold")], out_ns="sink", out_name=sink_uri, fields=None)

    for label, uri in (("bronze", BRONZE), ("silver", SILVER), ("gold", GOLD)):
        ds = lance.dataset(uri, storage_options=SO)
        print(f"  {label}: dsv={ds.data_storage_version} rows={ds.count_rows()} cols={ds.schema.names}")
    print(f"  source_uris={result.source_uris}")
    print(f"  sink_uri={sink_uri}")
    print(f"  lineage names: mp3_{RUN}_bronze -> mp3_{RUN}_silver -> mp3_{RUN}_gold")


if __name__ == "__main__":
    main()
