"""Parse Riksarkivet EAD XML metadata (oai_ape_ead format) and ingest into LanceDB."""

import os
import re
import xml.etree.ElementTree as ET

import lancedb
import pyarrow as pa


def _lance_storage_options() -> dict:
    """object_store options for HCP/MinIO (path-style addressing)."""
    endpoint = os.environ["HCP_ENDPOINT"]
    return {
        "aws_endpoint": endpoint,
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
        "aws_virtual_hosted_style_request": "false",
        "allow_http": "true" if endpoint.startswith("http://") else "false",
    }


def _connect(db_path: str):
    """lancedb.connect, with HCP/MinIO storage_options when db_path is on S3."""
    if db_path.startswith("s3://"):
        return lancedb.connect(db_path, storage_options=_lance_storage_options())
    return lancedb.connect(db_path)


CATALOG_TABLE = "archive_catalog"
EMBED_MODEL = "Snowflake/snowflake-arctic-embed-l-v2.0"
EMBED_DIM = 256  # Matryoshka truncation: 1024 → 256 (smaller than e5-small, better quality)

CATALOG_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("reference_code", pa.string()),
        pa.field("archive_code", pa.string()),
        pa.field("fonds_id", pa.string()),
        pa.field("fonds_title", pa.string()),
        pa.field("fonds_date", pa.string()),
        pa.field("fonds_description", pa.string()),
        pa.field("fonds_extent", pa.string()),
        pa.field("creator", pa.string()),
        pa.field("series_id", pa.string()),
        pa.field("series_title", pa.string()),
        pa.field("volume_id", pa.string()),
        pa.field("volume_title", pa.string()),
        pa.field("date_text", pa.string()),
        pa.field("date_start", pa.int32()),
        pa.field("date_end", pa.int32()),
        pa.field("description", pa.string()),
        pa.field("access_restriction", pa.string()),
        pa.field("digitized", pa.bool_()),
        pa.field("bild_id", pa.string()),
        pa.field("bildvisning_url", pa.string()),
        pa.field("iiif_manifest", pa.string()),
        pa.field("thumbnail_url", pa.string()),
        pa.field("nad_url", pa.string()),
        pa.field("search_text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
    ]
)

# APE EAD namespace (oai_ape_ead format from Riksarkivet OAI-PMH)
NS = "urn:isbn:1-931666-22-9"
XLINK = "http://www.w3.org/1999/xlink"


def _tag(name: str) -> str:
    return f"{{{NS}}}{name}"


def _text(el, path: str) -> str:
    """Extract text from a sub-element, or empty string."""
    child = el.find(path)
    return (child.text or "").strip() if child is not None else ""


def parse_date_range(text: str | None) -> tuple[int | None, int | None]:
    """Extract (start_year, end_year) from Swedish date strings."""
    if not text:
        return (None, None)
    years = [int(y) for y in re.findall(r"(\d{4})", text)]
    if not years:
        return (None, None)
    return (min(years), max(years))


def _extract_dao(did_el) -> tuple[str, str, str]:
    """Extract bildvisning URL, IIIF manifest, and thumbnail from dao elements inside did."""
    bildvisning = ""
    manifest = ""
    thumbnail = ""
    for dao in did_el.findall(_tag("dao")):
        role = dao.get(f"{{{XLINK}}}role", "")
        href = dao.get(f"{{{XLINK}}}href", "")
        if role == "TEXT" and "bildvisning" in href:
            bildvisning = href.replace("?partner=ape", "")
        elif role == "MANIFEST":
            manifest = href
        elif role == "IMAGE":
            thumbnail = href
    return bildvisning, manifest, thumbnail


def _find_nearest_series(vol_el, parent_map) -> tuple[str, str]:
    """Walk up from a volume to find its nearest series ancestor."""
    el = vol_el
    while el in parent_map:
        el = parent_map[el]
        if el.tag == _tag("c") and el.get("level") == "series":
            s_did = el.find(_tag("did"))
            if s_did is not None:
                return _text(s_did, _tag("unitid")), _text(s_did, _tag("unittitle"))
            return "", ""
    return "", ""


