"""Orphan-artifact janitor for the model lane (§9 blob-pointer lifecycle, Batch 4 2026-07-11).

The D4 registration order (bytes FIRST, then the atomic registry commit — docs/RAY-TRAIN.md) means
a training run that crashes between the two steps leaves plain artifact objects under
``<artifact_base>/<token>/`` that NO registry row points at — by design (never a half-registered
model). This janitor is the cleanup half of that contract:

1. read the model's registry (a Lance dataset) at ONE pinned version and collect every token any
   row's ``meta`` references — the REFERENCED set;
2. list the first-level ``<token>/`` prefixes under the artifact base with the age of their NEWEST
   object (a token still being written looks young);
3. report tokens that are BOTH unreferenced AND older than the TTL. Deletion happens ONLY with
   ``--delete`` — the default is a dry-run report.

FAIL-SAFE DIRECTION IS KEEP (🚧 batch guardrails): a registry read failure aborts before any
delete; a token whose meta cannot be parsed marks the WHOLE run report-only (an unreadable
registry row could reference anything); any per-token listing error skips that token; and the
janitor refuses outright if the registry and artifact trees overlap (it must never enumerate a
Lance dataset directory). The invariant "a referenced token is NEVER deleted, even past TTL" is
pinned by tests/unit/test_model_artifact_janitor.py — written before this deletion code existed.

Usage (one model per invocation; loop for many):
    uv run python scripts/model_artifact_janitor.py \
        --registry-uri s3://lake/medallion/models/churn \
        --artifact-base s3://lake/models/churn \
        [--ttl-hours 24] [--delete] [--s3-endpoint http://… --s3-key … --s3-secret …]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class JanitorReport:
    """What one sweep saw and (only with ``delete=True``) did."""

    referenced: set[str] = field(default_factory=set)
    kept_referenced: list[str] = field(default_factory=list)
    kept_young: list[str] = field(default_factory=list)
    kept_on_error: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)  # unreferenced + past TTL
    deleted: list[str] = field(default_factory=list)  # only ever populated with delete=True
    report_only_reason: str | None = None  # set → deletion was refused regardless of the flag


# The moving tag that names the promoted ("blessed") model version — kept byte-identical to
# ``catalog.services.models.BLESSED_TAG`` (this script is standalone, so the constant is inlined).
_BLESSED_TAG = "blessed"


def _blessed_version(ds: Any) -> int | None:
    """The Lance version the ``blessed`` tag points at, or ``None`` if no version is blessed / tags absent."""
    try:
        version = ds.tags.get_version(_BLESSED_TAG)
    except Exception:
        return None
    return int(version) if version is not None else None


def referenced_tokens(registry_uri: str, storage_options: dict[str, str] | None) -> set[str] | None:
    """Every token any registry row references, read at the latest version PLUS the blessed version.

    Returns ``None`` when the registry cannot be read or ANY row's meta cannot be parsed — the
    caller must then refuse to delete (an unreadable row could reference anything; fail-safe=keep).

    The registry is append-only, so the latest version already carries every publish's tokens — but a
    BLESSED (possibly non-latest) version's artifacts must be protected as a first-class invariant, robust to
    any future retention/compaction that could drop old rows from latest. So the blessed tag's version is read
    too and its tokens unioned in; a failure reading it is fail-safe=keep (returns ``None``), same as latest.
    """
    import lance

    try:
        ds = lance.dataset(registry_uri, storage_options=storage_options)
        latest = int(ds.version)
        pinned = lance.dataset(registry_uri, version=latest, storage_options=storage_options)
        metas = pinned.to_table(columns=["meta"]).column("meta").to_pylist()
        blessed = _blessed_version(ds)
        if blessed is not None and blessed != latest:
            blessed_ds = lance.dataset(registry_uri, version=blessed, storage_options=storage_options)
            metas = metas + blessed_ds.to_table(columns=["meta"]).column("meta").to_pylist()
    except Exception as exc:
        print(f"registry read failed ({exc}); refusing to delete anything", file=sys.stderr)
        return None
    tokens: set[str] = set()
    for meta in metas:
        try:
            token = json.loads(meta)["token"]
        except Exception:
            print("unparseable registry meta row; refusing to delete anything", file=sys.stderr)
            return None
        if not isinstance(token, str) or not token:
            print("registry meta row without a token; refusing to delete anything", file=sys.stderr)
            return None
        tokens.add(token)
    return tokens


def _filesystem(base: str, storage_options: dict[str, str] | None) -> tuple[Any, str]:
    """(pyarrow FileSystem, base path within it) for an s3:// or local artifact base."""
    import pyarrow.fs as pafs

    if base.startswith("s3://"):
        so = storage_options or {}
        scheme, _, host = so.get("endpoint", "").partition("://")
        fs = pafs.S3FileSystem(
            endpoint_override=host or None,
            access_key=so.get("access_key_id"),
            secret_key=so.get("secret_access_key"),
            scheme=scheme or "https",
            region=so.get("region", "us-east-1"),
        )
        return fs, base.removeprefix("s3://").rstrip("/")
    return pafs.LocalFileSystem(), base.rstrip("/")


