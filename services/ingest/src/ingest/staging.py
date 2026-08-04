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

**Keyed by the batch's UNIT SET, never by fragment id.** Pre-commit fragment ids all collide at 0
(`lance_docs/guide.md:1576-1578`, confirmed on pylance 9.0.0), so identity has to come from the work
a fragment represents. Re-running the same batch hashes to the same manifest and overwrites it, so a
retry converges instead of double-committing.

This used to read "keyed by unit", and that was true while a fragment covered exactly one unit.
Fragment batching made it cover N, and `flush()` staged the batch under `units[0][0]` — one
arbitrary member — leaving the other N-1 with no manifest at all. That is not a weaker version of
the same guarantee, it is the guarantee's premise removed: a redelivered unit had nothing of its own
to overwrite, so the old fragment and the new one both survived into `discover_staged` and the
lander appended both. Four units in, six rows out — `tests/test_partial_ack_duplication.py`.

Keying on the SET rather than per-unit keeps ONE manifest per batch. Per-unit would put 10k tiny
JSON objects on the store for a 10k-page volume, recreating the small-file problem batching had just
solved, and it would not even fix this: a fragment is committed whole, so knowing which single unit
wrote it is not enough to decide whether its rows are already somewhere else.

**A manifest records WHICH units its fragment covers**, and that is what makes an overlap detectable
at finalize. Two fragments are otherwise two opaque strings; with their unit sets written down, an
overlap is a set intersection the finalizer can act on. See `discover_staged`.

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


def manifest_name(unit_keys: Sequence[str] | str) -> str:
    """A batch's manifest filename — a hash of its UNIT SET, so re-running it overwrites itself.

    Source keys are URLs and paths: they carry slashes, query strings and unicode. Hashing gives a
    flat, fixed-width name that is legal on every store, and makes the overwrite-on-retry behaviour
    exact rather than dependent on how a store normalises a path.

    Sorted before hashing because a set has no order but `fetch()` does: the same units arriving in
    a different order are the same work, and must land on the same manifest rather than a second one
    that `discover_staged` would then have to reconcile.
    """
    keys = [unit_keys] if isinstance(unit_keys, str) else sorted(unit_keys)
    return f"{hashlib.sha256('\x00'.join(keys).encode()).hexdigest()[:32]}.json"


def stage_fragments(dataset_uri: str, run_id: str, unit_keys: Sequence[str] | str, fragments_json: Sequence[str]) -> str:
    """Record a batch's fragments durably. MUST be called before ANY of its units is acked.

    `unit_keys` is every unit whose rows are inside these fragments — not a label for the batch. The
    finalizer reads it to decide whether a fragment's rows are already covered elsewhere, so passing
    a subset silently reintroduces the duplication this signature exists to prevent.

    Returns the manifest key, so a caller can assert the write happened rather than assume it.
    """
    keys = [unit_keys] if isinstance(unit_keys, str) else sorted(unit_keys)
    payload = json.dumps({"unit": keys[0], "units": keys, "fragments": list(fragments_json)}).encode()
    name = manifest_name(keys)
    root = staging_root(dataset_uri, run_id)

    if _is_object_store(root):
        bucket, prefix = _split(root)
        _client().put_object(Bucket=bucket, Key=f"{prefix}/{name}", Body=payload, ContentType="application/json")
    else:
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return name


class StagingOverlapError(RuntimeError):
    """Two staged fragments each hold rows the other does not, so neither can be dropped.

    Raised rather than resolved because both alternatives are silent data corruption: committing
    both duplicates the units they share, committing one loses the units it lacks. A run that stops
    here keeps every byte it fetched — the fragments are still on the store, still named by their
    manifests — and can be finished by hand. The worker makes this unreachable (see
    `drain_chunk`: a redelivered unit is never batched with a fresh one), so reaching it means that
    isolation broke, which is worth a loud failure rather than a quiet repair.
    """


def discover_staged(dataset_uri: str, run_id: str) -> list[str]:
    """The fragments this run should commit — each of its units covered exactly ONCE.

    Not simply "everything staged". A fragment is committed whole (`LanceOperation.Append` takes
    fragments, not rows), so when a batch is partly acked and its remainder comes back as a second,
    smaller fragment, the two OVERLAP: the first already holds the rows the second re-fetched.
    Appending both writes those units twice, and nothing downstream would catch it — the lander's
    commit is a blind append, with `merge_insert` forbidden by `test_ingest_invariants.py`.

    So this resolves ownership instead of collecting. Largest unit set first, and a fragment is
    taken only if it adds units nothing already taken covers:

        F covers {u0,u1,u2,u3}   staged, then the pod died after acking u0 and u1
        G covers {u2,u3}         the remainder, redelivered and written again

    F is taken (4 units, none covered yet); G is skipped, because F already holds both of its rows.
    Four units in, four rows out. G's bytes stay on the store until `purge_staged`, uncommitted and
    unreferenced — the price of a crash, and cheaper than either wrong commit.

    Sorted by size then by unit key so the choice is deterministic: two workers finalizing the same
    staging prefix must select the same fragments, and a size tie must not resolve on dict order.

    An absent staging prefix is an empty list, not an error: a run with no units never staged
    anything, and a run whose staging was already purged has nothing left to commit. Both are
    legitimate, and raising here would turn a successful no-op run into a failure.
    """
    records: list[tuple[frozenset[str], list[str]]] = []
    for raw in sorted(_read_all(staging_root(dataset_uri, run_id))):
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            # A truncated manifest means the pod died mid-write, which means the unit was never
            # acked — so it is still on the queue and will be refetched. Skipping is correct;
            # failing the finalize over it would strand a run that the queue can still complete.
            continue
        fragments = [str(fragment) for fragment in record.get("fragments", [])]
        if not fragments:
            continue
        # `unit` is the pre-batching field name. A manifest written by an older worker names one
        # unit and covers one fragment, which is exactly a one-element set — no migration needed.
        units = record.get("units") or ([record["unit"]] if "unit" in record else [])
        records.append((frozenset(str(unit) for unit in units), fragments))

    records.sort(key=lambda item: (-len(item[0]), sorted(item[0])))

    covered: set[str] = set()
    out: list[str] = []
    seen: set[str] = set()
    for units, fragments in records:
        if units & covered:
            overlap_only = units - covered
            if overlap_only:
                raise StagingOverlapError(
                    f"run {run_id}: staged fragments overlap without containment — "
                    f"{sorted(units & covered)} are already committed by another fragment while "
                    f"{sorted(overlap_only)} exist only here, and a fragment cannot be split"
                )
            continue
        covered |= units
        for fragment in fragments:
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
