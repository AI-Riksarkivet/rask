"""Download official ALTO XML from Riksarkivet for a given volume and page range.

Mirrors the URL pattern ra-mcp uses:
    https://sok.riksarkivet.se/dokument/alto/{first4}/{vol}/{vol}_{page:05d}.xml

Usage:
    uv run python download_ra_alto.py --volume A0060198 --start 1 --end 96 --out /home/morgan/_ra_alto
"""

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)

BASE = "https://sok.riksarkivet.se/dokument/alto"


def alto_url(volume: str, page: int) -> str:
    return f"{BASE}/{volume[:4]}/{volume}/{volume}_{page:05d}.xml"


def fetch(url: str, dest: Path) -> tuple[str, str]:
    req = Request(url, headers={"Accept": "application/xml, text/xml, */*", "User-Agent": "rask/0.1"})  # noqa: S310 (trusted https URL)
    try:
        with urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted base URL)
            data = resp.read()
    except HTTPError as e:
        if e.code == 404:
            return (url, "404 (no ALTO for this page)")
        return (url, f"HTTP {e.code}")
    except URLError as e:
        return (url, f"URLError: {e.reason}")
    except Exception as e:
        return (url, f"ERROR {type(e).__name__}: {e}")
    dest.write_bytes(data)
    return (url, f"OK {len(data):,}B")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--volume", required=True, help="e.g. A0060198")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, required=True, help="inclusive")
    ap.add_argument("--out", type=Path, default=Path("/home/morgan/_ra_alto"))
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    out_dir = args.out / args.volume
    out_dir.mkdir(parents=True, exist_ok=True)

    jobs = [(alto_url(args.volume, p), out_dir / f"{args.volume}_{p:05d}.xml") for p in range(args.start, args.end + 1)]
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda j: fetch(*j), jobs))

    n_ok = sum(1 for _, s in results if s.startswith("OK"))
    n_404 = sum(1 for _, s in results if s.startswith("404"))
    n_err = len(results) - n_ok - n_404
    print(f"\nDownloaded {n_ok}/{len(results)} to {out_dir}")
    print(f"  404 (no ALTO on RA): {n_404}")
    print(f"  errors:              {n_err}")
    if n_err:
        print("\nErrors:")
        for url, status in results:
            if not (status.startswith("OK") or status.startswith("404")):
                print(f"  {url} -> {status}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