def parse_ead_file(path: str) -> list[dict]:
    """Parse one APE EAD XML file, return list of volume dicts."""
    tree = ET.parse(path)  # noqa: S314 — trusted local archive data
    root = tree.getroot()

    # Fonds-level metadata from eadid
    eadid_el = root.find(f".//{_tag('eadid')}")
    eadid = eadid_el.text.strip() if eadid_el is not None and eadid_el.text else ""
    # archive_code from mainagencycode: "SE-KrA" -> "SE_KrA"
    agency = eadid_el.get("mainagencycode", "") if eadid_el is not None else ""
    archive_code = agency.replace("-", "_")

    # archdesc/did for fonds-level info
    fonds_did = root.find(f".//{_tag('archdesc')}/{_tag('did')}")
    if fonds_did is None:
        return []

    fonds_uid = _text(fonds_did, _tag("unitid"))
    fonds_title = _text(fonds_did, _tag("unittitle"))
    fonds_date = _text(fonds_did, _tag("unitdate"))

    # Physical extent (e.g. "71,4" hyllmeter)
    phys_el = fonds_did.find(_tag("physdesc"))
    extent_parts = []
    if phys_el is not None:
        for ext in phys_el.findall(_tag("extent")):
            if ext.text:
                extent_parts.append(ext.text.strip())
    fonds_extent = ", ".join(extent_parts)

    # Fonds description: concatenate <scopecontent>/<p> and <odd>/<p> at archdesc level
    archdesc = root.find(f".//{_tag('archdesc')}")
    fonds_desc_parts = []
    if archdesc is not None:
        for container_tag in ("scopecontent", "odd"):
            for container in archdesc.findall(_tag(container_tag)):
                for p in container.findall(_tag("p")):
                    if p.text:
                        fonds_desc_parts.append(p.text.strip())
    fonds_description = " ".join(fonds_desc_parts)

    # Fonds-level access restriction
    fonds_access = ""
    if archdesc is not None:
        ar = archdesc.find(_tag("accessrestrict"))
        if ar is not None:
            ar_parts = []
            for p in ar.findall(_tag("p")):
                if p.text:
                    ar_parts.append(p.text.strip())
            fonds_access = " ".join(ar_parts)

    # Creator
    creator_el = root.find(f".//{_tag('origination')}/{_tag('corpname')}")
    if creator_el is None:
        creator_el = root.find(f".//{_tag('origination')}/{_tag('persname')}")
    if creator_el is None:
        creator_el = root.find(f".//{_tag('origination')}/{_tag('name')}")
    creator = (creator_el.text or "").strip() if creator_el is not None else ""

    # Walk all c elements recursively — APE EAD has variable nesting depth
    rows = []
    # Build parent map once per file (not per volume)
    parent_map = {c: p for p in root.iter() for c in p}

    for vol_el in root.iter(_tag("c")):
        if vol_el.get("otherlevel") != "volym":
            continue

        v_did = vol_el.find(_tag("did"))
        if v_did is None:
            continue

        vol_ref = _text(v_did, _tag("unitid"))
        volume_title = _text(v_did, _tag("unittitle"))
        date_text = _text(v_did, _tag("unitdate"))
        date_start, date_end = parse_date_range(date_text)

        # Digitized: check for dao elements inside did
        bildvisning_url, iiif_manifest, thumbnail_url = _extract_dao(v_did)
        digitized = bool(bildvisning_url)

        # NAD catalog link from otherfindaid/extref
        nad_url = ""
        ofa = vol_el.find(_tag("otherfindaid"))
        if ofa is not None:
            ext = ofa.find(f".//{_tag('extref')}")
            if ext is not None:
                nad_url = ext.get(f"{{{XLINK}}}href", "")

        # Volume description
        vol_desc_parts = []
        for container_tag in ("scopecontent", "odd"):
            for container in vol_el.findall(_tag(container_tag)):
                for p in container.findall(_tag("p")):
                    if p.text:
                        vol_desc_parts.append(p.text.strip())
        description = " ".join(vol_desc_parts)

        # Access restriction (volume-level overrides fonds-level)
        vol_access = ""
        ar = vol_el.find(_tag("accessrestrict"))
        if ar is not None:
            ar_parts = []
            for p in ar.findall(_tag("p")):
                if p.text:
                    ar_parts.append(p.text.strip())
            vol_access = " ".join(ar_parts)
        access_restriction = vol_access or fonds_access

        # Find nearest series ancestor
        series_id, series_title = _find_nearest_series(vol_el, parent_map)

        # vol_ref contains the full reference code (e.g. SE/KrA/0078/A/001:Ö/A/1)
        # Extract just the volume number (last segment)
        vol_id = vol_ref.rsplit("/", 1)[-1] if "/" in vol_ref else vol_ref

        search_text = " ".join(
            part
            for part in [
                fonds_title,
                fonds_description,
                series_title,
                volume_title,
                description,
                creator,
                vol_ref,
                vol_id,
                date_text,
            ]
            if part
        )

        rows.append(
            {
                "id": vol_ref,
                "reference_code": vol_ref,
                "archive_code": archive_code,
                "fonds_id": fonds_uid,
                "fonds_title": fonds_title,
                "fonds_date": fonds_date,
                "fonds_description": fonds_description,
                "fonds_extent": fonds_extent,
                "creator": creator,
                "series_id": series_id,
                "series_title": series_title,
                "volume_id": vol_id,
                "volume_title": volume_title,
                "date_text": date_text,
                "date_start": date_start,
                "date_end": date_end,
                "description": description,
                "access_restriction": access_restriction,
                "digitized": digitized,
                "bild_id": bildvisning_url.rsplit("/", 1)[-1] if bildvisning_url else "",
                "bildvisning_url": bildvisning_url,
                "iiif_manifest": iiif_manifest,
                "thumbnail_url": thumbnail_url,
                "nad_url": nad_url,
                "search_text": search_text,
            }
        )

    return rows


