"""Download all Riksarkivet EAD XML files via OAI-PMH.

IMPORTANT: Always use the ``oai_ape_ead`` metadata prefix (Archives Portal
Europe format).  The ``oai_ra_ead`` prefix is missing ``<dao>`` elements that
mark digitised volumes with bildvisning/IIIF links — NEVER use it.

Fetches archive identifiers from each regional archive, then downloads
the full EAD record for each, stripping the OAI-PMH envelope.  Files are
saved to ``<output_dir>/<archive_code>/<identifier>.xml``.
"""

import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx


OAI_BASE = "https://oai-pmh.riksarkivet.se/OAI"
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"

# IMPORTANT: oai_ape_ead includes <dao> elements with bildvisning/IIIF links.
# oai_ra_ead does NOT — never use it.
METADATA_PREFIX = "oai_ape_ead"

ARCHIVE_CODES = [
    "SE_RA",
    "SE_KrA",
    "SE_GLA",
    "SE_HLA",
    "SE_LLA",
    "SE_ULA",
    "SE_VALA",
    "SE_ViLA",
    "SE_ÖLA",
]

# Known broken records that consistently time out on the OAI-PMH server
# (even at 300s timeout with 3 retries). Last attempted 2026-03-27.
KNOWN_FAILURES = [
    "SE/RA/8204",
    "SE/RA/83001",
    "SE/RA/83002",
    "SE/RA/83004",
    "SE/RA/83005",
    "SE/RA/83007",
    "SE/RA/83008",
    "SE/RA/83009",
    "SE/ULA/- 55555",  # bogus test record, returns 404
]

# Register EAD namespaces so ET.write() keeps them clean
ET.register_namespace("", "urn:isbn:1-931666-22-9")
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")

# Characters that appear in some records but break XML parsing
_BAD_CHAR_RE = re.compile(rb"&#x[0-9a-fA-F]+;")
_BULLET = "•".encode()


def _clean_xml(raw: bytes) -> bytes:
    """Remove invalid XML character references and stray bullets."""
    raw = _BAD_CHAR_RE.sub(b"", raw)
    return raw.replace(_BULLET, b"")


def _get(client: httpx.Client, url: str, retries: int = 3) -> httpx.Response:
    """GET with retries and exponential backoff."""
    for attempt in range(retries):
        try:
            resp = client.get(url, timeout=300)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout) as exc:
            if attempt >= retries - 1:
                raise
            wait = min(2 ** (attempt + 1), 30)
            print(f"  Retry {attempt + 1}/{retries} after {exc}, waiting {wait}s")
            time.sleep(wait)
    # Unreachable for retries >= 1: the loop either returns or re-raises. A
    # non-positive `retries` would otherwise fall through and return None.
    raise ValueError(f"retries must be >= 1, got {retries}")


def list_identifiers(client: httpx.Client, archive_code: str) -> list[str]:
    """Return all record identifiers for a given archive code."""
    resp = _get(client, f"{OAI_BASE}/{archive_code}?verb=ListIdentifiers")
    root = ET.fromstring(resp.content)  # noqa: S314 — trusted Riksarkivet OAI-PMH
    return [el.text for el in root.findall(f"./{OAI_NS}ListIdentifiers/{OAI_NS}header/{OAI_NS}identifier") if el.text]


def download_record(client: httpx.Client, identifier: str) -> ET.Element | None:
    """Download a single EAD record, stripping the OAI-PMH envelope."""
    url = f"{OAI_BASE}?verb=GetRecord&identifier={identifier}&metadataPrefix={METADATA_PREFIX}"
    resp = _get(client, url)
    cleaned = _clean_xml(resp.content)
    oai_root = ET.fromstring(cleaned)  # noqa: S314 — trusted Riksarkivet OAI-PMH
    ead_el = oai_root.find(f"./{OAI_NS}GetRecord/{OAI_NS}record/{OAI_NS}metadata/*")
    return ead_el


def harvest_archive(client: httpx.Client, archive_code: str, output_dir: Path) -> tuple[int, list[str]]:
    """Download all records for one archive. Returns (success_count, failed_ids)."""
    identifiers = list_identifiers(client, archive_code)
    print(f"  {archive_code}: {len(identifiers)} records", flush=True)

    archive_dir = output_dir / archive_code
    archive_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0
    failed = []
    for i, ident in enumerate(identifiers):
        safe_name = ident.replace("/", "_") + ".xml"
        dest = archive_dir / safe_name
        if dest.exists():
            skipped += 1
            continue

        try:
            ead = download_record(client, ident)
            if ead is None:
                print(f"    No EAD content in {ident}, skipping", flush=True)
                failed.append(ident)
                continue
            tree = ET.ElementTree(element=ead)
            tree.write(str(dest), encoding="UTF-8", xml_declaration=True)
            downloaded += 1
            if downloaded % 100 == 0:
                print(f"    {archive_code}: {downloaded} new + {skipped} cached / {len(identifiers)} total", flush=True)
        except Exception as exc:
            print(f"    Failed {ident}: {exc}", flush=True)
            failed.append(ident)

    return downloaded + skipped, failed


def harvest_all(output_dir: str | Path, codes: list[str] | None = None) -> dict:
    """Download all EAD XML files from Riksarkivet OAI-PMH.

    Args:
        output_dir: Directory to save XML files into (e.g. ``data/xml``).
        codes: Archive codes to harvest. Defaults to all 9 regional archives.

    Returns:
        Summary dict with counts.
    """
    output_dir = Path(output_dir)
    codes = codes or ARCHIVE_CODES

    total_ok = 0
    all_failed: list[str] = []

    with httpx.Client() as client:
        for code in codes:
            ok, fails = harvest_archive(client, code, output_dir)
            total_ok += ok
            all_failed.extend(fails)
            print(f"  {code}: {ok} downloaded, {len(fails)} failed", flush=True)

    return {"downloaded": total_ok, "failed": all_failed}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Harvest Riksarkivet EAD XML via OAI-PMH")
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    default_dir = os.path.join(repo_root, ".cache", "lejonet-xml")
    parser.add_argument("--output-dir", default=default_dir, help="Output directory (default: <repo>/.cache/lejonet-xml)")
    parser.add_argument("--codes", nargs="*", default=None, help="Archive codes to harvest (default: all)")
    args = parser.parse_args()

    t0 = time.time()
    print(f"Harvesting to {os.path.abspath(args.output_dir)}")
    result = harvest_all(args.output_dir, args.codes)
    elapsed = time.time() - t0

    print(f"\nDone in {elapsed:.0f}s — {result['downloaded']} records downloaded")
    if result["failed"]:
        print(f"{len(result['failed'])} failed:")
        for f in result["failed"]:
            print(f"  {f}")


if __name__ == "__main__":
    main()
