#!/usr/bin/env python3
"""Seed a LanceDB database on HCP S3 with sample tables for testing.

Usage:
    # Using env vars from .env (reads HCP_DOMAIN, prompts for credentials):
    python scripts/seed_lance_data.py --tenant myTenant --bucket lance-test

    # With explicit path inside the bucket:
    python scripts/seed_lance_data.py --tenant myTenant --bucket lance-test --path data/lance

    # With explicit credentials:
    python scripts/seed_lance_data.py --tenant myTenant --bucket lance-test \
        --username admin --password secret123

    # Skip SSL verification (common for dev):
    python scripts/seed_lance_data.py --tenant myTenant --bucket lance-test --no-verify-ssl

This creates three tables in s3://<bucket>/<path>/:
  - documents  : text + metadata + 4-dim embedding vectors
  - metrics    : time-series sensor data
  - images     : records with binary-like columns (simulated as blobs)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import random
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env from repo root
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader — no external dependency needed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def derive_s3_keys(username: str, password: str) -> tuple[str, str]:
    """Derive HCP S3 credentials: base64(user), md5(password)."""
    access_key = base64.b64encode(username.encode()).decode()
    secret_key = hashlib.md5(password.encode()).hexdigest()  # noqa: S324 — HCP protocol requires MD5
    return access_key, secret_key


def make_documents_data() -> list[dict]:
    """Sample text documents with embedding vectors."""
    texts = [
        "The quick brown fox jumps over the lazy dog",
        "Machine learning models require large datasets",
        "HCP provides scalable object storage for enterprises",
        "LanceDB enables fast vector search on cloud storage",
        "Python is widely used for data science workflows",
        "Kubernetes orchestrates containerized applications",
        "Neural networks learn hierarchical representations",
        "Data lakes consolidate structured and unstructured data",
        "REST APIs enable microservices communication",
        "Cloud-native architectures improve scalability",
    ]
    categories = ["tech", "science", "storage", "ai", "general"]

    random.seed(42)
    rows = []
    for i, text in enumerate(texts):
        vec = [random.gauss(0, 1) for _ in range(4)]
        rows.append(
            {
                "id": i + 1,
                "text": text,
                "category": categories[i % len(categories)],
                "score": round(random.uniform(0.5, 1.0), 3),
                "vector": vec,
            }
        )
    return rows


def make_metrics_data() -> list[dict]:
    """Sample time-series sensor metrics."""
    random.seed(123)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sensors = ["temp-01", "temp-02", "humidity-01", "pressure-01"]
    rows = []
    for i in range(40):
        ts = base_time + timedelta(hours=i)
        sensor = sensors[i % len(sensors)]
        rows.append(
            {
                "timestamp": ts.isoformat(),
                "sensor_id": sensor,
                "value": round(random.uniform(15.0, 35.0), 2),
                "unit": "celsius"
                if "temp" in sensor
                else ("%" if "humidity" in sensor else "hPa"),
                "is_anomaly": random.random() < 0.1,
            }
        )
    return rows


def _download_image(url: str) -> bytes:
    """Download an image from a URL, returning raw bytes.

    Uses an unverified SSL context since certificate verification may fail
    for the demo endpoints.  Falls back to a small placeholder blob on error.
    """
    placeholder = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56  # 64-byte placeholder
    try:
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, headers={"User-Agent": "seed-script/1.0"})
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            data = resp.read()
            if data:
                return data
            return placeholder
    except Exception as exc:
        print(f"\n    [warn] download failed for {url}: {exc}")
        return placeholder


def make_images_data() -> list[dict]:
    """Download real images and store as binary data."""
    image_urls = [
        "https://riksarkivet-htr-demo.hf.space/gradio_api/file=/tmp/gradio/2799cab70cd5d1da347625baf9b44082df1726f558f1068c4a28f22a51ee512d/451511_1512_01.jpg",
        "https://riksarkivet-htr-demo.hf.space/gradio_api/file=/tmp/gradio/3ae36f7a533bff7bd06352257920c06c84de674011553ed8fc113adae47440d3/30002027_00008.jpg",
        "https://riksarkivet-htr-demo.hf.space/gradio_api/file=/tmp/gradio/053a755d8b5459e31f9bcf0a768c911bdf9fc735a2c7e7d0874906cf0b8723c5/A0062408_00006.jpg",
    ]

    # Download the three source images (with fallback)
    print("\n  Downloading source images...")
    downloaded: list[tuple[str, bytes]] = []
    for url in image_urls:
        filename = url.rsplit("/", 1)[-1]
        print(f"    {filename}... ", end="", flush=True)
        data = _download_image(url)
        downloaded.append((filename, data))
        print(f"{len(data)} bytes")

    # Cycle through the 3 images to fill 8 rows
    random.seed(456)
    rows = []
    for i in range(8):
        filename, blob = downloaded[i % len(downloaded)]
        rows.append(
            {
                "id": i + 1,
                "filename": filename,
                "width": random.choice([64, 128, 256]),
                "height": random.choice([64, 128, 256]),
                "image_data": blob,
            }
        )
    return rows


def main() -> None:
    load_dotenv(ENV_FILE)

    parser = argparse.ArgumentParser(description="Seed LanceDB tables on HCP S3")
    parser.add_argument("--tenant", required=True, help="HCP tenant name")
    parser.add_argument(
        "--bucket", required=True, help="S3 bucket name (e.g. lance-test)"
    )
    parser.add_argument(
        "--path", default="", help="Optional path inside bucket (e.g. data/lance)"
    )
    parser.add_argument(
        "--username", default=os.environ.get("HCP_USERNAME", ""), help="HCP username"
    )
    parser.add_argument(
        "--password", default=os.environ.get("HCP_PASSWORD", ""), help="HCP password"
    )
    parser.add_argument(
        "--domain", default=os.environ.get("HCP_DOMAIN", ""), help="HCP domain"
    )
    parser.add_argument(
        "--no-verify-ssl", action="store_true", help="Disable SSL verification"
    )
    args = parser.parse_args()

    if not args.username or not args.password:
        print(
            "Error: --username and --password required (or set HCP_USERNAME/HCP_PASSWORD)"
        )
        sys.exit(1)

    if not args.domain:
        print("Error: --domain required (or set HCP_DOMAIN in .env)")
        sys.exit(1)

    # Derive S3 credentials (HCP convention)
    access_key, secret_key = derive_s3_keys(args.username, args.password)
    endpoint_url = f"https://{args.tenant}.{args.domain}"

    # Build the S3 URI
    s3_uri = f"s3://{args.bucket}/{args.path}" if args.path else f"s3://{args.bucket}"

    print(f"Connecting to LanceDB at {s3_uri}")
    print(f"Endpoint: {endpoint_url}")

    try:
        import lancedb  # ty: ignore[unresolved-import]
    except ImportError:
        print("Error: lancedb not installed. Run: pip install lancedb pyarrow")
        sys.exit(1)

    storage_options = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "endpoint_url": endpoint_url,
        "allow_http": "false",
    }

    if args.no_verify_ssl:
        storage_options["aws_allow_invalid_certificates"] = "true"

    try:
        db = lancedb.connect(s3_uri, storage_options=storage_options)
    except Exception as e:
        print(f"Failed to connect: {e}")
        sys.exit(1)

    # Create tables
    tables = {
        "documents": make_documents_data(),
        "metrics": make_metrics_data(),
        "images": make_images_data(),
    }

    for name, data in tables.items():
        print(
            f"  Creating table '{name}' with {len(data)} rows... ", end="", flush=True
        )
        try:
            # Drop if exists, then recreate
            try:
                db.drop_table(name)
            except Exception:
                pass
            table = db.create_table(name, data)
            if name == "documents":
                try:
                    table.create_fts_index("text")
                    print("(FTS index created) ", end="")
                except Exception as e:
                    print(f"(FTS index skipped: {e}) ", end="")
            print("OK")
        except Exception as e:
            print(f"FAILED: {e}")

    # Verify
    print("\nVerifying tables:")
    try:
        result = db.list_tables()
        names: list[str] = (
            result.tables
            if hasattr(result, "tables")
            else result.get("tables", [])
            if isinstance(result, dict)
            else list(result)
        )
        for name in sorted(names):
            t = db.open_table(name)
            count = t.count_rows()
            print(f"  {name}: {count} rows, {len(t.schema)} columns")
    except Exception as e:
        print(f"  Verification failed: {e}")

    print("\nDone! In the Data Explorer UI, enter:")
    print(f"  Bucket: {args.bucket}")
    if args.path:
        print(f"  Path:   {args.path}")
    print("  Then click 'Load Tables'")


if __name__ == "__main__":
    main()
