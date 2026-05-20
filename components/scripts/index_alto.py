"""Build a Lance FTS index over the transcribed lines in a batch's ALTO output.

Smoke-test workflow (local files only):

    uv run python scripts/index_alto.py index --batch A0060198 \\
        --alto-dir /home/morgan/!_output/A0060198 \\
        --image-dir /home/morgan/!_input/A0060198 \\
        --out /home/morgan/_search

    uv run python scripts/index_alto.py search --query "Riksgiktet" \\
        --out /home/morgan/_search

The index produces, under `--out`:
    lines.lance/                 — Lance dataset, one row per text line
    thumbs/<batch>/<page>/<line_id>.jpg  — small line crops for the search UI

Schema is per-line (not per-page): finest granularity that maps one search hit
to one navigable place in the viewer. ReadingOrder is honoured for `line_idx`
so the UI can show hits in document order.

Local-only for now; S3/HCP support comes after the smoke test (Lance accepts
`storage_options=...` kwargs for the same API surface).
"""

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import boto3
import lance
import pyarrow as pa
import ray
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from PIL import Image


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("index_alto")

ALTO_NS = "{http://www.loc.gov/standards/alto/ns-v4#}"
THUMB_HEIGHT = 64  # pixels — enough to read short lines, tiny on disk

DEFAULT_IMAGE_BUCKET = "images-batch"
DEFAULT_ALTO_BUCKET = "images-batch-alto"
DEFAULT_SEARCH_BUCKET = "images-batch-search"


def s3_client() -> object:
    """boto3 client tuned for HCP/MinIO (path-style addressing).

    With Ray fan-out we hammer HCP with thumbnail PUTs from many tasks at
    once. Bumping the connection pool and retry budget keeps transient
    `IncompleteBody` / connection-pool-exhaustion errors from killing
    individual uploads (and through that, the whole batch via ray.get).
    """
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("HCP_ENDPOINT"),
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "adaptive"},
            max_pool_connections=64,
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def lance_storage_options() -> dict:
    """object_store-style options for Lance against HCP/MinIO."""
    endpoint = os.environ["HCP_ENDPOINT"]
    return {
        "aws_endpoint": endpoint,
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
        "aws_virtual_hosted_style_request": "false",  # HCP/MinIO use path-style
        "allow_http": "true" if endpoint.startswith("http://") else "false",
    }


def ensure_bucket(client: object, bucket: str) -> None:
    """Idempotent bucket create."""
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
            log.info("creating bucket %s", bucket)
            client.create_bucket(Bucket=bucket)
        else:
            raise


SCHEMA = pa.schema(
    [
        ("batch_id", pa.string()),
        ("page_id", pa.string()),
        ("page_idx", pa.int32()),
        ("line_id", pa.string()),
        ("line_idx", pa.int32()),
        ("text", pa.string()),
        ("confidence", pa.float32()),
        ("hpos", pa.int32()),
        ("vpos", pa.int32()),
        ("width", pa.int32()),
        ("height", pa.int32()),
        # YOLO line polygon — preserved verbatim from ALTO `<Shape><Polygon POINTS="x,y x,y …"/>`.
        # Empty string when ALTO has only a bbox. Renderer in the viewer can split on
        # whitespace and parse "x,y" pairs the same way it parses ALTO directly.
        ("polygon", pa.string()),
        ("thumb_key", pa.string()),
        ("indexed_at", pa.timestamp("us", tz="UTC")),
    ]
)


