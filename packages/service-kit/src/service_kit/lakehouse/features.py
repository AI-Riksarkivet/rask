"""Lance manifest feature flags — the refusal gate for datasets this pass cannot correctly maintain.

The spec settles the rule (``lance_docs/file_format.md`` "Feature Flags"): a reader checks
``reader_feature_flags``, a writer checks ``writer_feature_flags``, and "if either sees a flag they
don't know, they should return an 'unsupported' error on any read or write operation". The
maintenance pass is a **writer** — compaction commits, GC deletes manifests — so it checks both.

**Why this exists, concretely.** Two flags were measured against real datasets on pylance 9.0.0:

* **Flag 16 (``base_paths`` — shallow clone / multi-base) is unsafe to COMPACT on a clone.** A shallow
  clone opens fine and ``compact_one`` happily rewrote it: an 8-fragment clone with **no ``data/``
  directory at all** came back with its own full copy of every row. That is silent storage
  amplification which defeats the entire point of the feature, and ``cleanup_old_versions`` then ran
  on the result. **The flag alone does not say which kind of base a dataset has**, and that
  conflation cost the estate its bronze compaction — see :func:`describe_compaction_unsupported_flags`.
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

import re
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel


if TYPE_CHECKING:
    from collections.abc import Sequence


class _SerializedManifestHandle(Protocol):
    def serialized_manifest(self) -> bytes: ...


class ManifestCarrier(Protocol):
    """The one seam this module needs from a ``lance.LanceDataset`` — kept structural so this stays in
    dependency-light service-kit (both the maintenance sweep AND the catalog's on-demand doors gate on
    it, and neither can import the other)."""

    @property
    def _ds(self) -> _SerializedManifestHandle: ...


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
#: That measurement is about a CLONE, and a dataset whose only base is an external blob prefix would
#: compact safely — so refusing it is over-broad. :func:`describe_compaction_unsupported_flags` was
#: written to make that distinction from the manifest's ``BasePath`` entries, but IT IS NOT WIRED:
#: :mod:`maintenance.services.optimize` still asks :func:`describe_unsupported_flags`, the strict
#: flags-only gate, so compaction is refused on EVERY dataset carrying ``base_paths`` — including the
#: cascade's own tiers (``medallion.services.compute`` and ``ingest.lander`` both pass
#: ``initial_bases``). It cannot simply be swapped in: measured on pylance 10.0.0, ``initial_bases``
#: and ``add_bases`` both yield ``BasePathRef(is_dataset_root=False)``, so the helper cannot tell an
#: external blob base from a shallow clone and would permit the clone case it exists to refuse.
#: Distinguishing them needs something the manifest does not currently expose — an owner decision,
#: not a code cleanup. Until then this refusal is deliberate over-refusal, and the cost is that such
#: a dataset accumulates fragments forever.
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
    """One ``BasePath`` entry out of a manifest: WHERE the base is, and WHAT it is.

    ``is_dataset_root`` is the whole reason this model exists rather than a bare path. Flag 16 says
    only that a dataset spans bases; it cannot tell a shallow CLONE (whose base is another dataset's
    root and holds the only copy of the clone's rows) from an ingest bronze table (whose base is a
    plain object-store prefix where external blobs already live, its own data files under its own
    root). Those two need opposite answers from the compaction gate, and one bit cannot carry both.
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
    the catalog's on-demand doors gate on. **Compaction asks this one too**, so it refuses every
    ``base_paths`` dataset; :func:`describe_compaction_unsupported_flags` was written to relax that to
    external blob bases only and has no production caller (see the ``SUPPORTED_FOR_GC`` note above for
    why it cannot be wired yet). Version reclamation and index maintenance ask
    :func:`describe_gc_unsupported_flags`, which tolerates ``base_paths`` outright.
    """
    unknown = (reader | writer) & ~SUPPORTED
    if not unknown:
        return None
    return f"unsupported manifest feature flags: {_named(unknown)} (reader={reader}, writer={writer})"


def describe_compaction_unsupported_flags(reader: int, writer: int, bases: Sequence[BasePathRef]) -> str | None:
    """The refusal reason for COMPACTION, or ``None`` when the rewrite is safe and honest here.

    :func:`describe_unsupported_flags` refuses flag 16 outright, and that was too wide by exactly one
    case. TWO shapes set the flag and only one of them is the one that was measured:

    * a **shallow clone** — its base is another DATASET's root and its ``DataFile``s resolve through
      it, so compaction materialises the shared data into the clone's own root (a pristine clone went
      1,072 -> 108,199 bytes against a 119,693-byte base). Still refused, and this is a COST refusal:
      it never damages the base, it just defeats the point of cloning behind an operator's back.
    * an **external blob base** — ``ingest/lander.py::create_empty`` registers one plain object-store
      prefix through ``initial_bases``, naming where this table's payload bytes already live. Its own
      data files sit under its own root like any other table's, so compaction merges its own fragments
      and copies nothing. MEASURED on pylance 10.0.0: 4 fragments -> 1, every row still readable, the
      base directory untouched.

    Folded together, the refusal covered every ingest bronze table — the tier with the widest rows and
    the most fragments in the estate — which then accumulated fragments forever while the sweep
    reported a successful pass over it.

    ``bases`` is :func:`manifest_base_path_refs` of the same dataset. NO BASES, NO ALLOWANCE: a flag-16
    manifest we could not read a ``BasePath`` out of is one whose kind we do not know, and the
    whitelist's direction is that "we could not tell" reads as the refusal. One dataset-root base is
    enough to make the whole dataset a clone for this purpose, and every OTHER unknown flag still
    refuses exactly as before.
    """
    unknown = (reader | writer) & ~SUPPORTED
    if unknown & FLAG_BASE_PATHS and bases and not any(base.is_dataset_root for base in bases):
        unknown &= ~FLAG_BASE_PATHS
    if not unknown:
        return None
    return f"unsupported manifest feature flags: {_named(unknown)} (reader={reader}, writer={writer})"


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
