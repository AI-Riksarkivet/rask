"""Live medallion demo driver — real Lance data on S3 + real OpenLineage to the lineage service.

Unlike ``services/lineage/seed.py`` (which emits *synthetic* events only), this **executes** the medallion
flow against the real docker-compose services: it writes and evolves real Lance datasets on MinIO
*and* emits a real OpenLineage event after each step, with a pause in between, so the thin frontend
(``services/lineage/static/index.html``, served at ``/ui/``) shows the DAG build and the silver versions
evolve live.

The flow (faithful to ``services/lineage/seed.py`` / ``docs/LINEAGE.md``):

    ingest_events    (alice)    raw_events      -> bronze$events  v1  (blob payload)
    embed_features   (data_eng) bronze$events   x  silver$features     (FAILS — recorded, no data)
    embed_features   (data_eng) bronze$events   -> silver$features v1  (+embedding)
    caption_features (data_eng) silver$features -> silver$features v2  (+caption, in place)
    aggregate_gold   (analyst)  silver$features -> gold$catalog    v1  (+lineage JSONB)

The object store is **RustFS** (Rust, S3-compatible) by default — the driver is storage-agnostic
(it only speaks S3 ``storage_options``), so MinIO/Ceph/AWS work identically by changing the creds.

Run (after ``scripts/medallion_demo.sh`` brings up RustFS + lineage)::

    uv run scripts/medallion_demo.py

Env (defaults target the RustFS compose stack from the host)::

    S3_ENDPOINT=http://localhost:9000  S3_ACCESS_KEY=rustfsadmin  S3_SECRET_KEY=rustfsadmin
    S3_REGION=us-east-1  S3_BUCKET=lakehouse  LINEAGE_URL=http://localhost:8000  STEP_DELAY=2.5
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import attr
import lance
import pyarrow as pa
import pyarrow.fs as pafs
from openlineage.client import OpenLineageClient
from openlineage.client.event_v2 import Run, RunEvent, RunState
from openlineage.client.facet_v2 import RunFacet
from openlineage.client.transport.http import HttpConfig, HttpTransport
from PIL import Image


# The ``lineage`` / ``medallion`` service packages live under ``services/`` (not the repo root), so put
# that on sys.path when run as a plain script — matching the pytest ``pythonpath = ["services", "."]``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services"))

from lance import blob_array, blob_field
from lineage.schemas import LineageGraph, Producers, Runs
from lineage.seed import build_events
from medallion.services import media

from service_kit.lakehouse import blobs


def _load_demo_env() -> None:
    """Load demo endpoints from ``<repo>/.medallion-demo.env`` (written when the stack starts), so a
    bare ``--step N`` targets the *actual* ports without an env-var prefix. Real env vars win."""
    path = Path(__file__).resolve().parent.parent / ".medallion-demo.env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_demo_env()

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://localhost:9000")
S3_KEY = os.environ.get("S3_ACCESS_KEY", "rustfsadmin")
S3_SECRET = os.environ.get("S3_SECRET_KEY", "rustfsadmin")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "lakehouse")
LINEAGE_URL = os.environ.get("LINEAGE_URL", "http://localhost:8000").rstrip("/")
STEP_DELAY = float(os.environ.get("STEP_DELAY", "2.5"))
# Pause between RUNNING heartbeats so the "running + progress" state is visible in the UI.
PROGRESS_DELAY = float(os.environ.get("PROGRESS_DELAY", "0.6"))


@attr.define
class ProgressRunFacet(RunFacet):
    """Custom run facet carrying batch progress on RUNNING events.

    OpenLineage has no standard progress facet (RUNNING just "reports metrics"), so this is a custom
    facet (spec-legal: it inherits ``_producer``/``_schemaURL`` from RunFacet). The lineage service's
    ``/runs`` board reads ``run.facets.progress.{done,total}``.
    """

    done: int = attr.field(default=0)
    total: int = attr.field(default=0)


_BRONZE = f"s3://{S3_BUCKET}/bronze/events"
_SILVER = f"s3://{S3_BUCKET}/silver/features"
_GOLD = f"s3://{S3_BUCKET}/gold/catalog"


def _say(message: str) -> None:
    print(f"  ▸ {message}", flush=True)


def _storage_options() -> dict[str, str]:
    """The pylance ``storage_options`` for the demo S3 backend (same keys the catalog uses)."""
    return {
        "endpoint": S3_ENDPOINT,
        "access_key_id": S3_KEY,
        "secret_access_key": S3_SECRET,
        "region": S3_REGION,
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


def _s3_filesystem() -> pafs.S3FileSystem:
    scheme, _, host = S3_ENDPOINT.partition("://")
    return pafs.S3FileSystem(
        access_key=S3_KEY,
        secret_key=S3_SECRET,
        endpoint_override=host or scheme,
        scheme=scheme if host else "http",
        region=S3_REGION,
        allow_bucket_creation=True,
    )


def ensure_bucket() -> None:
    """Create the demo bucket if it doesn't exist (object stores never auto-create buckets)."""
    try:
        _s3_filesystem().create_dir(S3_BUCKET)  # allow_bucket_creation makes this the bucket
        _say(f"bucket s3://{S3_BUCKET} ready")
    except Exception as exc:
        _say(f"bucket s3://{S3_BUCKET} (already present or: {exc})")