def parse_alto(xml_bytes: bytes) -> tuple[str, list[dict]]:
    """Parse one ALTO file -> (page_id, [line_dict, …]) ordered by ReadingOrder."""
    # ALTO comes from our own HTR pipeline (trusted source); no XXE risk.
    root = ET.fromstring(xml_bytes)  # noqa: S314

    # page_id from sourceImageInformation/fileName
    page_id = ""
    fn = root.find(f".//{ALTO_NS}sourceImageInformation/{ALTO_NS}fileName")
    if fn is not None and fn.text:
        page_id = fn.text.strip()

    # Reading order (may be absent for very simple pages)
    order: list[str] = []
    og = root.find(f".//{ALTO_NS}OrderedGroup")
    if og is not None:
        for ref in og.findall(f"{ALTO_NS}ElementRef"):
            ref_id = ref.get("REF")
            if ref_id:
                order.append(ref_id)

    # Index TextLines by ID so we can iterate them in reading order.
    by_id: dict[str, dict] = {}
    for tl in root.iter(f"{ALTO_NS}TextLine"):
        line_id = tl.get("ID") or ""
        # A TextLine has one <String> per word. Concatenate their CONTENT with
        # spaces to get the full line text. Confidence is the length-weighted
        # mean of the per-word WC values (longer words dominate the average).
        words: list[str] = []
        wc_total = 0.0
        wc_chars = 0
        for s in tl.iter(f"{ALTO_NS}String"):
            content = s.get("CONTENT") or ""
            if not content:
                continue
            words.append(content)
            try:
                wc = float(s.get("WC") or 0.0)
            except ValueError:
                wc = 0.0
            wc_total += wc * len(content)
            wc_chars += len(content)
        text = " ".join(words)
        confidence = (wc_total / wc_chars) if wc_chars > 0 else 0.0
        # Polygon comes from <Shape><Polygon POINTS="…"/> inside the TextLine.
        # Stored as the raw "x,y x,y …" string for lossless round-trip.
        poly_el = tl.find(f"{ALTO_NS}Shape/{ALTO_NS}Polygon")
        polygon = (poly_el.get("POINTS") if poly_el is not None else "") or ""
        by_id[line_id] = {
            "line_id": line_id,
            "text": text,
            "confidence": confidence,
            "hpos": int(tl.get("HPOS") or 0),
            "vpos": int(tl.get("VPOS") or 0),
            "width": int(tl.get("WIDTH") or 0),
            "height": int(tl.get("HEIGHT") or 0),
            "polygon": polygon,
        }

    # Emit in reading order, then any remaining lines (degraded ALTO without RO).
    out: list[dict] = []
    seen: set[str] = set()
    for idx, lid in enumerate(order):
        if lid in by_id:
            d = by_id[lid] | {"line_idx": idx}
            out.append(d)
            seen.add(lid)
    next_idx = len(out)
    for lid, d in by_id.items():
        if lid in seen:
            continue
        out.append(d | {"line_idx": next_idx})
        next_idx += 1

    return page_id, out


def make_thumb(img: Image.Image, hpos: int, vpos: int, w: int, h: int, max_height: int = THUMB_HEIGHT) -> bytes:
    """Crop the line region from the page image, resize to max_height tall, return JPEG bytes."""
    # Clamp to image bounds in case ALTO bbox is slightly off.
    iw, ih = img.size
    x0 = max(0, min(hpos, iw - 1))
    y0 = max(0, min(vpos, ih - 1))
    x1 = max(x0 + 1, min(hpos + w, iw))
    y1 = max(y0 + 1, min(vpos + h, ih))
    crop = img.crop((x0, y0, x1, y1))
    if crop.height > max_height:
        ratio = max_height / crop.height
        crop = crop.resize((max(1, int(crop.width * ratio)), max_height), Image.LANCZOS)
    buf = BytesIO()
    crop.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _open_page_image(image_dir: Path | None, page_id: str) -> Image.Image | None:
    if image_dir is None:
        return None
    img_path = image_dir / page_id
    if not img_path.exists():
        img_path = image_dir / Path(page_id).stem
    if not img_path.exists():
        return None
    try:
        return Image.open(img_path).convert("RGB")
    except Exception as exc:
        log.warning("failed to open %s: %s", img_path, exc)
        return None


def _list_s3_alto_keys(s3, bucket: str, batch: str) -> list[str]:
    """List all .xml keys under <bucket>/<batch>/ in lexical order."""
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{batch}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []) if obj["Key"].lower().endswith(".xml"))
    return sorted(keys)


def _read_s3_bytes(s3, bucket: str, key: str) -> bytes | None:
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()
    except ClientError as e:
        log.warning("S3 GET %s/%s failed: %s", bucket, key, e)
        return None


def _open_s3_page_image(s3, bucket: str, key: str) -> Image.Image | None:
    data = _read_s3_bytes(s3, bucket, key)
    if data is None:
        return None
    try:
        return Image.open(BytesIO(data)).convert("RGB")
    except Exception as exc:
        log.warning("failed to decode %s/%s: %s", bucket, key, exc)
        return None