def token_ages(base: str, storage_options: dict[str, str] | None) -> dict[str, datetime | None]:
    """First-level ``<token>/`` prefixes under the base → the mtime of their NEWEST object.

    ``None`` age = the token's objects could not be statted (the caller keeps it — fail-safe).
    """
    import pyarrow.fs as pafs

    fs, root = _filesystem(base, storage_options)
    try:
        # allow_not_found: a missing base is an EMPTY sweep, not an error — and not every pyarrow
        # filesystem raises the same exception type for a missing selector base.
        entries = fs.get_file_info(pafs.FileSelector(root, recursive=False, allow_not_found=True))
    except FileNotFoundError:
        return {}
    ages: dict[str, datetime | None] = {}
    for entry in entries:
        if entry.type != pafs.FileType.Directory:
            continue  # tokens are directories; stray top-level objects are not ours to touch
        token = entry.path.rsplit("/", 1)[-1]
        try:
            files = fs.get_file_info(pafs.FileSelector(entry.path, recursive=True))
            mtimes = [f.mtime for f in files if f.mtime is not None]
            newest = max(mtimes) if mtimes else None
            ages[token] = newest.astimezone(UTC) if newest is not None else None
        except Exception:
            ages[token] = None
    return ages


def sweep(
    *,
    registry_uri: str,
    artifact_base: str,
    ttl_hours: float,
    delete: bool = False,
    storage_options: dict[str, str] | None = None,
    now: datetime | None = None,
) -> JanitorReport:
    """One janitor pass. Dry-run (report-only) unless ``delete=True`` AND nothing forced keep-all."""
    report = JanitorReport()
    reg = registry_uri.rstrip("/") + "/"
    base = artifact_base.rstrip("/") + "/"
    if reg.startswith(base) or base.startswith(reg):
        # The janitor must never enumerate (let alone delete inside) a Lance dataset directory.
        report.report_only_reason = "registry and artifact trees overlap; refusing to run"
        print(report.report_only_reason, file=sys.stderr)
        return report

    referenced = referenced_tokens(registry_uri, storage_options)
    if referenced is None:
        report.report_only_reason = "registry unreadable"
        referenced = set()
        delete = False  # fail-safe: without the referenced set, deletion is never allowed
    report.referenced = referenced

    cutoff = (now or datetime.now(UTC)) - timedelta(hours=ttl_hours)
    for token, age in sorted(token_ages(artifact_base, storage_options).items()):
        if token in referenced:
            report.kept_referenced.append(token)  # the invariant: referenced ⇒ never deleted
            continue
        if age is None:
            report.kept_on_error.append(token)
            continue
        if age > cutoff:
            report.kept_young.append(token)
            continue
        report.candidates.append(token)
        if delete:
            fs, root = _filesystem(artifact_base, storage_options)
            try:
                fs.delete_dir(f"{root}/{token}")
                report.deleted.append(token)
            except Exception as exc:
                print(f"delete failed for token {token}: {exc}", file=sys.stderr)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry-uri", required=True)
    parser.add_argument("--artifact-base", required=True)
    parser.add_argument("--ttl-hours", type=float, default=24.0)
    parser.add_argument("--delete", action="store_true", help="actually delete (default: dry-run report)")
    parser.add_argument("--s3-endpoint", default="")
    parser.add_argument("--s3-key", default="")
    parser.add_argument("--s3-secret", default="")
    parser.add_argument("--s3-region", default="us-east-1")
    args = parser.parse_args()

    so = None
    if args.registry_uri.startswith("s3://") or args.artifact_base.startswith("s3://"):
        so = {
            "endpoint": args.s3_endpoint,
            "access_key_id": args.s3_key,
            "secret_access_key": args.s3_secret,
            "region": args.s3_region,
            "allow_http": "true",
            "virtual_hosted_style_request": "false",
        }
    report = sweep(
        registry_uri=args.registry_uri,
        artifact_base=args.artifact_base,
        ttl_hours=args.ttl_hours,
        delete=args.delete,
        storage_options=so,
    )
    print(
        json.dumps(
            {
                "mode": "delete" if args.delete and not report.report_only_reason else "dry-run",
                "report_only_reason": report.report_only_reason,
                "referenced": sorted(report.referenced),
                "kept_referenced": report.kept_referenced,
                "kept_young": report.kept_young,
                "kept_on_error": report.kept_on_error,
                "candidates": report.candidates,
                "deleted": report.deleted,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
