"""Lance manifest feature flags — the refusal gate for datasets this pass cannot correctly maintain.

The spec settles the rule (``lance_docs/file_format.md`` "Feature Flags"): a reader checks
``reader_feature_flags``, a writer checks ``writer_feature_flags``, and "if either sees a flag they
don't know, they should return an 'unsupported' error on any read or write operation". The
maintenance pass is a **writer** — compaction commits, GC deletes manifests — so it checks both.

**Why this exists, concretely.** Two flags were measured against real datasets on pylance 9.0.0:

* **Flag 16 (``base_paths`` — shallow clone / multi-base) is genuinely unsafe today.** A shallow
  clone opens fine and ``compact_one`` happily rewrote it: an 8-fragment clone with **no ``data/``
  directory at all** came back with its own full copy of every row. That is silent storage
  amplification which defeats the entire point of the feature, and ``cleanup_old_versions`` then ran
  on the result.
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
from typing import Protocol


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
    """The refusal reason for an already-read flag pair, or ``None`` when both are understood."""
    unknown = (reader | writer) & ~SUPPORTED
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
