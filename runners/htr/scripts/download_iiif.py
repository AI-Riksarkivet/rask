"""Download IIIF images for a batch to a local directory.

Fetches the IIIF manifest, then pulls each page's image via the IIIF Image API
(reusing storage.iiif helpers so URL/manifest shapes stay in one place).

Usage:
    uv run python scripts/download_iiif.py --batch A0060198 --out /tmp/iiif
    uv run python scripts/download_iiif.py --batch A0060198 --limit 3 --size 'full/,800/0/default.jpg'
"""

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from htr.iiif import DEFAULT_IIIF_BASE, build_image_url, file_extension, get_image_ids


logger = logging.getLogger(__name__)


def fetch(client: httpx.Client, url: str, dest: Path) -> tuple[str, str]:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return (url, f"HTTP {e.response.status_code}")
    except httpx.HTTPError as e:
        return (url, f"ERROR {type(e).__name__}: {e}")
    dest.write_bytes(resp.content)
    return (url, f"OK {len(resp.content):,}B")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--batch", required=True, help="batch / volume id, e.g. A0060198")
    ap.add_argument("--out", type=Path, default=Path("/tmp/iiif"))  # noqa: S108 (smoke-test default; override with --out)
    ap.add_argument("--base-url", default=DEFAULT_IIIF_BASE)
    ap.add_argument("--size", default="full/max/0/default.jpg", help="IIIF query params (region/size/rotation/quality.format)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=None, help="only fetch the first N pages (smoke test)")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    out_dir = args.out / args.batch
    out_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=args.timeout) as client:
        image_ids = get_image_ids(args.batch, base_url=args.base_url, client=client)
        if args.limit:
            image_ids = image_ids[: args.limit]
        print(f"{len(image_ids)} image(s) to fetch into {out_dir}")

        ext = file_extension(args.size)
        jobs = [(build_image_url(img, base_url=args.base_url, query_params=args.size), out_dir / f"{img}{ext}") for img in image_ids]
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(lambda j: fetch(client, *j), jobs))

    n_ok = sum(1 for _, s in results if s.startswith("OK"))
    n_err = len(results) - n_ok
    print(f"\nDownloaded {n_ok}/{len(results)} to {out_dir}")
    if n_err:
        print(f"  errors: {n_err}")
        for url, status in results:
            if not status.startswith("OK"):
                print(f"  {url} -> {status}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