def reset_data() -> None:
    """Delete the demo's Lance datasets from S3 — a clean slate before re-running the flow."""
    fs = _s3_filesystem()
    for layer in ("bronze", "silver", "gold"):
        path = f"{S3_BUCKET}/{layer}"
        try:
            fs.delete_dir(path)
            _say(f"deleted s3://{path}")
        except Exception:
            _say(f"s3://{path} (nothing to delete)")


def _sample_image(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    """A small solid-colour PNG standing in for an ingested camera frame."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def write_bronze() -> None:
    """alice's ETL: land raw media as a blob ``payload`` column (real PNG frames)."""
    opts = _storage_options()
    ids = [1, 2, 3]
    payloads = [_sample_image(c) for c in ((200, 30, 30), (30, 200, 30), (30, 30, 200))]
    # payload_src = where each row's payload came from (the camera/sensor) — NOT the dataset's
    # source system (that's the raw_events node upstream of bronze in the lineage graph).
    payload_src = ["cam-a", "cam-b", "cam-a"]
    schema = pa.schema([pa.field("id", pa.int64()), blob_field("payload"), pa.field("payload_src", pa.string())])
    table = pa.table({"id": ids, "payload": blob_array(payloads), "payload_src": payload_src}, schema=schema)
    lance.write_dataset(table, _BRONZE, storage_options=opts, mode="overwrite", data_storage_version="2.2")
    _say(f"bronze$events written ({len(ids)} rows, real image blobs) -> {_BRONZE}")


def write_silver() -> None:
    """data_eng's ETL: decode each bronze media blob into a thumbnail + embedding, write silver v1.

    The heavy ``payload`` blob stays referenced in bronze; silver carries only the small derived artifacts
    (a downscaled ``thumbnail`` stored INLINE, and a pixel-derived ``embedding``) plus the keys.
    """
    opts = _storage_options()
    bronze = lance.dataset(_BRONZE, storage_options=opts)
    # ONE row-aligned scan for the keys AND the payload bytes (blobs.read_aligned_table wraps
    # blob_handling="all_binary"): read_blobs would DROP any null payload and misalign `images` against
    # `base` — the R27 landmine. This demo seeds three real PNGs, so nothing is null here; using the
    # aligned read anyway keeps the demo a correct example of the pattern.
    base = blobs.read_aligned_table(bronze, columns=["id", "payload_src", "payload"])
    rows = base.num_rows
    images = base.column("payload").to_pylist()
    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("payload_src", pa.string()),
            pa.field("thumbnail", pa.large_binary()),  # small INLINE binary — not a blob column
            pa.field("embedding", pa.list_(pa.float32(), media.EMBEDDING_DIMS)),
        ]
    )
    table = pa.table(
        {
            "id": base.column("id"),
            "payload_src": base.column("payload_src"),
            "thumbnail": pa.array([media.derive_thumbnail(img) for img in images], pa.large_binary()),
            "embedding": pa.array(
                [media.derive_embedding(img) for img in images],
                type=pa.list_(pa.float32(), media.EMBEDDING_DIMS),
            ),
        },
        schema=schema,
    )
    lance.write_dataset(table, _SILVER, storage_options=opts, mode="overwrite", data_storage_version="2.2")
    _say(f"silver$features v1 written ({rows} rows, thumbnail+embedding from media) -> {_SILVER}")


def add_caption() -> None:
    """data_eng's refine: add a ``caption`` column in place (Lance data evolution -> version bump)."""
    ds = lance.dataset(_SILVER, storage_options=_storage_options())
    ds.add_columns({"caption": "'auto-caption'"})
    _say("silver$features v2 written (+caption, in-place add-column)")


