"""Source adapters, registered — the concrete half of I1.

Every adapter here already existed. `LocalDirSource` and `S3Source` have been in
`service_kit.lakehouse.sources` all along, and `S3PrefixSource` + its `s3_input()` lineage twin have
sat in `medallion/services/s3_harvest.py` unit-tested against moto with NO ROUTE WIRED — recorded as
open work for months. They were unreachable not because they were unfinished but because reaching
them meant adding another head route, another settings block, another produce module. That is the
cost I1 removes.

Adding a source is now: one adapter (often already written), one `register()` call, one lineage
twin. Gate A9 says a diff that does more than that has re-welded something.

Importing this module is what populates the registry, so `ingest/__init__.py` imports it for effect.
That is a deliberate import-time side effect: the alternative is a hand-maintained list somewhere
else, which is the drift this design exists to prevent.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from ingest.sources import LineageInput, SourceOption, SourceSpec, register


if TYPE_CHECKING:
    from service_kit.lakehouse.sources import SourceAdapter


#: The ONE directory tree `local-dir` may read, and there is deliberately no default.
#:
#: `options.root` is caller-supplied and reaches `LocalDirSource`, which rglobs and reads every match.
#: Unconfined, that is an arbitrary-file-read primitive pointed at the ingest pod's own filesystem:
#: `{"kind":"local-dir","options":{"root":"/proc/self","pattern":"environ"}}` lands the process
#: environment — including the S3 credential — as rows in a governed table that the explorer will
#: then serve. Extensionless files sail past the payload validator, so nothing downstream catches it.
#:
#: Unset means the kind is REFUSED, not "read anything". A default of `/` or of the working directory
#: would be the same hole with an extra step, and a source that cannot be pointed anywhere is a
#: source nobody can abuse.
LOCAL_ROOT_ENV = "RASK_INGEST_LOCAL_ROOT"


def local_root() -> Path | None:
    """The configured base, resolved, or None when `local-dir` is not enabled here."""
    base = os.getenv(LOCAL_ROOT_ENV)
    return Path(base).resolve() if base else None


def confine_to_local_root(candidate: str) -> Path:
    """Resolve `candidate` and refuse it unless it sits under the configured base.

    `resolve()` before comparing, so `..` and symlinks are collapsed FIRST — comparing the raw string
    would let `/allowed/../etc` through, and a symlink inside the base would let the traversal happen
    on the filesystem's side of the check.
    """
    base = local_root()
    if base is None:
        raise ValueError(f"local-dir is not enabled here: set {LOCAL_ROOT_ENV} to the directory it may read")
    resolved = Path(candidate).resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"local-dir path {candidate!r} is outside {LOCAL_ROOT_ENV} ({base})")
    return resolved


def _local_dir(spec: SourceSpec) -> SourceAdapter:
    """A directory tree UNDER the configured root. The lane's fixture source — deterministic, no network."""
    from service_kit.lakehouse.sources import LocalDirSource

    root = str(spec.options.get("root") or "")
    if not root:
        raise ValueError("local-dir source requires options.root")
    return LocalDirSource(confine_to_local_root(root), str(spec.options.get("pattern") or "*"))


def _local_dir_lineage(spec: SourceSpec) -> LineageInput:
    return LineageInput(namespace="file", name=str(spec.options.get("root") or ""))


def _local_dir_partition(spec: SourceSpec, key: str) -> str | None:
    """The containing DIRECTORY — the local twin of the S3 folder rule.

    `LocalDirSource` rglobs, so a nested tree really does produce several directories under one root;
    partitioning on the root instead would give every unit the same value.
    """
    head, sep, _ = key.rpartition("/")
    if not sep:
        return None
    return head or None


#: The ONE dataset root `lance-append` may read, resolved exactly like :data:`LOCAL_ROOT_ENV`.
#:
#: Same reasoning, same shape: `options.uri` is caller-supplied and reaches a reader that will scan
#: whatever it is pointed at. Unset means the kind is REFUSED rather than unconfined — a source that
#: cannot be pointed anywhere is a source nobody can abuse.
LANCE_ROOT_ENV = "RASK_INGEST_LANCE_ROOT"

#: The catalog's own root. `lance-append` refuses anything under it (guard 1) even when the confinement
#: root would otherwise allow it, because a copy between governed tiers is the CASCADE's operation.
GOVERNED_ROOT_ENV = "LANCE_REST_ROOT"


def _has_scheme(uri: str) -> bool:
    """True for `s3://...`-style URIs, false for filesystem paths.

    A scheme'd URI is compared as a STRING prefix: `Path.resolve()` on `s3://bucket/x` yields
    `<cwd>/s3:/bucket/x`, which would both mangle the value and make the confinement check compare two
    fictions.
    """
    return "://" in uri