class ThumbWriter:
    """Writes line thumbnail JPEGs to either a local dir or an S3 bucket."""

    def __init__(self, batch: str, *, local_root: Path | None = None, s3=None, s3_bucket: str | None = None) -> None:
        self.batch = batch
        self.local_root = local_root
        self.s3 = s3
        self.s3_bucket = s3_bucket
        if local_root is not None:
            local_root.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.local_root is not None or (self.s3 is not None and self.s3_bucket is not None)

    def write(self, page_id: str, line: dict, page_img: Image.Image) -> str:
        """Write the line crop and return the key (relative for local; S3 key for s3 mode)."""
        rel_key = f"thumbs/{self.batch}/{page_id}/{line['line_id']}.jpg"
        try:
            thumb_bytes = make_thumb(page_img, line["hpos"], line["vpos"], line["width"], line["height"])
        except Exception as exc:
            log.warning("thumb failed for %s: %s", line["line_id"], exc)
            return ""
        if self.local_root is not None:
            p = self.local_root / page_id / f"{line['line_id']}.jpg"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(thumb_bytes)
        if self.s3 is not None and self.s3_bucket is not None:
            # Tolerate per-thumb upload failures — under heavy concurrency HCP
            # occasionally returns IncompleteBody / 500. Losing one thumbnail
            # is fine (the row is still indexed; UI just won't have a crop),
            # but raising here would kill the entire batch via ray.get().
            try:
                self.s3.put_object(Bucket=self.s3_bucket, Key=rel_key, Body=thumb_bytes, ContentType="image/jpeg")
            except Exception as exc:
                log.warning("thumb upload failed for %s: %s", line["line_id"], exc)
                return ""
        return rel_key


def _rows_for_page(
    batch: str,
    page_idx: int,
    page_id: str,
    lines: list[dict],
    page_img: Image.Image | None,
    thumb_writer: ThumbWriter | None,
    indexed_at: datetime,
) -> list[dict]:
    rows: list[dict] = []
    for line in lines:
        thumb_key = ""
        if page_img is not None and thumb_writer is not None and thumb_writer.enabled and line["text"]:
            thumb_key = thumb_writer.write(page_id, line, page_img)
        rows.append(
            {
                "batch_id": batch,
                "page_id": page_id,
                "page_idx": page_idx,
                "line_id": line["line_id"],
                "line_idx": line["line_idx"],
                "text": line["text"],
                "confidence": line["confidence"],
                "hpos": line["hpos"],
                "vpos": line["vpos"],
                "width": line["width"],
                "height": line["height"],
                "polygon": line["polygon"],
                "thumb_key": thumb_key,
                "indexed_at": indexed_at,
            }
        )
    return rows


def _index_local(args: argparse.Namespace, thumb_writer: ThumbWriter | None, indexed_at: datetime) -> list[dict]:
    alto_dir = Path(args.alto_dir)
    if not alto_dir.is_dir():
        log.error("ALTO dir not found: %s", alto_dir)
        return []
    xml_files = sorted(alto_dir.glob("*.xml"))
    if args.limit:
        xml_files = xml_files[: args.limit]
    log.info("local mode: %d ALTO files for batch=%s", len(xml_files), args.batch)
    image_dir = Path(args.image_dir) if args.image_dir else None
    rows: list[dict] = []
    for page_idx, xml_path in enumerate(xml_files):
        page_id, lines = parse_alto(xml_path.read_bytes())
        if not page_id:
            page_id = xml_path.stem
        if not lines:
            continue
        page_img = _open_page_image(image_dir, page_id) if thumb_writer and thumb_writer.enabled else None
        rows.extend(_rows_for_page(args.batch, page_idx, page_id, lines, page_img, thumb_writer, indexed_at))
    return rows


def _process_alto_key(
    row: dict,
    batch: str,
    alto_bucket: str,
    image_bucket: str,
    write_thumbs: bool,
    thumb_bucket: str | None,
    indexed_at: datetime,
) -> list[dict]:
    """Per-page worker — reads ALTO + image, generates thumbs, returns Lance rows.

    Runs inside Ray Data tasks. Module-level (not a closure) so it pickles
    cleanly across the cluster. Each call constructs its own boto3 client
    rather than sharing one — clients are cheap to spin up (~ms) and not
    safe to share across worker processes.
    """
    s3 = s3_client()
    xml_bytes = _read_s3_bytes(s3, alto_bucket, row["key"])
    if xml_bytes is None:
        return []
    page_id, lines = parse_alto(xml_bytes)
    if not page_id:
        page_id = Path(row["key"]).stem
    if not lines:
        return []
    page_img = None
    local_writer = None
    if write_thumbs and thumb_bucket:
        page_img = _open_s3_page_image(s3, image_bucket, f"{batch}/{page_id}")
        local_writer = ThumbWriter(batch, s3=s3, s3_bucket=thumb_bucket)
    return _rows_for_page(batch, int(row["page_idx"]), page_id, lines, page_img, local_writer, indexed_at)


