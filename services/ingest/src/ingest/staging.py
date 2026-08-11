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

import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any


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


def unit_manifest_uri(dataset_uri: str, run_id: str) -> str:
    """Where a run's ENUMERATED UNIT LIST lives — deliberately OUTSIDE its fragment-staging prefix.

    `_ingest_staging/_units/<run_id>.json`, a SIBLING of `_ingest_staging/<run_id>/`, and the location
    is the whole safety argument. `discover_staged` reads everything under the run's prefix and treats
    each record as a fragment manifest, and `_read_all` is asymmetric about how deep it looks: on the
    filesystem it globs `*.json` at the top level, on an object store it LISTS THE PREFIX RECURSIVELY.
    So a unit manifest tucked anywhere beneath the run prefix would be invisible to the recovery path
    in dev and read by it in production — the worst shape a bug can have, in the one code path where a
    mistake is duplicated or lost rows.

    (It would in fact survive today: `discover_staged` skips any record with no `fragments`. Being
    outside the prefix means the recovery path's correctness does not DEPEND on that defensive branch.)
    """
    return f"{dataset_uri.rstrip('/')}/{STAGING_DIR}/_units/{run_id}.json"


def write_unit_manifest(dataset_uri: str, run_id: str, pairs: Sequence[tuple[str, str | None]]) -> str:
    """Persist the run's enumerated `(key, token)` list once, so chunk descriptors can be POINTERS.

    §2.13: `enumerate_chunks` used to return every key and token inline, which put the whole set into
    one activity result AND again into each child's input — the payload that hit grpc's 4 MiB ceiling
    at roughly 38k units, on a plane whose own docstrings advertise million-unit harvests. Written
    here, the workflow carries `(offset, count)` and history becomes O(chunks) instead of O(units).

    Object storage, not the state store: this is bulk run data, and the state store is where the
    workflow's own history lives — the thing being kept small.
    """
    payload = json.dumps({"run_id": run_id, "units": [[key, token] for key, token in pairs]}).encode()
    uri = unit_manifest_uri(dataset_uri, run_id)
    if _is_object_store(uri):
        bucket, key = _split(uri)
        _client().put_object(Bucket=bucket, Key=key, Body=payload, ContentType="application/json")
    else:
        target = Path(uri)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return uri


def read_unit_slice(dataset_uri: str, run_id: str, offset: int, count: int) -> list[tuple[str, str | None]]:
    """One chunk's `(key, token)` window, read back from the manifest.

    Raises rather than returning a short slice when the manifest is missing: an absent manifest means
    the chunk cannot know what to publish, and publishing nothing would report a chunk that quietly
    ingested zero units as successful. `drain_chunk`'s short-drain reconcile is built to notice a
    shortfall, not an empty intent.
    """
    uri = unit_manifest_uri(dataset_uri, run_id)
    if _is_object_store(uri):
        bucket, key = _split(uri)
        try:
            raw = _client().get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        except Exception as exc:
            raise UnitManifestMissing(run_id, uri) from exc
    else:
        path = Path(uri)
        if not path.exists():
            raise UnitManifestMissing(run_id, uri)
        raw = path.read_text(encoding="utf-8")
    units = json.loads(raw).get("units") or []
    window = units[offset : offset + count]
    return [(str(pair[0]), pair[1] if len(pair) > 1 and pair[1] is not None else None) for pair in window]


class UnitManifestMissing(RuntimeError):
    """The run's enumerated unit list is gone, so a chunk cannot know what to publish."""

    def __init__(self, run_id: str, uri: str) -> None:
        super().__init__(f"the unit manifest for run {run_id!r} is missing at {uri} — the run cannot be published from pointers")