def _under(candidate: str, base: str) -> bool:
    if _has_scheme(candidate) or _has_scheme(base):
        normalized = base.rstrip("/")
        return candidate == normalized or candidate.startswith(normalized + "/")
    resolved, root = Path(candidate).resolve(), Path(base).resolve()
    return resolved == root or root in resolved.parents


def confine_to_lance_root(candidate: str) -> str:
    """Refuse `candidate` unless it sits under the configured dataset root, and never if it is governed.

    The governed check runs FIRST and independently of the confinement root: an operator who points
    `RASK_INGEST_LANCE_ROOT` at the catalog's own bucket must still not be able to re-ingest a governed
    tier through this door. Its message names the mover, because "denied" without a destination is how a
    caller ends up building the second, unlineaged copy path by hand.
    """
    governed = os.getenv(GOVERNED_ROOT_ENV)
    if governed and _under(candidate, governed):
        raise ValueError(
            f"{candidate!r} is a catalog-governed dataset; ingest does not copy between governed tiers — "
            "that is the medallion mover's job (the bronze->silver->gold cascade). To land an EXISTING "
            "table under governance instead, use POST /v1/table/{id}/register."
        )
    base = os.getenv(LANCE_ROOT_ENV)
    if not base:
        raise ValueError(f"lance-append is not enabled here: set {LANCE_ROOT_ENV} to the dataset root it may read")
    if not _under(candidate, base):
        raise ValueError(f"lance-append uri {candidate!r} is outside {LANCE_ROOT_ENV} ({base})")
    return candidate


def _lance_append(spec: SourceSpec) -> SourceAdapter:
    """One unit per FRAGMENT of an ungoverned `.lance` dataset.

    `probe()` before returning is guard 2: it opens the dataset and counts fragments, so an absent or
    unreadable source fails while the caller still holds the request rather than inside a worker that
    has already claimed a unit and then has nothing to fetch.
    """
    from service_kit.lakehouse.sources import LanceFragmentSource

    uri = str(spec.options.get("uri") or "")
    if not uri:
        raise ValueError("lance-append source requires options.uri")
    source = LanceFragmentSource(confine_to_lance_root(uri))
    source.probe()
    return source


def _lance_append_lineage(spec: SourceSpec) -> LineageInput:
    """The dataset itself is the lineage input — the fragment is an ingest unit, not a provenance node."""
    return LineageInput(namespace="lance", name=str(spec.options.get("uri") or ""))


class LanceFragmentFetcher:
    """Read ONE fragment of a Lance dataset and return its rows as an Arrow IPC stream.

    This kind's keys are `<uri>#fragment=<id>`, which is not a fetchable URI: the path names a
    DATASET (a directory), and the unit is a fragment inside it. `ingest.fetch.UriFetcher` resolves
    by scheme and has no scheme to resolve here — it fell through to the `file`/`""` branch, which
    read the path as a blob and refused it as outside RASK_INGEST_LOCAL_ROOT. Enumeration therefore
    succeeded and every fetch failed, which is why the run reported COMPLETE_WITH_ERRORS with
    `units_total: 3, units_done: 0` and "nothing to commit".

    The confinement is re-checked HERE, not trusted from accept time. A unit key crosses the queue
    and can be replayed by a later build, so the worker must not assume the root that admitted it is
    still the root configured now — the same reason `probe()` does not stand in for a fetch-time
    check. It is the identical call the accept door makes, so a key that was legal at accept and is
    illegal now fails closed rather than reading whatever it currently points at.
    """

    async def fetch(self, key: str) -> bytes:
        return await asyncio.to_thread(self._read, key)

    @staticmethod
    def _read(key: str) -> bytes:
        import lance
        import pyarrow as pa

        uri, marker, raw = key.partition("#fragment=")
        if not marker:
            raise ValueError(f"lance-append unit key is missing its fragment marker: {key!r}")
        try:
            fragment_id = int(raw)
        except ValueError:
            raise ValueError(f"lance-append unit key has a non-numeric fragment id: {key!r}") from None

        dataset = lance.dataset(confine_to_lance_root(uri))
        for fragment in dataset.get_fragments():
            if fragment.fragment_id == fragment_id:
                table = fragment.to_table()
                break
        else:
            # PERMANENT, not transient: fragment ids are stable, so an id this dataset does not have
            # will not appear on a retry. Compaction can RETIRE one, which is exactly this case.
            raise FileNotFoundError(f"fragment {fragment_id} is not in {uri!r} (retired by compaction, or never existed)")

        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        return sink.getvalue().to_pybytes()


def _lance_append_fetcher() -> LanceFragmentFetcher:
    return LanceFragmentFetcher()


