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
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ingest.config import settings


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
    # No legacy `unit` key: that was the pre-batching spelling, and writing it on every new manifest
    # kept it alive forever. `discover_staged` still READS it, for manifests already on a store.
    payload = json.dumps({"units": keys, "fragments": list(fragments_json)}).encode()
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
            with contextlib.closing(_client().get_object(Bucket=bucket, Key=key)["Body"]) as body:
                raw = body.read().decode()
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


class StagingCoverAbandoned(RuntimeError):
    """The finalizer STOPPED SEARCHING for a selection. It did not find the fragments in conflict.

    Deliberately not a `StagingOverlapError`, and deliberately not a subclass of one, because the two
    demand opposite responses from an operator: an overlap needs fragments reconciled by hand, an
    abandoned search needs the run re-finalized. They used to be indistinguishable — `_exact_cover`
    returned a bare `None` for both "no cover exists" and "I ran out of budget", and this module
    rendered both as an overlap. That mattered far more than it sounds: before the solver stopped
    rebuilding its candidate index over the run's whole unit universe at every step, running out of
    budget was the ORDINARY outcome for a large CLEAN run, so the message an operator got told them
    their fragments conflicted when nothing was wrong with them at all.

    Reaching this now means the staged family really is ambiguous on a scale `drain_chunk`'s
    singleton-redelivery rule exists to prevent. As with an overlap, every byte is still on the store
    and still named by its manifest — nothing has been committed and nothing has been lost.
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
    # NOT `sorted(_read_all(...))`. Determinism comes from the `records.sort` below, and that key is
    # total here: a manifest is named by a hash of its unit set, so no two records can share one.
    # Sorting the raw manifest BODIES first bought nothing and held every manifest's JSON in memory
    # at once — hundreds of MB on a run this module's docstrings advertise, for an ordering thrown
    # away three lines later.
    for raw in _read_all(staging_root(dataset_uri, run_id)):
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
    verdict = _exact_cover([units for units, _ in records], staged_units)

    if verdict.exhausted:
        # Reported SEPARATELY from an overlap, and worded so the difference is unmissable. Telling an
        # operator "your fragments overlap" when the finalizer merely stopped looking sends them to
        # reconcile fragments that are fine, and hides the one action that would work — re-running
        # the finalize.
        raise StagingCoverAbandoned(
            f"run {run_id}: the finalizer ABANDONED its search for a fragment selection over "
            f"{len(records)} staged manifests covering {len(staged_units)} units. This is not a "
            f"verdict that they overlap — the search was cut short, and a selection may well exist. "
            f"Every byte is still on the store and named by its manifest."
        )

    if verdict.chosen is None:
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
    for index in verdict.chosen:
        for fragment in records[index][1]:
            if fragment not in seen:
                seen.add(fragment)
                out.append(fragment)
    return out


class CoverResult(BaseModel):
    """A cover search's verdict, keeping "no selection exists" apart from "I stopped looking".

    The two used to arrive as the same bare `None`, and `discover_staged` rendered both as an
    overlap. They are opposite diagnoses, so they get separate fields and separate exceptions —
    see `StagingCoverAbandoned`.
    """

    chosen: list[int] | None = None
    exhausted: bool = False


#: Budget for the backtracking search over the AMBIGUOUS residue, counted in primitive steps rather
#: than in search nodes. A node bound was the wrong unit: it capped how many nodes were visited while
#: the cost OF a node grew with the run's unit count, so the cap did not bound anything an operator
#: cares about and a clean 25,600-unit run still spent 95s inside the search. Steps bound wall time
#: directly, and stay deterministic — two workers finalizing the same prefix must reach the same
#: verdict, so a real clock could not be used here.
#:
#: Reaching it now requires fragments that overlap in a way unit propagation cannot decide, and it is
#: reported as ABANDONED, never as an overlap.
_SEARCH_WORK_LIMIT = 20_000_000


def _exact_cover(sets: Sequence[frozenset[str]], universe: frozenset[str]) -> CoverResult:
    """Indices of a subset covering `universe` with NO unit counted twice.

    A real search rather than a greedy pass, because greedy is not merely suboptimal here — it
    REFUSES work it could do. Taking the largest fragment first and skipping whatever overlaps it
    raised on 24% of inputs that had a perfect cover, measured over a fuzz against a brute-force
    oracle. Deferring the raise to the end of the sweep cut that but did not close it: the choice
    itself, not the moment of judging it, is what blocks the cover.

    Three stages, cheapest first, because the input this actually gets is almost never a search
    problem. An OWNER INDEX (unit -> the fragments holding it) is built once, instead of being
    rebuilt over the whole remaining universe at every step. UNIT PROPAGATION then forces every
    fragment that is the sole holder of some unit — which resolves an uncrashed run outright, since
    there each unit has exactly one holder — and eliminates whatever those forced picks collide with.
    Only what survives reaches the SEARCH, and that search walks an explicit stack.

    None of that is an optimisation. This runs inside `finalize_run`, after every unit has been acked
    off a WORK_QUEUE stream, so it is the last point at which a run can still fail recoverably — and
    the version before this one made per-step work proportional to the RUN's unit count and stack
    depth equal to its fragment count. Measured: a clean 25,600-unit run took 95s, and ~950 clean
    fragments overflowed the interpreter stack. Both are ordinary runs at the scale this module's own
    docstrings advertise, and both surfaced to the operator as "your fragments overlap".

    Deterministic by construction, because two workers finalizing the same staging prefix must select
    the same fragments and S3 listing order is not a choice. Propagation cannot make the verdict
    order-dependent — a fragment that is some unit's sole holder belongs to EVERY exact cover, so
    forcing it can neither create a cover nor destroy one — and the indices come back sorted.
    """
    if not universe:
        return CoverResult(chosen=[])

    owners: dict[str, list[int]] = {}
    for index, unit_set in enumerate(sets):
        for unit in unit_set:
            owners.setdefault(unit, []).append(index)

    if any(unit not in owners for unit in universe):
        return CoverResult()

    settled = _propagate(sets, owners)
    if settled is None:
        return CoverResult()

    undecided = universe - settled.covered
    if not undecided:
        return CoverResult(chosen=sorted(settled.forced))

    found = _search_core(sets, settled.core, frozenset(undecided))
    if found.chosen is None:
        return found
    return CoverResult(chosen=sorted(settled.forced + found.chosen))


class _Propagation(BaseModel):
    """What forcing the sole-held fragments settled, and what it left for the search."""

    forced: list[int]
    covered: set[str]
    core: list[int]


def _propagate(sets: Sequence[frozenset[str]], owners: dict[str, list[int]]) -> _Propagation | None:
    """Force every fragment that is some unit's ONLY holder, transitively. None if that proves no cover.

    Sound because a unit held by exactly one live fragment leaves no choice: every exact cover of the
    universe must contain that fragment, so taking it can neither create a cover nor destroy one.
    That is also why the result cannot depend on the order units come off the worklist, which the
    determinism contract in `discover_staged` requires.

    This is the whole reason the finalizer is now linear on the input it actually gets. An uncrashed
    run's batches share no units, so every unit has exactly one holder, every fragment is forced, and
    nothing reaches the search at all.
    """
    # A manifest with fragments but an EMPTY unit set is never live, so it is never forced and never
    # committed. That matches what the previous search did — it was a candidate for no unit, so
    # backtracking never reached it — and here it has to be explicit: propagation would otherwise be
    # free to force a fragment covering nothing, committing rows no unit in the run claims.
    live = {index for index, unit_set in enumerate(sets) if unit_set}
    holders_left = {unit: len(indices) for unit, indices in owners.items()}
    covered: set[str] = set()
    forced: list[int] = []
    pending = [unit for unit, count in holders_left.items() if count == 1]

    def drop(index: int) -> None:
        """Rule a fragment out. Units it held may become sole-held, so they go back on the worklist."""
        live.discard(index)
        for unit in sets[index]:
            if unit in covered:
                continue
            holders_left[unit] -= 1
            if holders_left[unit] <= 1:
                pending.append(unit)

    while pending:
        unit = pending.pop()
        if unit in covered or holders_left[unit] > 1:
            continue
        if holders_left[unit] == 0:
            # Nothing live holds it and nothing forced covers it, so no selection can. A genuine
            # overlap, decided in linear time rather than by exhausting a search.
            return None
        index = next(i for i in owners[unit] if i in live)
        forced.append(index)
        live.discard(index)
        covered |= sets[index]
        # Anything else still holding one of those units would now commit it a second time.
        for held in sets[index]:
            for other in owners[held]:
                if other in live:
                    drop(other)

    return _Propagation(forced=forced, covered=covered, core=sorted(live))


def _search_core(sets: Sequence[frozenset[str]], core: Sequence[int], remaining: frozenset[str]) -> CoverResult:
    """Backtracking over the fragments propagation could not decide, on an EXPLICIT stack.

    Explicit rather than recursive even though the core is normally empty and never large: the old
    solver recursed once per fragment it PICKED, and in the ordinary disjoint case exactly one is
    picked per level, so stack depth equalled the run's manifest count. Around 950 manifests — a
    ~970k-unit run at the default `fragment_rows=1024` — overflowed CPython's stack from inside the
    finalize activity. A frame cost that scales with the run is the one thing this cannot afford,
    whatever the search's own complexity is.

    Scans are restricted to the core, never the run's universe, so the per-step cost tracks how
    AMBIGUOUS the staging is rather than how big the run is.
    """
    core_holders: dict[str, list[int]] = {}
    for index in core:
        for unit in sets[index]:
            core_holders.setdefault(unit, []).append(index)
    core_occurrences = sum(len(sets[index]) for index in core)

    def candidates(rest: frozenset[str]) -> list[int]:
        """Fragments that could cover the most-constrained unit left, in ascending index order.

        Most-constrained first is what keeps the search small: a unit nothing can supply ends the
        branch here instead of after every other branch has been explored.
        """
        usable = {index for index in core if sets[index] <= rest}
        best: list[int] | None = None
        for unit in sorted(rest):
            holders = [index for index in core_holders.get(unit, ()) if index in usable]
            if not holders:
                return []
            if best is None or len(holders) < len(best):
                best = holders
        return best or []

    budget = _SEARCH_WORK_LIMIT
    frames: list[tuple[frozenset[str], Iterator[int]]] = []
    picked: list[int] = []

    def descend(rest: frozenset[str]) -> bool:
        """Open a frame for `rest`, charging what its candidate scan costs. False when spent."""
        nonlocal budget
        budget -= core_occurrences + len(rest)
        if budget <= 0:
            return False
        frames.append((rest, iter(candidates(rest))))
        return True

    if not descend(remaining):
        return CoverResult(exhausted=True)

    while frames:
        rest, untried = frames[-1]
        if not rest:
            return CoverResult(chosen=list(picked))
        index = next(untried, None)
        if index is None:
            frames.pop()
            if picked:
                picked.pop()
            continue
        picked.append(index)
        if not descend(rest - sets[index]):
            return CoverResult(exhausted=True)

    return CoverResult()


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


@cache
def _client_for(endpoint: str | None) -> Any:  # noqa: ANN401 — boto3 client has no public stub; matches `storage.s3_client`
    """The one S3 client per endpoint, memoized. `storage.s3_client` opens a connection pool, and a
    fresh one per staging call throws that pool away every time — the endpoint is process-stable and
    the wrapped client is thread-safe, so one instance serves the whole run.

    KEYED ON THE ENDPOINT, not a bare no-arg cache: the estate swaps MinIO/RustFS/AWS by env alone,
    so a changed `RASK_S3_ENDPOINT_URL` must build a distinct client rather than return the stale one.
    Within one process the value does not change, so this is a singleton in practice.
    """
    from storage import s3_client

    return s3_client(endpoint)


def _client() -> Any:  # noqa: ANN401 — boto3 client has no public stub; matches `storage.s3_client`
    """The estate's sanctioned S3 wrapper. Never boto3 directly — `packages/storage` owns the
    endpoint/credential resolution that keeps this MinIO/RustFS/AWS-agnostic.

    Returns `Any` because that is what `s3_client` returns, and for the same stated reason. Narrowing
    it to `object` here was strictly worse than the truth: every call site then needed a suppression
    (`get_paginator` carried one) or produced a diagnostic, so the annotation bought no safety and
    cost four — and a real typo in a boto3 kwarg would have arrived indistinguishable from them.
    """
    return _client_for(settings().s3_endpoint_url)


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
            with contextlib.closing(client.get_object(Bucket=bucket, Key=key)["Body"]) as body:
                yield body.read().decode()
        return

    directory = Path(root)
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.json")):
        yield path.read_text(encoding="utf-8")
