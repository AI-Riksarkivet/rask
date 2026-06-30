#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx==0.28.1",
# ]
# ///
"""Seed a rask cluster with the Riksarkivet htr_demo example images.

Pulls the handwritten-document examples bundled in the htr_demo Hugging Face
Space and uploads them as a single batch via the gateway's
`POST /api/batches/<id>/upload` endpoint (which puts them in the images-batch
bucket and registers the volume, so the orchestrator can pick it up for HTR).

Standalone uv script — no workspace sync needed:

    uv run scripts/seed_demo_batch.py                  # -> http://localhost (k3s ingress)
    uv run scripts/seed_demo_batch.py --base-url http://rask.local
    uv run scripts/seed_demo_batch.py --batch-id htr-demo

Idempotent: re-running re-uploads the same files and re-registers the volume.
"""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import PurePosixPath

import httpx

# Pinned source: the example images live under this prefix in the Space repo.
SPACE_ID = "Riksarkivet/htr_demo"
SPACE_REVISION = "main"
EXAMPLES_PREFIX = ".gradio_cache/examples/"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

HF_API = "https://huggingface.co/api/spaces/{repo}/tree/{rev}"
HF_RESOLVE = "https://huggingface.co/spaces/{repo}/resolve/{rev}/{path}"


def list_example_images(client: httpx.Client) -> list[str]:
    """Return the example-image paths in the Space, newest tree, sorted."""
    resp = client.get(
        HF_API.format(repo=SPACE_ID, rev=SPACE_REVISION),
        params={"recursive": "true"},
    )
    resp.raise_for_status()
    paths = [
        entry["path"]
        for entry in resp.json()
        if entry.get("type") == "file" and entry["path"].startswith(EXAMPLES_PREFIX) and entry["path"].lower().endswith(IMAGE_SUFFIXES)
    ]
    return sorted(paths)


def download(client: httpx.Client, path: str) -> bytes:
    resp = client.get(HF_RESOLVE.format(repo=SPACE_ID, rev=SPACE_REVISION, path=path))
    resp.raise_for_status()
    return resp.content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://localhost",
        help="rask gateway/ingress base URL (default: http://localhost — the k3s ingress)",
    )
    parser.add_argument(
        "--batch-id",
        default="htr-demo",
        help="volume id to create (letters, digits, '-', '_'; default: htr-demo)",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="per-request timeout in seconds")
    args = parser.parse_args()

    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        print(f"Listing example images in {SPACE_ID} ...")
        paths = list_example_images(client)
        if not paths:
            print("ERROR: no example images found in the Space.", file=sys.stderr)
            return 1
        print(f"Found {len(paths)} images. Downloading ...")

        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for path in paths:
            name = PurePosixPath(path).name
            data = download(client, path)
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            files.append(("files", (name, data, content_type)))
            print(f"  + {name} ({len(data) // 1024} KiB)")

        url = f"{args.base_url.rstrip('/')}/api/batches/{args.batch_id}/upload"
        print(f"Uploading {len(files)} files to {url} ...")
        resp = client.post(url, files=files)
        if resp.status_code >= 400:
            print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
            return 1

        batch = resp.json()
        print(
            f"Done. Batch {batch.get('batch_id')!r}: page_count={batch.get('page_count')} htr_status={batch.get('htr_status')} chunk_id={batch.get('chunk_id')}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