def _lance_append_partition(spec: SourceSpec, key: str) -> str | None:
    """Every fragment of one dataset shares its dataset — the folder rule has no analogue here.

    `key` is `<uri>#fragment=<id>`; the part before the fragment marker is the dataset, which is the only
    grouping this kind has. Partitioning per fragment would make the column a second copy of the id.
    """
    dataset, _, _ = key.partition("#fragment=")
    return dataset or None


def _s3_prefix(spec: SourceSpec) -> SourceAdapter:
    """An S3 prefix over the estate's provider-agnostic client.

    `storage.s3_client` rather than boto3 directly — the estate's rule, and the reason a bucket can
    move between RustFS, MinIO, HCP and AWS with env vars instead of a code change.
    """
    from pyarrow import fs as pafs

    from service_kit.lakehouse.sources import S3Source

    bucket = str(spec.options.get("bucket") or "")
    if not bucket:
        raise ValueError("s3-prefix source requires options.bucket")
    endpoint = spec.options.get("endpoint")
    fs = pafs.S3FileSystem(endpoint_override=str(endpoint)) if endpoint else pafs.S3FileSystem()
    # The storage client rides along for the VERSIONED listing (ETags): pyarrow FileInfo carries
    # no ETag, and the estate rule is storage.s3_client, never raw boto3. Same endpoint resolution
    # as the filesystem, so the two views of the bucket cannot diverge.
    from storage import s3_client

    client = s3_client(str(endpoint)) if endpoint else s3_client()
    return S3Source(fs, bucket, str(spec.options.get("prefix") or ""), client=client)


def _s3_prefix_lineage(spec: SourceSpec) -> LineageInput:
    """The `s3://bucket` / prefix pair the lineage graph already MERGEs on."""
    return LineageInput(namespace=f"s3://{spec.options.get('bucket')}", name=str(spec.options.get("prefix") or "/"))


# ── IIIF: REMOVED by owner ruling (2026-08-07) ────────────────────────────────────────────────
#
# The `iiif` source kind (IIIFVolumeSource + its lineage twin + its partition rule + its
# registration) was deleted, not disabled. The ruling's reason is the design's own open questions:
# a IIIF endpoint offers NO version token (no etag, no listing), so every convergence guarantee the
# plane now makes — (key, etag) identity, replay-safe commits, re-runs that skip what landed — would
# be fiction for it, and there is no reliable way to test against a live archive endpoint. Sources
# are s3-prefix (the governed lane, with etags) and local-dir (the hermetic test seam). If IIIF
# returns, it returns as an EXPLICIT snapshot importer with its own design, not as a watched sink.
# The medallion producer's separate /ingest-iiif head is out of this ruling's scope (#34 owns it).

# ── partition keys: how each kind GROUPS its units (the bronze `partition_key` column) ────────
#
# One function per kind, registered beside the factory, because only the adapter knows what a unit
# key means. The worker resolves units by URI SCHEME and must stay that way.
#
# Read `runtime.BRONZE_SCHEMA` for why this is a COLUMN and not a fragment boundary: Lance has no
# table partitioning, and key-pure fragments are merged back together by the very first maintenance
# compaction (measured, 5 fragments → 1).


# ── external blob bases: WHERE this kind's bytes already live (open_data_spec.md §4.1) ────────
#
# One function per kind, registered beside the factory, for the same reason the partition rules are:
# only the adapter knows what a unit key means, so only the adapter can say what contains it.
#
# A base makes bronze store the URI instead of the bytes. Measured on a 4 MB / 20-row corpus
# (`scripts/measure_external_blob_carry_forward.py`): external bronze is 3,232 B — 0.1% of the corpus
# — against 4,002,901 B for the managed form, and the descriptor still resolves 20/20 after being
# carried into a second dataset, which is what lets silver and gold stop copying too.
#
# The base is the CONTAINER of every unit key, not the run's prefix: keys are full URIs, and Lance
# stores `blob_uri` RELATIVE to the base it matched.


def _s3_prefix_external_base(spec: SourceSpec) -> str | None:
    """The BUCKET, not `options.prefix`.

    Every key this kind produces is `s3://<bucket>/<object>`, so the bucket is the one root
    guaranteed to contain all of them — including a run with no prefix at all, where a
    prefix-derived base would be the bucket anyway. Using the prefix would also mean two runs over
    two prefixes of one bucket registering two bases for the same store.
    """
    bucket = str(spec.options.get("bucket") or "")
    return f"s3://{bucket}" if bucket else None


def _local_dir_external_base(spec: SourceSpec) -> str | None:
    """The configured root, resolved and confined exactly as the adapter itself resolves it.

    `confine_to_local_root` rather than the raw option: the base is written into the dataset manifest
    and is what Lance will accept blob URIs against, so a base that escaped the configured root would
    widen what this dataset may point at — the confinement has to hold at BOTH doors, not just the
    one that reads files.
    """
    root = str(spec.options.get("root") or "")
    if not root:
        return None
    return str(confine_to_local_root(root))