@ray.remote(num_cpus=0.5)
def _process_alto_key_remote(
    row: dict,
    batch: str,
    alto_bucket: str,
    image_bucket: str,
    write_thumbs: bool,
    thumb_bucket: str | None,
    indexed_at: datetime,
) -> list[dict]:
    return _process_alto_key(row, batch, alto_bucket, image_bucket, write_thumbs, thumb_bucket, indexed_at)


def _index_s3(args: argparse.Namespace, thumb_writer: ThumbWriter | None, indexed_at: datetime) -> list[dict]:
    """Per-batch indexing via Ray remote tasks — parallel S3 fetches per page.

    Each Ray task processes one ALTO key (read XML, read page image, crop and
    upload N line thumbnails, build rows). Pure I/O, so we claim 0.25 CPU per
    task — that lets ~4x the cluster's CPU count run concurrently without
    starving other workloads. We submit all N tasks at once and `ray.get`
    collects them; backpressure is handled implicitly by the cluster's
    scheduler. Rows for one batch (~22k) come back to the driver and the
    caller commits them in a single Lance transaction.

    We use plain `ray.remote` tasks rather than Ray Data here — the streaming
    executor's ResourceBudget backpressure throttles flat_map to one task at
    a time when sharing the cluster, even with override_num_blocks set.
    """
    s3 = s3_client()
    keys = _list_s3_alto_keys(s3, args.alto_bucket, args.batch)
    if args.limit:
        keys = keys[: args.limit]
    log.info("s3 mode: %d ALTO keys under %s/%s/", len(keys), args.alto_bucket, args.batch)
    if not keys:
        return []

    write_thumbs = bool(thumb_writer and thumb_writer.enabled)
    thumb_bucket = thumb_writer.s3_bucket if (thumb_writer and thumb_writer.s3_bucket) else None

    # Connect to whatever cluster RAY_ADDRESS / `ray start` already provides.
    # Workers spawn fresh and don't inherit our .env, so explicitly forward the
    # AWS/HCP creds via runtime_env. Idempotent: ignore_reinit_error means
    # re-entry from index-all's loop is a no-op (the first init's runtime_env
    # applies to every job task).
    _aws_env = {k: os.environ[k] for k in ("HCP_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION") if k in os.environ}
    ray.init(
        address="auto",
        ignore_reinit_error=True,
        log_to_driver=False,
        runtime_env={"env_vars": _aws_env},
    )

    batch_id = args.batch
    alto_bucket = args.alto_bucket
    image_bucket = args.image_bucket

    futures = [
        _process_alto_key_remote.remote(
            {"page_idx": i, "key": k},
            batch_id,
            alto_bucket,
            image_bucket,
            write_thumbs,
            thumb_bucket,
            indexed_at,
        )
        for i, k in enumerate(keys)
    ]
    rows: list[dict] = []
    for sublist in ray.get(futures):
        rows.extend(sublist)
    return rows


def cmd_index(args: argparse.Namespace) -> int:
    is_s3 = args.out.startswith("s3://")
    indexed_at = datetime.now(UTC)

    # Pick output target + thumb writer
    if is_s3:
        bucket = args.out[len("s3://") :].split("/", 1)[0]
        prefix = args.out[len("s3://") + len(bucket) + 1 :].rstrip("/")
        lance_uri = f"s3://{bucket}/{prefix}/lines.lance" if prefix else f"s3://{bucket}/lines.lance"
        s3 = s3_client()
        ensure_bucket(s3, bucket)
        thumb_writer = None if args.no_thumbs else ThumbWriter(args.batch, s3=s3, s3_bucket=bucket)
        storage_options = lance_storage_options()
    else:
        out_root = Path(args.out)
        out_root.mkdir(parents=True, exist_ok=True)
        lance_uri = str(out_root / "lines.lance")
        thumb_writer = None if args.no_thumbs else ThumbWriter(args.batch, local_root=out_root / "thumbs" / args.batch)
        storage_options = None

    # Source: local dir or S3 bucket
    rows = _index_local(args, thumb_writer, indexed_at) if args.alto_dir else _index_s3(args, thumb_writer, indexed_at)

    if not rows:
        log.warning("no rows extracted; nothing to index")
        return 0

    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    log.info("writing %d rows to %s", len(rows), lance_uri)

    # If the dataset already has rows for this batch, drop them so re-runs are
    # idempotent. Lance supports DELETE with a SQL-ish filter.
    write_kwargs = {"schema": SCHEMA}
    if storage_options is not None:
        write_kwargs["storage_options"] = storage_options
    if _dataset_exists(lance_uri, storage_options):
        ds = lance.dataset(lance_uri, storage_options=storage_options) if storage_options else lance.dataset(lance_uri)
        ds.delete(f"batch_id = '{args.batch}'")
        log.info("removed prior rows for batch=%s", args.batch)
        lance.write_dataset(table, lance_uri, mode="append", **write_kwargs)
    else:
        lance.write_dataset(table, lance_uri, mode="overwrite", **write_kwargs)

    # Build/refresh the FTS index over `text`. Lance recognises this as
    # "full-text search" via the inverted index type. The `index-all` driver
    # passes `skip_fts=True` so it can rebuild the index once at the end of
    # the loop instead of after every batch (a per-batch rebuild dominates
    # wall time for the 1,633-batch backfill).
    if not getattr(args, "skip_fts", False):
        ds = lance.dataset(lance_uri, storage_options=storage_options) if storage_options else lance.dataset(lance_uri)
        log.info("building FTS index on `text` …")
        ds.create_scalar_index("text", index_type="INVERTED", replace=True)
        log.info("done. dataset has %d rows total.", ds.count_rows())
    return 0


