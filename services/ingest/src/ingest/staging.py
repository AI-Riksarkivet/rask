"""Where a unit's fragments live between the ack and the commit.

**This module is what makes the ack contract true.** The worker's rule is "fragment on disk BEFORE
the ack", and the ack is a promise that the work survived. But until now the only record that a
fragment existed was the worker's RETURN VALUE — so a pod that died after acking a unit and before
returning took that fragment's identity with it. The bytes were on the object store; nothing knew
their name. The next attempt could not recommit them (it had no FragmentMetadata) and could not
refetch them either (the ack had already removed the unit from a WORK_QUEUE stream). That is silent
data loss, and it is invisible: the run completes, reports fewer rows than it fetched, and nothing
anywhere says a page went missing.

So a fragment's IDENTITY is written durably, next to its bytes, before the unit is acked. The
staging prefix becomes the run's outstanding-commit ledger, and `finalize` reads it rather than
trusting anything carried through the workflow. Storage truth, exactly like `reconcile_from_queue`
asks the stream rather than a counter.

**Keyed by unit, never by fragment id.** Pre-commit fragment ids all collide at 0
(`lance_docs/guide.md:1576-1578`, confirmed on pylance 9.0.0), so a retried unit must overwrite its
own manifest and nothing else. Last write wins per unit, which makes a redelivered unit converge
instead of double-committing — the same idempotency the merge on `id` gives at the row level.

**Why not `storage.build_sink`.** `S3Sink.write` ignores its own prefix (it is applied in
`existing_keys` only, `packages/storage/src/storage/s3.py:118-128`), while `FSSink.write` honours
its root. Passing a relative key to the two builds different paths, so the scheme-agnostic seam is
not actually agnostic for writes. This module splits the scheme explicitly instead of relying on a
symmetry that is not there.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

#: Lives INSIDE the dataset directory on purpose: staging travels with the data it belongs to, so a
#: warehouse move or a bucket rename cannot separate a run's uncommitted fragments from their
#: dataset. Underscore-prefixed to sit beside Lance's own `_versions`/`_transactions` without
#: colliding with a data file.
STAGING_DIR = "_ingest_staging"


def _is_object_store(uri: str) -> bool:
    return "://" in uri and not uri.startswith("file://")


def staging_root(dataset_uri: str, run_id: str) -> str:
    """The run's staging location. Per-run, so purging one run cannot touch another's."""
    return f"{dataset_uri.rstrip('/')}/{STAGING_DIR}/{run_id}"


def manifest_name(unit_key: str) -> str:
    """A unit's manifest filename — a hash, so an arbitrary source URI is a safe object key.

    Source keys are URLs and paths: they carry slashes, query strings and unicode. Hashing gives a
    flat, fixed-width name that is legal on every store, and makes the overwrite-on-retry behaviour
    exact rather than dependent on how a store normalises a path.
    """
    return f"{hashlib.sha256(unit_key.encode()).hexdigest()[:32]}.json"


def stage_fragments(dataset_uri: str, run_id: str, unit_key: str, fragments_json: Sequence[str]) -> str:
    """Record a unit's fragments durably. MUST be called before the unit is acked.

    Returns the manifest key, so a caller can assert the write happened rather than assume it.
    """
    payload = json.dumps({"unit": unit_key, "fragments": list(fragments_json)}).encode()
    name = manifest_name(unit_key)
    root = staging_root(dataset_uri, run_id)

    if _is_object_store(root):
        bucket, prefix = _split(root)
        _client().put_object(Bucket=bucket, Key=f"{prefix}/{name}", Body=payload, ContentType="application/json")
    else:
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return name


def discover_staged(dataset_uri: str, run_id: str) -> list[str]:
    """Every fragment this run staged and has not yet committed — the finalizer's input.

    Deduplicated by fragment JSON: a unit whose manifest was rewritten on retry contributes once, and
    two units can never contribute the same fragment because each writes its own files.

    An absent staging prefix is an empty list, not an error: a run with no units never staged
    anything, and a run whose staging was already purged has nothing left to commit. Both are
    legitimate, and raising here would turn a successful no-op run into a failure.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in sorted(_read_all(staging_root(dataset_uri, run_id))):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            # A truncated manifest means the pod died mid-write, which means the unit was never
            # acked — so it is still on the queue and will be refetched. Skipping is correct;
            # failing the finalize over it would strand a run that the queue can still complete.
            continue
        for fragment in record.get("fragments", []):
            if fragment not in seen:
                seen.add(fragment)
                out.append(fragment)
    return out


def purge_staged(dataset_uri: str, run_id: str) -> int:
    """Drop the run's staging after its commit lands. Returns how many manifests were removed.

    Called only AFTER the commit succeeds. Purging earlier would delete the very record a retried
    finalize needs, converting a recoverable failure into the data loss this module exists to
    prevent.
    """
    root = staging_root(dataset_uri, run_id)
    removed = 0
    if _is_object_store(root):
        bucket, prefix = _split(root)
        client = _client()
        for key in _list_object_keys(bucket, prefix):
            client.delete_object(Bucket=bucket, Key=key)
            removed += 1
        return removed

    directory = Path(root)
    if not directory.exists():
        return 0
    for path in sorted(directory.glob("*.json")):
        path.unlink()
        removed += 1
    return removed


# ── scheme split ──────────────────────────────────────────────────────────────────────


def _split(uri: str) -> tuple[str, str]:
    from storage import split_s3_uri

    bucket, prefix = split_s3_uri(uri)
    return bucket, prefix.strip("/")


def _client() -> object:
    """The estate's sanctioned S3 wrapper. Never boto3 directly — `packages/storage` owns the
    endpoint/credential resolution that keeps this MinIO/RustFS/AWS-agnostic."""
    from storage import s3_client

    return s3_client(os.getenv("RASK_S3_ENDPOINT_URL"))


def _list_object_keys(bucket: str, prefix: str) -> list[str]:
    paginator = _client().get_paginator("list_objects_v2")  # type: ignore[attr-defined]
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        keys.extend(obj["Key"] for obj in page.get("Contents", []) if obj["Key"].endswith(".json"))
    return keys


def _read_all(root: str) -> Iterator[str]:
    if _is_object_store(root):
        bucket, prefix = _split(root)
        client = _client()
        for key in _list_object_keys(bucket, prefix):
            yield client.get_object(Bucket=bucket, Key=key)["Body"].read().decode()  # type: ignore[attr-defined]
        return

    directory = Path(root)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        yield path.read_text(encoding="utf-8")