class StagingOverlapError(RuntimeError):
    """Two staged fragments each hold rows the other does not, so neither can be dropped.

    Raised rather than resolved because both alternatives are silent data corruption: committing
    both duplicates the units they share, committing one loses the units it lacks. A run that stops
    here keeps every byte it fetched — the fragments are still on the store, still named by their
    manifests — and can be finished by hand.

    **This is REACHABLE.** An earlier version of this docstring claimed the worker made it
    impossible, "a redelivered unit is never batched with a fresh one". That is only half of what
    `drain_chunk` does: it separates redelivered messages from fresh ones WITHIN ONE FETCH, but it
    batches all redeliveries in that fetch TOGETHER, whatever original batch each came from. Two
    different batches' remainders therefore land in one new fragment, which partially overlaps both
    predecessors and is contained by neither — exactly this error.

    So it is a real operational state, not a broken-invariant alarm, and a run can hit it without
    anything else having gone wrong. Failing loudly is still right — the alternative is choosing
    silently between duplication and loss — but "fix the isolation" is NOT the remedy; the remedy is
    a finalizer that can resolve a partial overlap, or a batching rule that keeps redeliveries in
    their original grouping. Both are open.
    """


def discover_staged(dataset_uri: str, run_id: str) -> list[str]:
    """The fragments this run should commit — each of its units covered exactly ONCE.

    Not simply "everything staged". A fragment is committed whole (`LanceOperation.Append` takes
    fragments, not rows), so when a batch is partly acked and its remainder comes back as a second,
    smaller fragment, the two OVERLAP: the first already holds the rows the second re-fetched.
    Appending both writes those units twice, and nothing downstream would catch it — the lander's
    commit is a blind `Append`.

    That last part used to say `merge_insert` was "forbidden by test_ingest_invariants.py", which is
    simply false: I4's allowlist is `LANDER_ALLOWED = {"lander.py"}`, so the lander is precisely
    where `merge_insert` IS permitted. The real reason is structural and was measured rather than
    assumed. `MergeInsertBuilder.execute` takes a `ReaderLike` and coerces it (`lance/dataset.py`
    `_coerce_reader`); there is no `FragmentMetadata` overload on it or on `execute_uncommitted`,
    and the lander holds exactly that — JSON from `FragmentMetadata.to_json()`. The one route that
    works (commit `detached=True`, read the staged rows back, re-wrap with `blob_array`, upsert)
    requires materialising every payload in the lander and rewriting all of it, which writes the
    archive twice and pushes every byte through the single process the fan-out design exists to keep
    them out of. So a blind append is right; the reason it is right had nothing to do with I4.

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

    staged_units = frozenset().union(*(units for units, _ in records)) if records else frozenset()
    chosen = _exact_cover([units for units, _ in records], staged_units)

    if chosen is None:
        # NAME the units the search could not place. A refusal an operator cannot act on is only
        # half a refusal — "some overlap somewhere" makes them re-derive by hand what the finalizer
        # already knows. The greedy pass is the diagnostic here, not the decision: it takes what it
        # can and whatever is left over is what no selection could reach.
        covered: set[str] = set()
        for units, _ in records:
            if not units & covered:
                covered |= units
        stranded = sorted(staged_units - covered) or sorted(staged_units)
        raise StagingOverlapError(
            f"run {run_id}: no selection of the staged fragments covers every unit exactly once. "
            f"{stranded} exist only inside fragments that overlap one another, and a fragment cannot "
            f"be split — committing both would duplicate the units they share, committing neither "
            f"would lose these. Every byte is still on the store and named by its manifest."
        )

    out: list[str] = []
    seen: set[str] = set()
    for index in chosen:
        for fragment in records[index][1]:
            if fragment not in seen:
                seen.add(fragment)
                out.append(fragment)
    return out


#: Bound on the search below. A run's staging holds one manifest per BATCH, so the realistic input is
#: a handful; the cap exists so a pathological one degrades to a loud refusal rather than to a finalize
#: that never returns. Exceeding it is indistinguishable from "no cover" to the caller, which is the
#: safe direction: it refuses instead of committing a guess.
_SEARCH_NODE_LIMIT = 50_000


def _exact_cover(sets: Sequence[frozenset[str]], universe: frozenset[str]) -> list[int] | None:
    """Indices of a subset covering `universe` with NO unit counted twice, or None if none exists.

    A real search rather than a greedy pass, because greedy is not merely suboptimal here — it
    REFUSES work it could do. Taking the largest fragment first and skipping whatever overlaps it
    raised on 24% of inputs that had a perfect cover, measured over a fuzz against a brute-force
    oracle. Deferring the raise to the end of the sweep cut that but did not close it: the choice
    itself, not the moment of judging it, is what blocks the cover.

    Standard exact-cover backtracking: pick the least-covered unit, try each fragment containing it,
    recurse. Choosing the most-constrained unit first is what keeps this small — it fails a doomed
    branch immediately instead of exploring it.

    Deterministic by construction (sorted candidate order), because two workers finalizing the same
    staging prefix must select the same fragments, and S3 listing order is not a choice.
    """
    if not universe:
        return []

    budget = [_SEARCH_NODE_LIMIT]

    def search(remaining: frozenset[str], used: frozenset[int], picked: list[int]) -> list[int] | None:
        if not remaining:
            return list(picked)
        budget[0] -= 1
        if budget[0] <= 0:
            return None

        # The most constrained unit: the one the fewest unused fragments can supply. A unit no
        # fragment can supply ends the branch here rather than after exploring everything else.
        candidates = {unit: [i for i, s in enumerate(sets) if i not in used and unit in s and s <= remaining] for unit in remaining}
        unit = min(candidates, key=lambda u: (len(candidates[u]), u))
        if not candidates[unit]:
            return None

        for index in candidates[unit]:
            picked.append(index)
            found = search(remaining - sets[index], used | {index}, picked)
            if found is not None:
                return found
            picked.pop()
        return None

    return search(universe, frozenset(), [])


def purge_staged(dataset_uri: str, run_id: str) -> int:
    """Drop the run's staging after its commit lands. Returns how many manifests were removed.

    Called only AFTER the commit succeeds. Purging earlier would delete the very record a retried
    finalize needs, converting a recoverable failure into the data loss this module exists to
    prevent.
    """
    root = staging_root(dataset_uri, run_id)
    removed = _purge_unit_manifest(dataset_uri, run_id)
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


def _purge_unit_manifest(dataset_uri: str, run_id: str) -> int:
    """Remove the run's unit manifest. Counted with the fragment manifests — it is run staging too.

    It needs its own removal precisely BECAUSE it lives outside the run's staging prefix (see
    `unit_manifest_uri`): the prefix delete that clears the fragments cannot reach it, so without this
    every completed run would leave one manifest behind forever.
    """
    uri = unit_manifest_uri(dataset_uri, run_id)
    if _is_object_store(uri):
        bucket, key = _split(uri)
        with contextlib.suppress(Exception):
            _client().delete_object(Bucket=bucket, Key=key)
            return 1
        return 0
    path = Path(uri)
    if not path.exists():
        return 0
    path.unlink()
    return 1


# ── scheme split ──────────────────────────────────────────────────────────────────────


def _split(uri: str) -> tuple[str, str]:
    from storage import split_s3_uri

    bucket, prefix = split_s3_uri(uri)
    return bucket, prefix.strip("/")


def _client() -> Any:  # noqa: ANN401 — boto3 client has no public stub; matches `storage.s3_client`
    """The estate's sanctioned S3 wrapper. Never boto3 directly — `packages/storage` owns the
    endpoint/credential resolution that keeps this MinIO/RustFS/AWS-agnostic.

    Returns `Any` because that is what `s3_client` returns, and for the same stated reason. Narrowing
    it to `object` here was strictly worse than the truth: every call site then needed a suppression
    (`get_paginator` carried one) or produced a diagnostic, so the annotation bought no safety and
    cost four — and a real typo in a boto3 kwarg would have arrived indistinguishable from them.
    """
    from storage import s3_client

    return s3_client(os.getenv("RASK_S3_ENDPOINT_URL"))


def _list_object_keys(bucket: str, prefix: str) -> list[str]:
    paginator = _client().get_paginator("list_objects_v2")
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
