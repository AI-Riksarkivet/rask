"""#128d + #114 — the pre-pass that stops a reclaimer destroying a live shallow clone's data.

THE DEFECT, in both of its forms. A shallow clone is a metadata-only copy: its manifest declares the
SOURCE in ``base_paths[]`` and its ``DataFile``s resolve through it, so the source's data files are
the only copy either dataset has. Two operations in this service will happily destroy them:

  * **#128d — purge.** ``delete_location`` deletes the recorded dataset directory outright. Purging
    the source of a live clone deletes data the clone still resolves through.
  * **#114 — the sweep.** ``compact_one`` runs compact → optimize_indices → cleanup as ONE pass, and
    the pass deletes the source's data files out from under the clone. MEASURED, and the mechanism is
    not what the issue title says — compaction is not the step that does the damage:

        4 data files -> compact_files()        -> 5 files (it ADDS the merged file, deletes nothing)
                                               -> clone opens fine in a fresh process
                     -> cleanup_old_versions() -> 1 file  (the obsoleted originals are removed)
                                               -> clone fails: ArrowInvalid, in a FRESH PROCESS

    That is why the refusal sits in front of the whole pass rather than in front of compaction: a
    guard on compaction alone would be walked straight past by the step that actually deletes. And it
    must be checked in a cold process — Lance caches dataset state per process, so an in-process read
    can keep succeeding against files that are gone.

WHY THE PER-DATASET FEATURE CHECK CANNOT SEE IT. Flag 16 marks the dataset that SPANS bases — the
clone. That is the right gate for the scan (subtracting a prefix listing from a clone is nonsense),
and ``orphans.py`` already refuses on it. But the endangered dataset is the SOURCE, and the source
carries no flag and no ``base_paths`` at all; it looks completely ordinary. Measured:

    SOURCE  flags (0, 0)   base_paths []
    CLONE   flags (16, 16) base_paths ['/tmp/…/src.lance']

The evidence lives only on the referring side, so no check that opens one dataset can find it. It has
to be collected ACROSS the estate first — hence a pre-pass.

WHY ONE PRE-PASS SERVES BOTH. #128d and #114 are the same question ("does anything else resolve
through these bytes?") asked by two callers. Fixing either alone re-opens the other: a purge guard
leaves compaction free to rewrite the source, and a compaction guard leaves purge free to delete it.

AND WHY IT LIVES IN SERVICE-KIT rather than inside the maintenance service, where it began. There is
a THIRD caller: the catalog's on-demand maintenance doors run the same three verbs behind a UI button
(``catalog/services/maintenance.py``), and a guard the cron has and the button does not is not a
guard — it is a slower path to the same deleted bytes. The catalog cannot import the maintenance
service, so leaving this there meant either an unguarded door or a second copy of the compare, and a
duplicated "two spellings of one path" comparator is the failure mode :func:`normalise` documents:
one that silently never matches looks exactly like having no guard at all.

WHAT THIS DELIBERATELY DOES NOT DO. It does not try to be complete across an estate it cannot
enumerate. If the referring dataset is in a bucket the caller did not pass, its reference is invisible
here — which is why :func:`protected_roots` reports what it FAILED to read, and callers refuse rather
than proceed on a partial map. A partial answer treated as complete is how this defect got shipped in
the first place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from service_kit.lakehouse.features import manifest_base_paths
from service_kit.lakehouse.lance_session import lance_session


if TYPE_CHECKING:
    from collections.abc import Iterable

    import lance

    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

#: Cache caps for the session this module mints when a caller supplies none. The same numbers the
#: maintenance pod defaults to (`MaintenanceSettings.lance_metadata_cache_mb` / `_index_cache_mb`),
#: sized for a few-hundred-Mi pod — against Lance's own 1 GiB + 6 GiB PER OPEN, which is the #102
#: defect. A service with a different memory budget passes its own `session`.
DEFAULT_METADATA_CACHE_MB = 128
DEFAULT_INDEX_CACHE_MB = 256


class BaseRefs(BaseModel):
    """Roots that some OTHER dataset's manifest resolves through, plus what could not be read.

    ``unreadable`` is not decoration. A dataset that would not open might have been the one holding
    the reference that protects the bytes a caller is about to delete, so "we could not read it" and
    "it referenced nothing" must stay distinguishable — the same rule the orphan scan follows with
    ``checked=False``.
    """

    #: Absolute roots referenced BY another dataset, normalised (scheme dropped, trailing `/` gone).
    protected: set[str] = Field(default_factory=set)
    #: ``(dataset_uri, reason)`` for every dataset whose manifest could not be read.
    unreadable: list[tuple[str, str]] = Field(default_factory=list)

    def is_protected(self, location: str) -> str | None:
        """The referencing root when ``location`` is one, or lies UNDER one; else ``None``.

        Containment, not equality: a base path may name a dataset root whose subdirectories
        (``data/``, ``_deletions/``, ``_indices/``) hold the referenced files, so deleting anything at
        or beneath it breaks the referrer. Equality alone would pass a request to delete
        ``<root>/data`` — which destroys precisely the files a clone resolves through.
        """
        target = _normalise(location)
        for root in self.protected:
            if target == root or target.startswith(f"{root}/") or root.startswith(f"{target}/"):
                return root
        return None


def normalise(uri: str) -> str:
    """Compare paths, not spellings: drop the scheme and any trailing slash.

    A manifest states ``/bucket/ns/t.lance`` where the caller holds ``s3://bucket/ns/t.lance``. Left
    unnormalised the guard silently never matches, which is the failure mode that looks exactly like
    having no guard at all.

    The LEADING slash is stripped for the same reason and it is the half that is easy to miss:
    dropping the scheme from ``s3://bucket/x`` leaves ``bucket/x`` while the manifest for the same
    object says ``/bucket/x``, so scheme-stripping alone still fails to match. Two spellings of one
    path must compare equal, and no two different paths are conflated by this — a leading slash never
    distinguishes one object from another here.
    """
    without_scheme = uri.split("://", 1)[-1]
    return without_scheme.strip("/")


#: PUBLIC as of the F6(d) trash exclusion — the sweep must compare a trash record's `location` against
#: a discovered dataset URI, and the estate gets exactly ONE comparator for "two spellings of one path".
#: Re-implementing the compare at the call site is how a guard silently never matches, which this
#: function's own docstring names as the failure mode indistinguishable from having no guard at all.
#: The private alias stays so the in-module call sites read unchanged.
_normalise = normalise


def protected_roots(
    dataset_uris: Iterable[str],
    storage_options: StorageOptions | None = None,
    *,
    session: lance.Session | None = None,
) -> BaseRefs:
    """Collect every root referenced by any of ``dataset_uris``, so callers can refuse to touch them.

    Opens each dataset and reads its manifest's ``base_paths``. A dataset that references only itself
    contributes nothing, which is the overwhelmingly common case — the cost is one manifest read per
    dataset, no data files touched.

    A dataset that will not open is RECORDED rather than skipped: it may be the referrer whose
    reference matters, and a caller that proceeds on a partial map is doing the thing this module
    exists to prevent.

    ``storage_options`` IS USED, and the fact that it once was not is the whole reason this paragraph
    exists. The parameter was accepted and dropped, so every ``s3://`` open failed for want of
    credentials and an endpoint — the maintenance pod carries no ambient ``AWS_*``, only
    ``MAINTENANCE_S3_*`` — and ``protected`` came back EMPTY on every tick. Both guards built on this
    (the #114 sweep refusal and the #128d purge refusal) were therefore inert in production, which is
    precisely the data-loss path they were written to close: maintaining a base whose clone depends on
    it deletes files the clone resolves through, and the clone then will not open at all.

    The failure was invisible because "unreadable" is a legitimate state here — the sweep logs
    ``maintenance_base_refs_incomplete`` and proceeds — so an empty protected set read as "no clones in
    this estate" rather than "this pre-pass cannot open anything".

    The Lance open is ALWAYS SESSION-BOUND (#102), and never optionally: without a session each
    dataset mints Lance's own 1 GiB metadata + 6 GiB index cache ceilings and discards them with the
    handle, and this function opens every dataset in the estate in a loop. ``session`` lets a caller
    thread the same bounded session its other passes use — the maintenance service does (`sweep` and
    `purge` pass `shared_lance_session()`; the catalog's on-demand door has no session of its own and
    correctly takes the default minted below), so a tick's
    later opens hit the cache instead of re-minting it — and when it is omitted this mints one at
    :data:`DEFAULT_METADATA_CACHE_MB` / :data:`DEFAULT_INDEX_CACHE_MB` rather than leaving the open
    unbounded. A caller can narrow the caps; it cannot opt out of having them.

    ``lance`` is imported inside the call, not at module scope, for the reason every other lakehouse
    module in this library does it: ``service-kit``'s base install carries no pylance, and a top-level
    import would make merely importing ``service_kit.lakehouse`` require the ``lancekit`` extra.
    """
    import lance

    if session is None:
        session = lance_session(DEFAULT_METADATA_CACHE_MB << 20, DEFAULT_INDEX_CACHE_MB << 20)
    refs = BaseRefs()
    for uri in dataset_uris:
        try:
            paths = manifest_base_paths(lance.dataset(uri, storage_options=storage_options, session=session))
        except Exception as exc:
            refs.unreadable.append((uri, f"{type(exc).__name__}: {exc}"))
            continue
        for path in paths:
            root = _normalise(path)
            # A dataset naming ITSELF is not a foreign reference and must not protect itself from its
            # own maintenance — that would make every clone permanently unmaintainable.
            if root == _normalise(uri):
                continue
            refs.protected.add(root)
            log.info("maintenance_base_ref", extra={"referrer": uri, "protects": root})
    return refs