def walk_archive_dir(directory: str, limit: int | None = None):
    """Yield volume dicts from all XML files in a directory."""
    count = 0
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".xml"):
            continue
        if limit is not None and count >= limit:
            break
        try:
            rows = parse_ead_file(os.path.join(directory, fname))
            yield from rows
            count += 1
        except ET.ParseError as e:
            print(f"Skipping {fname}: {e}")


def create_embedder():
    # Undeclared on purpose: it pulls torch, which the seal keeps out of the fleet.
    # Run with `uv run --with sentence-transformers`.
    from sentence_transformers import SentenceTransformer  # ty: ignore[unresolved-import]

    return SentenceTransformer(EMBED_MODEL, truncate_dim=EMBED_DIM)


def embed_documents(embedder, texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Embed documents (no prefix for Arctic). Sub-batches to limit memory."""
    all_vecs = []
    for i in range(0, len(texts), batch_size):
        sub = texts[i : i + batch_size]
        vecs = embedder.encode(sub, normalize_embeddings=True, show_progress_bar=False)
        all_vecs.extend(v.tolist() for v in vecs)
    return all_vecs


def embed_query(embedder, query: str) -> list[float]:
    """Embed a search query (Arctic requires 'query: ' prefix)."""
    vec = embedder.encode(f"query: {query}", normalize_embeddings=True)
    return vec.tolist()


def embed_documents_remote(texts: list[str], gpu_server: str, batch_size: int = 64) -> list[list[float]]:
    """Embed documents via GPU server /embed endpoint with retries."""
    import time

    import httpx

    all_vecs = []
    for i in range(0, len(texts), batch_size):
        sub = [t[:500] for t in texts[i : i + batch_size]]
        for attempt in range(10):
            try:
                resp = httpx.post(f"{gpu_server}/embed", json={"texts": sub, "mode": "document"}, timeout=300)
                resp.raise_for_status()
                all_vecs.extend(resp.json()["vectors"])
                break
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout) as e:
                if isinstance(e, httpx.HTTPStatusError):
                    print(f"  Response body: {e.response.text[:500]}")
                if attempt < 9:
                    wait = min(2**attempt, 60)
                    print(f"  Embed request failed ({e}), retrying in {wait}s... (attempt {attempt + 1}/10)")
                    time.sleep(wait)
                else:
                    raise
    return all_vecs


def ingest_rows(
    rows: list[dict],
    db_path: str,
    batch_size: int = 10_000,
    embed: bool = True,
) -> dict:
    """Write rows to LanceDB archive_catalog table in batches."""
    db = _connect(db_path)
    embedder = create_embedder() if embed else None

    written = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]

        if embedder:
            vectors = embed_documents(embedder, [r["search_text"] for r in batch])
            for row, vec in zip(batch, vectors, strict=True):
                row["vector"] = vec
        else:
            for row in batch:
                row["vector"] = [0.0] * EMBED_DIM

        arrow_batch = pa.Table.from_pylist(batch, schema=CATALOG_SCHEMA)

        if written == 0:
            table = db.create_table(CATALOG_TABLE, arrow_batch, mode="overwrite")
        else:
            table = db.open_table(CATALOG_TABLE)
            table.add(arrow_batch)

        written += len(batch)
        print(f"  Written {written} rows...")

    return {"rows_written": written}


def build_fts_index(db_path: str):
    """Build FTS word-level index on search_text column."""
    db = _connect(db_path)
    table = db.open_table(CATALOG_TABLE)
    try:
        table.create_fts_index(
            "search_text",
            replace=True,
        )
        print(f"FTS index built on {table.count_rows()} rows")
    except Exception as e:
        print(f"FTS index failed: {e}")


def _stream_all_dirs(data_dir: str, sample: int | None = None):
    """Yield volume dicts from all archive subdirectories, streaming."""
    for archive_dir in sorted(os.listdir(data_dir)):
        full_path = os.path.join(data_dir, archive_dir)
        if not os.path.isdir(full_path):
            continue
        yield from walk_archive_dir(full_path, limit=sample)


def ingest_streaming(
    rows_iter,
    db_path: str,
    batch_size: int = 10_000,
    embed: bool = True,
    gpu_server: str | None = None,
    digitized_only: bool = False,
) -> int:
    """Stream rows into LanceDB in fixed-size batches. Memory-efficient."""
    db = _connect(db_path)
    embedder = create_embedder() if embed and not gpu_server else None
    written = 0
    batch: list[dict] = []

    def flush(batch: list[dict], first: bool) -> None:
        nonlocal written
        if gpu_server:
            vectors = embed_documents_remote([r["search_text"] for r in batch], gpu_server)
            for row, vec in zip(batch, vectors, strict=True):
                row["vector"] = vec
        elif embedder:
            vectors = embed_documents(embedder, [r["search_text"] for r in batch])
            for row, vec in zip(batch, vectors, strict=True):
                row["vector"] = vec
        else:
            for row in batch:
                row["vector"] = [0.0] * EMBED_DIM
        arrow_batch = pa.Table.from_pylist(batch, schema=CATALOG_SCHEMA)
        if first:
            db.create_table(CATALOG_TABLE, arrow_batch, mode="overwrite")
        else:
            db.open_table(CATALOG_TABLE).add(arrow_batch)
        written += len(batch)
        print(f"  Written {written} rows...")

    for row in rows_iter:
        if digitized_only and not row.get("digitized"):
            continue
        batch.append(row)
        if len(batch) >= batch_size:
            flush(batch, first=(written == 0))
            batch = []

    if batch:
        flush(batch, first=(written == 0))

    return written


def main():
    import argparse
    import time

    parser = argparse.ArgumentParser(description="Ingest Riksarkivet EAD metadata into LanceDB")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    default_data = os.path.join(repo_root, ".cache", "lejonet-xml")
    parser.add_argument("data_dir", nargs="?", default=default_data, help="Path to XML directory (default: <repo>/.cache/lejonet-xml)")
    parser.add_argument(
        "--db-path",
        default="s3://images-batch-search",
        help="LanceDB directory or s3:// URI (default: s3://images-batch-search alongside lines.lance)",
    )
    parser.add_argument("--sample", type=int, default=0, help="Only process N files per archive (0=all)")
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument("--no-embed", action="store_true", help="Skip embedding (for testing)")
    parser.add_argument("--gpu-server", type=str, default=None, help="GPU server URL for remote embedding (e.g. http://localhost:8080)")
    parser.add_argument("--digitized-only", action="store_true", help="Only ingest digitized volumes")
    args = parser.parse_args()

    t0 = time.time()
    sample = args.sample or None

    print("Streaming parse + ingest...")
    rows_iter = _stream_all_dirs(args.data_dir, sample=sample)
    written = ingest_streaming(
        rows_iter,
        args.db_path,
        batch_size=args.batch_size,
        embed=not args.no_embed,
        gpu_server=args.gpu_server,
        digitized_only=args.digitized_only,
    )
    print(f"Wrote {written} rows to {args.db_path} in {time.time() - t0:.1f}s")

    if written == 0:
        print("No volumes found, exiting.")
        return

    print("Building FTS index...")
    build_fts_index(args.db_path)

    print(f"Done in {time.time() - t0:.1f}s total")


if __name__ == "__main__":
    main()