# `lance-append` registers NO base, and that is a real answer rather than an omission: its fetcher
# SYNTHESISES each unit's bytes as Arrow IPC from dataset fragments, so the payload exists at no URI
# and there is nothing for an external descriptor to point at. It must own its bytes.


def _s3_prefix_partition(spec: SourceSpec, key: str) -> str | None:
    """The containing FOLDER of the object — the owner's "partition on folder level".

    Derived from the key rather than from `options.prefix`, because the prefix is the run's ROOT and
    every object under it would then share one value; the folder is what actually varies. `s3://b/a/
    b/c.tif` -> `s3://b/a/b`. An object at the bucket root has no folder, which is a null rather
    than an invented one.
    """
    head, sep, _ = key.rpartition("/")
    if not sep:
        return None
    # A bare `s3://bucket` head means the object sat at the root — no folder to name.
    return head or None


def lance_append_unusable() -> str | None:
    """Why `lance-append` cannot run here, or None when it can.

    It reads datasets from under `RASK_INGEST_LANCE_ROOT` and that root defaults to EMPTY, so on a
    stock deployment the kind was advertised by the registry and refused every run — naming an
    environment variable to whoever was filling in the form. The reason now travels with the kind
    instead, and the form renders it disabled (the estate's "show disabled, never hide" ruling).

    NOT fixed by pointing the root at the lakehouse bucket: `chart/templates/fleet.yaml` warns that
    doing so "gets a second, unlineaged copy path" into governed data. It wants its own ungoverned
    exports area, which is a deployment decision, not a default.
    """
    return None if os.getenv(LANCE_ROOT_ENV, "").strip() else f"{LANCE_ROOT_ENV} is not set — this deployment has no ungoverned Lance root to read from"


def register_builtin_sources() -> None:
    """Idempotent: safe to call from the app factory and from a test.

    Each kind declares the `options` it takes right here, beside the factory that reads them — the
    two drift the moment they are apart. `describe_sources()` serves these, so a UI renders the
    fields a kind actually has and nothing in the frontend restates them. That is what keeps adding
    a source a backend-only diff (gate A9): the compute zone's form gained S3-prefix and local-dir
    without a line of Svelte, because it asks.
    """
    from ingest.sources import registered_kinds

    known = set(registered_kinds())

    if "local-dir" not in known:
        register(
            "local-dir",
            build=_local_dir,
            lineage_input=_local_dir_lineage,
            # The containing directory — the local twin of the S3 folder rule.
            partition_of=_local_dir_partition,
            external_base_of=_local_dir_external_base,
            label="Local directory",
            description="A directory tree on the worker. Deterministic and offline — the lane's fixture source.",
            options=[
                SourceOption(name="root", label="Directory", required=True, placeholder="/data/pages", help="Absolute path, readable by the ingest pod."),
                SourceOption(name="pattern", label="Filename pattern", placeholder="*", help="Glob applied within the directory. Defaults to every file."),
            ],
        )

    if "s3-prefix" not in known:
        register(
            "s3-prefix",
            build=_s3_prefix,
            lineage_input=_s3_prefix_lineage,
            partition_of=_s3_prefix_partition,
            external_base_of=_s3_prefix_external_base,
            label="S3 prefix",
            description="Every object under a bucket prefix, through the estate's provider-agnostic client — RustFS, MinIO, HCP or AWS.",
            options=[
                SourceOption(name="bucket", label="Bucket", required=True, placeholder="lance-catalog"),
                SourceOption(name="prefix", label="Prefix", placeholder="volumes/A0068688/", help="Leave empty to take the whole bucket."),
                SourceOption(
                    name="endpoint",
                    label="Endpoint URL",
                    placeholder="(the configured default)",
                    help="Override only when the bucket is not on the estate's own endpoint.",
                ),
            ],
        )

    if "lance-append" not in known:
        register(
            "lance-append",
            build=_lance_append,
            lineage_input=_lance_append_lineage,
            partition_of=_lance_append_partition,
            fetcher=_lance_append_fetcher,
            label="Lance dataset",
            description="Every fragment of an ungoverned .lance dataset, each landed as one bronze row carrying its rows as Arrow IPC.",
            unusable=lance_append_unusable,
            options=[
                SourceOption(
                    name="uri",
                    label="Dataset URI",
                    required=True,
                    placeholder="/data/exports/run-42.lance",
                    help="An UNGOVERNED dataset under the configured root. A catalog-governed table is refused — that copy is the cascade's job.",
                ),
            ],
        )


register_builtin_sources()
