"""Lance manifest feature flags — the refusal gate for datasets this pass cannot correctly maintain.

The spec settles the rule (``lance_docs/file_format.md`` "Feature Flags"): a reader checks
``reader_feature_flags``, a writer checks ``writer_feature_flags``, and "if either sees a flag they
don't know, they should return an 'unsupported' error on any read or write operation". The
maintenance pass is a **writer** — compaction commits, GC deletes manifests — so it checks both.

**Why this exists, concretely.** Two flags were measured against real datasets on pylance 9.0.0:

* **Flag 16 (``base_paths`` — shallow clone / multi-base) is unsafe to COMPACT when this dataset's
  files live under the base.** A shallow clone opens fine and ``compact_one`` happily rewrote it: an
  8-fragment clone with **no ``data/`` directory at all** came back with its own full copy of every
  row. That is silent storage amplification which defeats the entire point of the feature, and
  ``cleanup_old_versions`` then ran on the result. **The flag alone does not say which kind of base a
  dataset has**, and refusing on the flag alone cost the estate its bronze compaction for 785 sweep
  ticks — so compaction gates on OBSERVED evidence about the bases instead
  (:func:`describe_compaction_unsupported_flags`), while every other consumer keeps the flags-only
  refusal.
* **Flag 64 (data overlays) is only ACCIDENTALLY safe.** pylance 9.0.0 refuses the open itself
  (``ValueError: Not supported: This dataset cannot be read by this version of Lance… Flags: 64``),
  which surfaced as a generic ``open:`` error that the sweep's lineage selection treats as transient
  non-dataset noise and ``summarize`` buried in ``errors``. A pylance that gains overlay support
  flips that to a silent correctness bug with zero code change on our side.

pylance exposes neither flag field. ``LanceDataset._ds.serialized_manifest()`` returns the Manifest
protobuf (pylance's own pickle path uses it), and the two flags are top-level varints at fields 9 and
10. Verified empirically on pylance 9.0.0 against the documented values: plain -> ``(0, 0)``, a
``delete()`` -> ``(1, 1)``, ``enable_stable_row_ids`` -> ``(2, 2)``, both -> ``(3, 3)``, a
``shallow_clone`` of the deletion dataset -> ``(17, 17)`` = 1|16, ``add_bases`` -> ``(16, 16)``, a
committed ``LanceOperation.DataOverlay`` -> ``(64, 64)``.

**The failure direction is deliberate and it has a cost.** :data:`SUPPORTED` is a WHITELIST, so a
pylance upgrade that introduces a legitimate new flag stops this pass maintaining every dataset that
sets it — versions grow forever while the sweep still reports success. That is why a refusal must be
its own counted, logged and metric'd line in the sweep summary rather than folded into ``errors`` or
``skipped``. There is deliberately **no env escape hatch**: "maintain it anyway" is exactly the
answer that rewrote a shallow clone.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


log = logging.getLogger(__name__)


class _SerializedManifestHandle(Protocol):
    def serialized_manifest(self) -> bytes: ...


class ManifestCarrier(Protocol):
    """The one seam this module needs from a ``lance.LanceDataset`` — kept structural so this stays in
    dependency-light service-kit (both the maintenance sweep AND the catalog's on-demand doors gate on
    it, and neither can import the other)."""

    @property
    def _ds(self) -> _SerializedManifestHandle: ...


class _DataFileHandle(Protocol):
    @property
    def base_id(self) -> int | None: ...


class _FragmentHandle(Protocol):
    def data_files(self) -> Sequence[_DataFileHandle]: ...


class FragmentCarrier(ManifestCarrier, Protocol):
    """The manifest seam PLUS the fragment walk :func:`gather_compaction_bases` needs.

    Separate from :class:`ManifestCarrier` because every other consumer here reads only the manifest,
    and widening the one protocol would make them all claim a dependency they do not have.
    """

    def get_fragments(self) -> Sequence[_FragmentHandle]: ...


#: "Is a Lance dataset rooted at this path" — the object store's answer, injected. Bound in the fleet
#: to :func:`service_kit.lakehouse.objectfs.is_lance_dataset_root`.
type DatasetRootProbe = Callable[[str], bool]


#: The documented flags (``lance_docs/file_format.md``, "Current Feature Flags"). Named rather than
#: inlined so a refusal message and this module's whitelist cannot drift apart.
FLAG_DELETION_FILES = 1
FLAG_STABLE_ROW_IDS = 2
FLAG_USE_V2_FORMAT_DEPRECATED = 4
FLAG_TABLE_CONFIG = 8
FLAG_BASE_PATHS = 16
#: Not in the vendored spec table (which stops at 16 and calls 32+ "unknown"), but real and measured:
#: a committed ``LanceOperation.DataOverlay`` sets it, and an overlay writes new cell values to
#: ``data/overlay-<uuid>.lance`` referenced from ``DataFragment.overlays`` rather than ``data_files()``.
FLAG_DATA_OVERLAYS = 64

#: Flags this pass can compact / GC / scan without being wrong.
#:
#: WIDENING THIS IS A DELIBERATE EDIT, NEVER A DEFAULT. An unknown flag means the on-disk layout
#: carries something our rewrite does not account for; adding a bit here is a claim that compaction,
#: version GC and the orphan pass have each been checked against that layout on a real dataset.
SUPPORTED = FLAG_DELETION_FILES | FLAG_STABLE_ROW_IDS | FLAG_USE_V2_FORMAT_DEPRECATED | FLAG_TABLE_CONFIG

#: The same whitelist plus ``base_paths``, for the operations that are ROOT-SCOPED and therefore safe on
#: a shallow clone: ``cleanup_old_versions``, ``enable_auto_cleanup`` and ``optimize_indices``.
#:
#: A SECOND mask rather than a widening of ``SUPPORTED``, because that constant is also consumed by
#: ``maintenance/services/orphans.py`` and ``catalog/services/maintenance.py`` — and for the orphan scan
#: the refusal is genuinely required: a shallow clone's files resolve through ``base_paths`` to another
#: dataset's root, so "list the prefix, subtract what is referenced" would report live data as garbage.
#: Widening one constant would have silently changed all three call sites.
#:
#: MEASURED on pylance 9.0.0 rather than argued, because the blanket refusal's own docstring asserted it
#: was necessary. The discriminating experiment: a clone whose overwrite orphaned dead fragments on BOTH
#: sides, then ONE ``cleanup_old_versions(delete_unverified=True)`` call — it removed the 2 clone-owned
#: files and left all 4 base-owned ones, base data files 4 -> 4, and the base still read in a fresh
#: process. The same held for inherited ``_deletions/`` and ``_indices/``, across six cleanup shapes and
#: ten repeat cycles, with zero base files deleted in every run. ``optimize_indices`` likewise writes a
#: delta into the clone's own root and leaves the base byte-identical.
#:
#: ``compact_files`` is EXCLUDED and stays on ``SUPPORTED``: it silently materialises the shared data into
#: the clone's own root — a pristine clone went 1,072 -> 108,199 bytes against a 119,693-byte base —
#: which defeats the point of cloning. It never damages the base, so this is a cost refusal, not a safety
#: one, but it is still the wrong thing to do behind an operator's back.
#:
#: That measurement is about a CLONE, and a dataset whose only base is an external blob prefix compacts
#: safely — so a flags-only refusal is over-broad, and the cost was the cascade's own tiers
#: (``medallion.services.compute`` and ``ingest.lander`` both pass ``initial_bases``) accumulating
#: fragments forever. :func:`describe_compaction_unsupported_flags` now makes that distinction from
#: EVIDENCE rather than from the flag, and :mod:`maintenance.services.optimize` asks it — see that
#: function for the three readings and the measurements behind each.
SUPPORTED_FOR_GC = SUPPORTED | FLAG_BASE_PATHS

_FLAG_NAMES = {
    FLAG_DELETION_FILES: "deletion files",
    FLAG_STABLE_ROW_IDS: "stable row ids",
    FLAG_USE_V2_FORMAT_DEPRECATED: "v2 format (deprecated)",
    FLAG_TABLE_CONFIG: "table config",
    FLAG_BASE_PATHS: "base_paths (shallow clone / multi-base)",
    FLAG_DATA_OVERLAYS: "data overlays",
}

#: Manifest protobuf field numbers. Pinned by ``test_maintenance_features.py`` against the documented
#: flag values, so a manifest reshuffle in a future pylance fails THERE — loudly — instead of here,
#: silently, by reading some other field as the flags.
_MANIFEST_READER_FLAGS_FIELD = 9
_MANIFEST_WRITER_FLAGS_FIELD = 10

#: ``base_paths`` — repeated ``BasePath``, and ``BasePath.path`` inside it. MEASURED, not read: the
#: format doc gives the BasePath message (``id=1, name=2, is_dataset_root=3, path=4``) but never the
#: manifest field number that carries it. Taken off a real shallow clone's manifest bytes —
#: ``92 01`` decodes as varint 146 → field 18, wire 2 — with the source path following at field 4.
_MANIFEST_BASE_PATHS_FIELD = 18
_BASE_PATH_PATH_FIELD = 4
#: ``BasePath.is_dataset_root`` — the bit that separates a shallow clone from an external blob base.
#: MEASURED on pylance 10.0.0 off both manifests: a clone's submessage is
#: ``18 01 22 1a <source path>`` (field 3 = 1, then the path), an ``initial_bases`` blob prefix's is
#: ``08 01 12 03 <name> 22 19 <path>`` — id and name, and field 3 ABSENT, which proto3 means as false.
_BASE_PATH_IS_DATASET_ROOT_FIELD = 3

#: pylance's own refusal names the offending bits: "… Please upgrade Lance to read this dataset.
#: Flags: 64, /home/runner/work/lance/…". Match on Lance's WORDING, never on "the open failed" —
#: a missing directory must keep reading as an ordinary ``open:`` error, not as a feature refusal.
_OPEN_REFUSAL_MARKERS = ("cannot be read by this version of Lance", "Flags:")
_OPEN_REFUSAL_FLAGS = re.compile(r"Flags:\s*(\d+)")


def manifest_feature_flags(ds: ManifestCarrier) -> tuple[int, int]:
    """``(reader_feature_flags, writer_feature_flags)`` from the dataset's own manifest.

    Both default to 0 — proto3 omits a zero varint, so an ABSENT field is genuinely "no flags set"
    rather than "unknown". Every other field is skipped by wire type, so an unrelated manifest change
    cannot shift the read.
    """
    blob: bytes = ds._ds.serialized_manifest()  # noqa: SLF001 — pylance's own __reduce__ path uses it
    reader = writer = 0
    i, n = 0, len(blob)
    while i < n:
        key, i = _varint(blob, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(blob, i)
            if field == _MANIFEST_READER_FLAGS_FIELD:
                reader = value
            elif field == _MANIFEST_WRITER_FLAGS_FIELD:
                writer = value
        elif wire == 2:
            length, i = _varint(blob, i)
            i += length
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            # A wire type we cannot skip. STOP rather than misread the remaining bytes as flags —
            # a garbage reader value would refuse a healthy dataset forever.
            break
    return reader, writer


class BasePathRef(BaseModel):
    """One ``BasePath`` entry out of a manifest: WHERE the base is, and WHAT the manifest SAYS it is.

    Flag 16 says only that a dataset spans bases; it cannot tell a shallow CLONE (whose base is
    another dataset's root and holds the only copy of the clone's rows) from an ingest bronze table
    (whose base is a plain object-store prefix where external blobs already live, its own data files
    under its own root). Those two need opposite answers from the compaction gate, so this model
    carries ``is_dataset_root`` alongside the path.

    IT IS A SELF-REPORT, NOT AN OBSERVATION, and on its own it is not enough to gate on. Measured on
    pylance 10.0.0: ``shallow_clone`` is the only writer that ever sets the bit, so an ``add_bases``
    pointed straight at a live Lance root reads False here. :func:`gather_compaction_bases` therefore
    pairs it with the object store's own answer and with ``DataFile.base_id`` — see
    :class:`CompactionBases`.
    """

    path: str
    #: False for a plain prefix. Absent in the manifest means false — proto3 omits a zero.
    is_dataset_root: bool = False


def manifest_base_path_refs(ds: ManifestCarrier) -> list[BasePathRef]:
    """Every ``BasePath`` this dataset's manifest declares, path AND kind.

    The parse is :func:`manifest_base_paths`' — see its docstring for why the referring side is the
    only side that carries this evidence — reading one field more.
    """
    blob: bytes = ds._ds.serialized_manifest()  # noqa: SLF001 — same access `manifest_feature_flags` documents
    refs: list[BasePathRef] = []
    i, n = 0, len(blob)
    while i < n:
        key, i = _varint(blob, i)
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, i = _varint(blob, i)
            # A submessage carrying no ``path`` is dropped rather than recorded as an empty root:
            # "" would compare equal to nothing useful downstream and reads as a base that exists.
            if field == _MANIFEST_BASE_PATHS_FIELD and (ref := _base_path_ref(blob[i : i + length])).path:
                refs.append(ref)
            i += length
        elif wire == 0:
            _, i = _varint(blob, i)
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            # Same rule as the flag walker: STOP rather than misread. A garbage path here would
            # either refuse a healthy dataset forever or, worse, fail to name a real one.
            break
    return refs


def manifest_base_paths(ds: ManifestCarrier) -> list[str]:
    """Every ``BasePath.path`` this dataset's manifest declares — the roots its files may live under.

    WHY THIS IS NEEDED AT ALL, and why the feature flag is not enough. Flag 16 tells you that THIS
    dataset spans bases, and that is what stops a scan subtracting a prefix listing from a clone. It
    says nothing about the opposite direction, which is the destructive one: the SOURCE of a shallow
    clone carries NO flag and looks completely ordinary, while its data files are the only copy the
    clone's manifest resolves through. Delete or compact the source and the clone breaks — a defect
    reproduced as 8 data files becoming 1, after which the clone fails to open in a fresh process.

    So the paths have to be read from the referring side and collected across the estate, which is
    what `service_kit.lakehouse.base_refs` does with this. A per-dataset check cannot see it, because
    the endangered dataset is not the one carrying the evidence.

    Returns absolute paths as the manifest states them (the format calls them "interpretable by the
    object store"). An empty list is the overwhelmingly common case and means exactly what it says:
    this dataset resolves everything under its own root.

    The PATHS view of :func:`manifest_base_path_refs`, and derived from it rather than parsed a second
    time — one walker, so a manifest reshuffle cannot fix one reader and leave the other misreading.
    """
    return [ref.path for ref in manifest_base_path_refs(ds)]


def _base_path_ref(message: bytes) -> BasePathRef:
    """One ``BasePath`` submessage. Skips by wire type, so added fields cannot shift it."""
    path, is_dataset_root = "", False
    i, n = 0, len(message)
    while i < n:
        key, i = _varint(message, i)
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, i = _varint(message, i)
            if field == _BASE_PATH_PATH_FIELD:
                path = message[i : i + length].decode("utf-8", errors="replace")
            i += length
        elif wire == 0:
            value, i = _varint(message, i)
            if field == _BASE_PATH_IS_DATASET_ROOT_FIELD:
                is_dataset_root = bool(value)
        elif wire == 5:
            i += 4
        elif wire == 1:
            i += 8
        else:
            break
    return BasePathRef(path=path, is_dataset_root=is_dataset_root)


def _varint(blob: bytes, i: int) -> tuple[int, int]:
    """One protobuf base-128 varint starting at ``i``; returns ``(value, next_index)``."""
    value = shift = 0
    while i < len(blob):
        byte = blob[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return value, i


def unsupported_features(ds: ManifestCarrier) -> str | None:
    """Why this dataset must be REFUSED, or ``None`` when every flag it sets is understood.

    The reason NAMES the offending bits, because "unsupported" alone is not actionable: an operator
    needs to know it is looking at a shallow clone rather than at a broken dataset.
    """
    reader, writer = manifest_feature_flags(ds)
    return describe_unsupported_flags(reader, writer)


def describe_unsupported_flags(reader: int, writer: int) -> str | None:
    """The refusal reason for an already-read flag pair, or ``None`` when both are understood.

    THE FLAGS-ONLY gate, and the strictest of the three: it refuses ``base_paths`` in every form. That
    is what the ORPHAN SCAN needs — a shallow clone's files resolve through another dataset's root, so
    "list the prefix, subtract what is referenced" would report live data as garbage — and it is what
    the catalog's on-demand doors gate on (one check in front of all three verbs, so it inherits the
    strictest of them).

    **Compaction does NOT ask this one.** It asks :func:`describe_compaction_unsupported_flags`, which
    weighs evidence about the bases themselves; version reclamation and index maintenance ask
    :func:`describe_gc_unsupported_flags`, which tolerates ``base_paths`` outright.
    """
    unknown = (reader | writer) & ~SUPPORTED
    if not unknown:
        return None
    return f"unsupported manifest feature flags: {_named(unknown)} (reader={reader}, writer={writer})"


class BaseEvidence(BaseModel):
    """One declared base, as the manifest states it AND as the object store answers for it."""

    path: str
    #: The manifest's self-report (``BasePath.is_dataset_root``). True only for a ``shallow_clone``.
    declares_dataset_root: bool = False
    #: The listing's answer: ``<path>/_versions/`` is a directory. ``None`` = the store could not be
    #: asked (permission, endpoint, a path shape the filesystem cannot resolve) — treated as a refusal.
    probed_dataset_root: bool | None = None


class CompactionBases(BaseModel):
    """Everything the compaction gate could OBSERVE about a flag-16 dataset's bases.

    Three readings, because no one of them is sufficient — each was measured on pylance 10.0.0 against
    a fixture built by hand, and each catches a shape the others miss:

    ``bases[].declares_dataset_root``
        The manifest's own ``BasePath.is_dataset_root``. Set by ``shallow_clone`` and by nothing else,
        so it is a true positive and a useless negative: an ``add_bases`` pointed straight at a live
        Lance root reports False.
    ``bases[].probed_dataset_root``
        The OBJECT STORE's answer — does ``<base>/_versions/`` exist. Ground truth about the base,
        independent of what the referring manifest chose to say. ``None`` means the store could not
        answer, which is a refusal, never a permission.
    ``data_resolves_through_a_base``
        Whether any ``DataFile`` of any fragment carries a ``base_id`` — i.e. whether OUR data is
        living over there. This is the one that decides the hazard, and it is the only signal that
        catches ``write_dataset(..., target_bases=[...])``: measured, that lands our own data files
        under a base which is neither declared nor probed a dataset root, and compacting pulled them
        home (local root 3,540 -> 5,991 bytes, the base's three files left behind as garbage).
        ``None`` means the fragments could not be read — again a refusal.

    A model rather than three arguments because they are ONE answer with one failure direction, and a
    caller that gathered two of three must not be able to spell that as a permit.
    """

    #: Every ``BasePath`` the manifest declares. EMPTY while flag 16 is set is itself a refusal: the
    #: dataset says it spans bases and we could not read which.
    bases: list[BaseEvidence] = Field(default_factory=list)
    #: None = unread. See the class docstring — this is the deciding signal.
    data_resolves_through_a_base: bool | None = None


def gather_compaction_bases(ds: FragmentCarrier, probe: DatasetRootProbe) -> CompactionBases:
    """The evidence :func:`describe_compaction_unsupported_flags` weighs, gathered off one open dataset.

    ``probe`` answers "is a Lance dataset rooted at this path" — in the fleet,
    :func:`service_kit.lakehouse.objectfs.is_lance_dataset_root` bound to the caller's storage options.
    It is injected rather than imported so this module stays free of the object-store layer (the
    catalog and the sweep both gate here and neither can import the other's plumbing), and so a test
    can drive the ambiguous branch without an unreachable endpoint.

    NOTHING HERE RAISES. Every read that fails is recorded as the unknown it is — ``None`` — because a
    gatherer that threw would surface as a per-dataset ``error`` and be reported as a failure of the
    dataset rather than as a refusal by the gate. The cost of a wrong refusal is wasted space; the
    cost of a wrong permit is a clone's whole reason to exist, so unknown resolves to refusal.

    Cheap by construction: one ``get_file_info`` per declared base (datasets declare one, or none),
    and the fragment walk reads metadata the open manifest already holds.
    """
    bases: list[BaseEvidence] = []
    try:
        refs = manifest_base_path_refs(ds)
    except Exception:
        # A manifest we cannot re-read is one whose bases we do not know. An empty list is the refusal
        # (see `_base_paths_compaction_refusal`), which is what "we could not tell" must mean here.
        log.warning("compaction_base_paths_unreadable", exc_info=True)
        refs = []
    for ref in refs:
        try:
            probed: bool | None = probe(ref.path)
        except Exception:
            log.warning("compaction_base_probe_failed", extra={"base": ref.path}, exc_info=True)
            probed = None
        bases.append(BaseEvidence(path=ref.path, declares_dataset_root=ref.is_dataset_root, probed_dataset_root=probed))
    try:
        resolves: bool | None = any(file.base_id is not None for fragment in ds.get_fragments() for file in fragment.data_files())
    except Exception:
        log.warning("compaction_fragment_read_failed", exc_info=True)
        resolves = None
    return CompactionBases(bases=bases, data_resolves_through_a_base=resolves)


def describe_compaction_unsupported_flags(reader: int, writer: int, bases: CompactionBases | None) -> str | None:
    """The refusal reason for COMPACTION, or ``None`` when the rewrite is safe and honest here.

    :func:`describe_unsupported_flags` refuses flag 16 outright, and that is too wide. TWO shapes set
    the flag and only one of them is the one that was measured:

    * a **shallow clone** — its base is another DATASET's root and its ``DataFile``s resolve through
      it, so compaction materialises the shared data into the clone's own root (a pristine clone went
      1,072 -> 108,199 bytes against a 119,693-byte base). Still refused, and this is a COST refusal:
      it never damages the base, it just defeats the point of cloning behind an operator's back.
    * an **external blob base** — ``ingest/lander.py::create_empty`` and
      ``medallion/services/compute.py`` register one plain object-store prefix through
      ``initial_bases``, naming where this table's payload bytes already live. Its own data files sit
      under its own root like any other table's, so compaction merges its own fragments and copies
      nothing. MEASURED on pylance 10.0.0: 4 fragments -> 1, 9,445 -> 14,366 bytes locally, the base
      directory byte-identical, 20/20 external payloads still resolving afterwards.

    Folded together, the refusal covered every ingest bronze table and every medallion tier — the rows
    with the most fragments in the estate — which then accumulated fragments forever while the sweep
    reported a successful pass over them (``fragments_removed_total=0`` across 785 ticks).

    **THE FLAG CANNOT MAKE THIS DISTINCTION AND NEITHER CAN ANY SINGLE BIT.** An earlier attempt read
    ``BasePath.is_dataset_root`` alone; measured on pylance 10.0.0, ``shallow_clone`` is the only
    writer that ever sets it, so ``add_bases`` pointed at a live Lance root reports False and would
    have been waved through. What decides the hazard is whether OUR FILES LIVE OVER THERE, and
    :class:`CompactionBases` carries three readings of that question — the manifest's self-report, the
    object store's own listing, and ``DataFile.base_id``. Compaction is permitted only when all three
    say no, and the last one is not redundant: ``target_bases=[...]`` puts our data under a base that
    is a dataset root by neither reading (measured: compacting it pulled 3,540 -> 5,991 bytes home and
    orphaned the base's three files).

    **FAIL CLOSED, deliberately asymmetric.** ``bases`` is ``None``, or declares no base, or carries a
    base whose probe could not be answered, or could not read the fragments -> REFUSE. The cost of a
    wrong refusal is wasted space and a loud counted line in the sweep summary; the cost of a wrong
    permit is destroying the reason a clone exists. Every OTHER unknown flag still refuses exactly as
    before, and the base-path allowance never waves one of those through.
    """
    unknown = (reader | writer) & ~SUPPORTED
    clause: str | None = None
    if unknown & FLAG_BASE_PATHS:
        clause = _base_paths_compaction_refusal(bases)
        if clause is None:
            unknown &= ~FLAG_BASE_PATHS
    if not unknown:
        return None
    reason = f"unsupported manifest feature flags: {_named(unknown)} (reader={reader}, writer={writer})"
    return f"{reason} — {clause}" if clause is not None else reason


def _base_paths_compaction_refusal(bases: CompactionBases | None) -> str | None:
    """Why flag 16 still refuses THIS dataset, or ``None`` when its bases are provably foreign to it.

    The order is evidence-first: the reading that decides the hazard is checked before the two that
    only describe the base, so the reason an operator reads names the thing that would actually have
    been rewritten.
    """
    if bases is None:
        return "no base evidence was gathered for this dataset, so its bases could be another dataset's root — refusing rather than guessing"
    if bases.data_resolves_through_a_base is None:
        return "this dataset's fragments could not be read, so whether its data files live under a base is unknown — refusing rather than guessing"
    if bases.data_resolves_through_a_base:
        return "this dataset's data files resolve through a base, so compacting would materialise another root's bytes into this one"
    if not bases.bases:
        return "the manifest sets base_paths but declares no BasePath this reader could parse — refusing rather than guessing"
    for base in bases.bases:
        if base.declares_dataset_root:
            return f"the base at {base.path} is declared a dataset root (a shallow clone) — compacting would materialise the shared data into this root"
        if base.probed_dataset_root is None:
            return f"the base at {base.path} could not be read in object storage, so whether it is a dataset root is unknown — refusing rather than guessing"
        if base.probed_dataset_root:
            return f"a Lance dataset is rooted at the base {base.path} — compacting would materialise its data into this root"
    return None


def describe_gc_unsupported_flags(reader: int, writer: int) -> str | None:
    """The refusal reason for the ROOT-SCOPED operations, or ``None`` when they are safe to run.

    Version reclamation and index maintenance touch only the dataset's own root, so a shallow clone's
    ``base_paths`` does not endanger them — see :data:`SUPPORTED_FOR_GC` for the measurement. Everything
    else refuses exactly as before.
    """
    unknown = (reader | writer) & ~SUPPORTED_FOR_GC
    if not unknown:
        return None
    return f"unsupported manifest feature flags: {_named(unknown)} (reader={reader}, writer={writer})"


def unsupported_features_from_open_error(exc: BaseException) -> str | None:
    """The same refusal, recovered from an open that pylance itself rejected — or ``None`` when the
    failure is an ordinary unopenable path.

    pylance refuses a manifest whose flags IT does not know before we can read them ourselves, so
    for those datasets this is the only place the refusal can be recognised. Without it the pass
    reports a Rust source path from a GitHub runner as a generic ``open:`` error, which the sweep's
    lineage selection classifies as transient non-dataset noise and drops entirely.
    """
    text = str(exc)
    if not any(marker in text for marker in _OPEN_REFUSAL_MARKERS):
        return None
    match = _OPEN_REFUSAL_FLAGS.search(text)
    if match is None:
        return "pylance refused the open: unsupported manifest feature flags (the error named none)"
    flags = int(match.group(1))
    # Describe the bits pylance named. They are unsupported by DEFINITION here (pylance would not
    # have refused otherwise), so mask against SUPPORTED only to drop the ones we do understand.
    return f"pylance refused the open: unsupported manifest feature flags: {_named(flags & ~SUPPORTED or flags)} (flags={flags})"


def _named(mask: int) -> str:
    return ", ".join(f"{bit} ({_FLAG_NAMES.get(bit, 'unknown')})" for bit in _bits(mask))


def _bits(mask: int) -> list[int]:
    """The set bits of ``mask``, low to high, as their integer values."""
    return [1 << i for i in range(64) if mask >> i & 1]