def cmd_index_all(args: argparse.Namespace) -> int:
    """Loop over every batch in batches.db with htr_status='done' and index it."""
    import sqlite3

    db_path = args.db or str(Path(__file__).resolve().parents[2] / ".cache" / "batches.db")
    if not Path(db_path).exists():
        log.error("batches.db not found at %s", db_path)
        return 1
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT batch_id FROM batches WHERE htr_status = 'done' ORDER BY batch_id").fetchall()
    db.close()
    batch_ids = [r[0] for r in rows]
    log.info("found %d batches with htr_status='done'", len(batch_ids))

    if args.skip_existing and batch_ids:
        lance_uri, storage_options = _resolve_lance_uri(args.out)
        if _dataset_exists(lance_uri, storage_options):
            ds = lance.dataset(lance_uri, storage_options=storage_options) if storage_options else lance.dataset(lance_uri)
            existing_tbl = ds.scanner(columns=["batch_id"]).to_table()
            existing = set(existing_tbl["batch_id"].to_pylist())
            before = len(batch_ids)
            batch_ids = [b for b in batch_ids if b not in existing]
            log.info("skipping %d already-indexed batches; %d remain", before - len(batch_ids), len(batch_ids))

    if not batch_ids:
        log.info("nothing to index.")
        return 0

    failed: list[tuple[str, str]] = []
    for i, batch_id in enumerate(batch_ids, 1):
        log.info("[%d/%d] indexing %s", i, len(batch_ids), batch_id)
        sub = argparse.Namespace(**vars(args))
        sub.batch = batch_id
        sub.skip_fts = True  # rebuild once at the end
        try:
            cmd_index(sub)
        except Exception as exc:
            log.error("failed: %s — %s", batch_id, exc)
            failed.append((batch_id, str(exc)))

    # Single FTS rebuild at the end.
    lance_uri, storage_options = _resolve_lance_uri(args.out)
    if _dataset_exists(lance_uri, storage_options):
        ds = lance.dataset(lance_uri, storage_options=storage_options) if storage_options else lance.dataset(lance_uri)
        log.info("building FTS index over %d rows …", ds.count_rows())
        ds.create_scalar_index("text", index_type="INVERTED", replace=True)

    if failed:
        log.warning("%d batches failed:", len(failed))
        for b, err in failed:
            log.warning("  %s: %s", b, err)
        return 2
    log.info("indexed %d batches successfully.", len(batch_ids))
    return 0


def _dataset_exists(lance_uri: str, storage_options: dict | None) -> bool:
    if lance_uri.startswith("s3://"):
        try:
            lance.dataset(lance_uri, storage_options=storage_options)
            return True
        except Exception:
            return False
    return Path(lance_uri).exists()


def _resolve_lance_uri(out: str) -> tuple[str, dict | None]:
    """Map --out arg to a (lance_uri, storage_options) pair."""
    if out.startswith("s3://"):
        return f"{out.rstrip('/')}/lines.lance", lance_storage_options()
    return str(Path(out) / "lines.lance"), None