def _lineage_get(path: str) -> str:
    """GET a JSON body from the lineage service (the AGE graph is the provenance SoT)."""
    with urllib.request.urlopen(f"{LINEAGE_URL}/{path}", timeout=10) as resp:  # noqa: S310 - trusted local URL
        return resp.read().decode()


def _gold_provenance() -> dict[str, object]:
    """Assemble gold's **whole** upstream lineage from the lineage graph — the full
    ``gold -> silver -> bronze -> raw_events`` DAG plus every producing run (job / author / state /
    version / error, including the failed embed attempt), chronologically ordered.

    This co-locates a live copy of the cross-dataset source-of-truth into the gold table instead of a
    hand-typed snapshot. Best-effort: if the lineage service can't be reached the gold write still
    succeeds with a minimal record, since enrichment must never fail the primary write.
    """
    produced_by = {"job": "aggregate_gold", "author": "analyst"}
    try:
        jobs = {r.run_id: r.job for r in Runs.model_validate_json(_lineage_get("runs")).runs}
        graph = LineageGraph.model_validate_json(_lineage_get(f"datasets/{urllib.parse.quote('silver$features')}/graph"))
        history: list[dict[str, object]] = []
        for node in graph.nodes:
            producers = Producers.model_validate_json(_lineage_get(f"datasets/{urllib.parse.quote(node.id)}/producers"))
            for p in producers.producers:
                history.append(
                    {
                        "dataset": node.id,
                        "job": jobs.get(p.run_id),
                        "author": p.author,
                        "state": p.event_type,
                        "version": p.dataset_version,
                        "event_time": p.event_time,
                        "error": p.error_message,
                    }
                )
        history.sort(key=lambda h: str(h.get("event_time") or ""))
        # gold isn't in the graph yet (its COMPLETE event is emitted *after* this write), so add its
        # node + the gold->silver edge so the embedded DAG is complete through gold itself.
        nodes = [{"name": n.id, "source_uri": n.source_uri} for n in graph.nodes]
        nodes.append({"name": "gold$catalog", "source_uri": _GOLD})
        edges = [{"from": e.source, "to": e.target} for e in graph.edges]
        edges.append({"from": "gold$catalog", "to": "silver$features"})
        return {
            "dataset": "gold$catalog",
            "produced_by": produced_by,
            "graph": {"nodes": nodes, "edges": edges},
            "history": history,
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:  # ValueError covers JSON + Pydantic
        _say(f"⚠ lineage service unreachable ({exc}) — embedding minimal provenance")
        return {
            "dataset": "gold$catalog",
            "produced_by": produced_by,
            "graph": {"nodes": [], "edges": []},
            "history": [],
        }


def write_gold() -> None:
    """analyst's aggregate: write gold and embed the **whole** upstream lineage (the full DAG + every
    producing run, pulled live from the lineage graph) as a JSONB ``lineage`` column."""
    opts = _storage_options()
    sv = lance.dataset(_SILVER, storage_options=opts).to_table()
    provenance = _gold_provenance()
    lineage_json = [json.dumps(provenance)] * sv.num_rows
    try:
        lineage_col = pa.array(lineage_json, type=pa.json_())
    except (AttributeError, pa.ArrowNotImplementedError, TypeError):  # pragma: no cover - older arrow build
        # AttributeError: a pre-JSON pyarrow has no pa.json_ at all (the other two cover present-but-unbuilt).
        lineage_col = pa.array(lineage_json, type=pa.string())
    # Carry the keys forward (id, payload_src) so column lineage is visible through to gold; add the
    # embedded provenance JSONB. Only the raw `payload` blob was dropped (at silver, becoming embedding).
    table = pa.table(
        {
            "id": sv.column("id"),
            "payload_src": sv.column("payload_src"),
            "embedding": sv.column("embedding"),
            "caption": sv.column("caption"),
            "lineage": lineage_col,
        }
    )
    lance.write_dataset(table, _GOLD, storage_options=opts, mode="overwrite", data_storage_version="2.2")
    _say(f"gold$catalog v1 written ({sv.num_rows} rows, +lineage JSONB) -> {_GOLD}")


def _perform(event: RunEvent) -> None:
    """Run the real Lance operation that corresponds to one OpenLineage event."""
    if event.eventType == RunState.FAIL:
        _say("embed_features attempt FAILED (simulated CUDA OOM) — recorded, no data written")
        return
    {
        "ingest_events": write_bronze,
        "embed_features": write_silver,
        "caption_features": add_caption,
        "aggregate_gold": write_gold,
    }[event.job.name]()


def _describe(event: RunEvent) -> str:
    state = "FAIL" if event.eventType == RunState.FAIL else "COMPLETE"
    out = event.outputs[0].name if event.outputs else "-"
    src = event.inputs[0].name if event.inputs else "-"
    return f"{event.job.name:16} {state:8} {src} -> {out}"


def _lifecycle_event(event: RunEvent, state: RunState, facets: dict) -> RunEvent:
    """A non-terminal (START/RUNNING) event for ``event``'s run — same run/job/inputs, no output yet."""
    return RunEvent(
        eventTime=event.eventTime,
        producer=event.producer,
        eventType=state,
        run=Run(runId=event.run.runId, facets=facets),
        job=event.job,
        inputs=event.inputs,
        outputs=[],
    )


def _emit_step(client: OpenLineageClient, index: int, total: int, event: RunEvent, *, data: bool) -> None:
    """Emit the full OpenLineage lifecycle (START -> RUNNING+progress -> COMPLETE/FAIL) for one step.

    Real Lance op runs between RUNNING and the terminal event (unless ``data`` is False). The shared
    runId ties the events into one run, so the lineage service's /runs board shows it go
    queued -> running (with progress) -> done/failed live.
    """
    fail = event.eventType == RunState.FAIL
    state = "FAIL" if fail else "COMPLETE"
    base = dict(event.run.facets or {})
    print(f"\n[{index}/{total}] {_describe(event)}")

    client.emit(_lifecycle_event(event, RunState.START, base))
    _say("START -> RUNNING")
    steps = 2 if fail else 3  # a failed run dies partway through
    for i in range(1, steps + 1):
        if PROGRESS_DELAY:
            time.sleep(PROGRESS_DELAY)
        facets = {**base, "progress": ProgressRunFacet(done=i, total=3)}
        client.emit(_lifecycle_event(event, RunState.RUNNING, facets))
        _say(f"RUNNING {i}/3")

    if data:
        _perform(event)  # the real Lance write (or the simulated-failure message)
    client.emit(event)  # terminal COMPLETE (carries version) / FAIL (carries error)
    _say(f"{state} emitted → {LINEAGE_URL}/api/v1/lineage")


def main() -> None:
    parser = argparse.ArgumentParser(description="Drive the live medallion flow (data + OpenLineage).")
    parser.add_argument("--step", type=int, metavar="N", help="emit ONLY step N (you are the producer)")
    parser.add_argument("--list", action="store_true", help="list the steps and exit (no side effects)")
    parser.add_argument("--reset", action="store_true", help="delete the demo's Lance datasets and exit")
    parser.add_argument("--emit-only", action="store_true", help="emit the OpenLineage event(s) only, skip the Lance data op")
    parser.add_argument("--delay", type=float, default=STEP_DELAY, help="seconds between steps in auto mode")
    args = parser.parse_args()

    events = build_events()
    if args.list:
        print("\nMedallion steps (use --step N to trigger one manually):\n")
        for i, event in enumerate(events, start=1):
            print(f"  {i}. {_describe(event)}")
        print(f"\nWatch the graph build at {LINEAGE_URL}/ui/\n")
        return

    if args.reset:
        print(f"\nReset → clearing the demo's Lance datasets under s3://{S3_BUCKET}\n")
        reset_data()
        return

    do_data = not args.emit_only
    print(f"\nLive medallion → S3 {S3_ENDPOINT} (bucket {S3_BUCKET}) + lineage {LINEAGE_URL}\n")
    if do_data:
        ensure_bucket()
    # HttpTransport appends its default endpoint (api/v1/lineage) to the base url, so pass the base.
    client = OpenLineageClient(transport=HttpTransport(HttpConfig(url=LINEAGE_URL)))

    if args.step is not None:
        if not 1 <= args.step <= len(events):
            parser.error(f"--step must be between 1 and {len(events)}")
        _emit_step(client, args.step, len(events), events[args.step - 1], data=do_data)
        nxt = min(args.step + 1, len(events))
        print(f"\n✓ step {args.step} done. Graph: {LINEAGE_URL}/ui/  ·  next: --step {nxt}\n")
        return

    for index, event in enumerate(events, start=1):
        _emit_step(client, index, len(events), event, data=do_data)
        time.sleep(args.delay)
    print(f"\n✓ done. Watch the lineage graph build at {LINEAGE_URL}/ui/\n")


if __name__ == "__main__":
    main()