def cmd_search(args: argparse.Namespace) -> int:
    lance_uri, storage_options = _resolve_lance_uri(args.out)
    if not _dataset_exists(lance_uri, storage_options):
        log.error("dataset not found: %s", lance_uri)
        return 1
    ds = lance.dataset(lance_uri, storage_options=storage_options) if storage_options else lance.dataset(lance_uri)
    log.info("searching %d rows for %r", ds.count_rows(), args.query)
    tbl = ds.scanner(
        full_text_query={"query": args.query, "columns": ["text"]},
        columns=["batch_id", "page_id", "line_idx", "text", "confidence", "thumb_key"],
        limit=args.limit,
    ).to_table()
    rows = tbl.to_pylist()
    print(f"{len(rows)} hit(s):")
    for r in rows:
        print(f"  [{r['batch_id']} · {r['page_id']} · L{r['line_idx']:03d}]  conf={r['confidence']:.3f}  {r['text'][:120]!r}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    lance_uri, storage_options = _resolve_lance_uri(args.out)
    if not _dataset_exists(lance_uri, storage_options):
        log.error("dataset not found: %s", lance_uri)
        return 1
    ds = lance.dataset(lance_uri, storage_options=storage_options) if storage_options else lance.dataset(lance_uri)
    print(f"Lance dataset: {lance_uri}")
    print(f"  rows:    {ds.count_rows()}")
    print(f"  version: {ds.version}")
    print("  schema:")
    for f in ds.schema:
        print(f"    {f.name}: {f.type}")
    # Per-batch row counts
    tbl = ds.scanner(columns=["batch_id"]).to_table()
    counter: dict[str, int] = {}
    for v in tbl["batch_id"].to_pylist():
        counter[v] = counter.get(v, 0) + 1
    print("  rows per batch:")
    for b, n in sorted(counter.items()):
        print(f"    {b}: {n}")
    return 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    # Default S3 target: s3://images-batch-search (lance dataset under this prefix).
    default_out_s3 = f"s3://{DEFAULT_SEARCH_BUCKET}"

    pi = sub.add_parser("index", help="parse ALTO + generate thumbnails + append to Lance")
    pi.add_argument("--batch", required=True)
    pi.add_argument("--alto-dir", help="local directory of <page>.xml files (omit to read from S3)")
    pi.add_argument("--image-dir", help="local directory of page JPGs (for thumbnails)")
    pi.add_argument("--alto-bucket", default=DEFAULT_ALTO_BUCKET, help="S3 bucket of ALTO XMLs (S3 mode)")
    pi.add_argument("--image-bucket", default=DEFAULT_IMAGE_BUCKET, help="S3 bucket of source JPGs (S3 mode)")
    pi.add_argument("--out", default=default_out_s3, help='lance+thumbs target — local path or "s3://bucket[/prefix]"')
    pi.add_argument("--limit", type=int, default=None, help="limit pages for a quick test")
    pi.add_argument("--no-thumbs", action="store_true")
    pi.add_argument("--concurrency", type=int, default=16, help="parallel S3 fetches per batch (Ray Data tasks)")
    pi.set_defaults(fn=cmd_index)

    pa = sub.add_parser("index-all", help="loop over every htr_status='done' batch in batches.db")
    pa.add_argument("--db", help="path to batches.db (defaults to <repo>/.cache/batches.db)")
    pa.add_argument("--alto-bucket", default=DEFAULT_ALTO_BUCKET)
    pa.add_argument("--image-bucket", default=DEFAULT_IMAGE_BUCKET)
    pa.add_argument("--out", default=default_out_s3)
    pa.add_argument("--limit", type=int, default=None, help="limit pages per batch (for testing)")
    pa.add_argument("--no-thumbs", action="store_true")
    pa.add_argument("--skip-existing", action="store_true", help="skip batches already in the Lance dataset")
    pa.add_argument("--concurrency", type=int, default=16, help="parallel S3 fetches per batch (Ray Data tasks)")
    # `index-all` always uses S3 mode — local --alto-dir doesn't make sense across many batches.
    pa.set_defaults(fn=cmd_index_all, alto_dir=None, image_dir=None)

    ps = sub.add_parser("search", help="run an FTS query against the index")
    ps.add_argument("--query", required=True)
    ps.add_argument("--out", default=default_out_s3)
    ps.add_argument("--limit", type=int, default=20)
    ps.set_defaults(fn=cmd_search)

    pn = sub.add_parser("info", help="show dataset stats")
    pn.add_argument("--out", default=default_out_s3)
    pn.set_defaults(fn=cmd_info)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
