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
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import unquote_to_bytes

import pyarrow.fs as pafs
from pydantic import BaseModel, Field

from service_kit.lakehouse.features import BasePathRef
from service_kit.lakehouse.lance_session import lance_session
from service_kit.lakehouse.objectfs import fs_and_base


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    import lance

    from service_kit.lakehouse.base_registry import BaseRecord
    from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

#: Cache caps for the session this module mints when a caller supplies none. The same numbers the
#: maintenance pod defaults to (`MaintenanceSettings.lance_metadata_cache_mb` / `_index_cache_mb`),
#: sized for a few-hundred-Mi pod — against Lance's own 1 GiB + 6 GiB PER OPEN, which is the #102
#: defect. A service with a different memory budget passes its own `session`.
DEFAULT_METADATA_CACHE_MB = 128
DEFAULT_INDEX_CACHE_MB = 256


class BaseRelation(StrEnum):
    """Where an unsanctioned base sits against the root of the dataset that declares it."""

    #: Outside the referrer's root and not containing it — another table, another prefix.
    FOREIGN = "foreign"
    #: CONTAINS the referrer's root — a parent prefix or a bucket root, which would otherwise protect
    #: every table beneath it at once.
    ANCESTOR = "ancestor"


class BaseFinding(BaseModel):
    """One declared base nothing sanctions: not the referrer's own root, not configured, not recorded."""

    #: The dataset whose manifest declares the base, as it was discovered.
    referrer: str
    #: The declared base, as the path it decodes to (:func:`decoded_path`).
    base: str
    relation: BaseRelation


class BaseRefs(BaseModel):
    """Roots that some OTHER dataset's manifest resolves through, plus what could not be read.

    ``unreadable`` is not decoration. A dataset that would not open might have been the one holding
    the reference that protects the bytes a caller is about to delete, so "we could not read it" and
    "it referenced nothing" must stay distinguishable — the same rule the orphan scan follows with
    ``checked=False``.
    """

    #: Absolute roots referenced BY another dataset, as the paths they decode to (:func:`decoded_path`),
    #: like every root here. No verb may touch one: every referrer that names it is unconditional.
    protected: set[str] = Field(default_factory=set)
    #: Roots every referrer reaches through a PINNED recorded relation ([[LH-279]]), mapped to the source
    #: tags that pin them — the tags come from the catalog's record, never the manifest (a clone's
    #: ``BasePath.name`` is ``None``, lh279 p4). Deletion is refused as for ``protected``; compaction and
    #: version reclamation are permitted while every tag exists on the root, because reclamation keeps a
    #: tagged version's files and the relation reads through exactly that version (lh279 p4: the clone
    #: read cold after source ``compact_files`` + ``cleanup_old_versions(error_if_tagged_old_versions=False)``).
    pinned: dict[str, set[str]] = Field(default_factory=dict)
    #: ``(dataset_uri, reason)`` for every dataset whose manifest could not be read.
    unreadable: list[tuple[str, str]] = Field(default_factory=list)
    #: Declared bases that protect nothing because nothing sanctions them ([[LH-279]]). A manifest's
    #: base list is writable by any holder of the table's write vend (``UpdateBases``), so a base that
    #: is not the referrer's own, not operator-configured and not in the catalog's record is a claim
    #: nobody with authority made — honouring it froze the table it named (lh279 p1/p3).
    findings: list[BaseFinding] = Field(default_factory=list)

    def is_protected(self, location: str) -> str | None:
        """The referencing root when ``location`` is one, or lies UNDER one; else ``None`` — the DELETE answer.

        Containment, not equality: a base path may name a dataset root whose subdirectories
        (``data/``, ``_deletions/``, ``_indices/``) hold the referenced files, so deleting anything at
        or beneath it breaks the referrer. Equality alone would pass a request to delete
        ``<root>/data`` — which destroys precisely the files a clone resolves through.

        Pinned roots count: no pin survives its source's directory being deleted. An unconditional root
        is answered before a pinned one, so a caller that then asks :meth:`pin_tags` about the answer
        learns the strongest protection over ``location``.

        ``location`` may be spelled either way the estate holds it: the catalog's percent-encoded
        location or the decoded directory discovery lists (lh279 r6 spellings). It is compared as the
        path it decodes to, the form every root here is held in.
        """
        target = decoded_path(location)
        for root in (*sorted(self.protected), *sorted(self.pinned)):
            if target == root or target.startswith(f"{root}/") or root.startswith(f"{target}/"):
                return root
        return None

    def pin_tags(self, root: str) -> frozenset[str] | None:
        """The tags whose presence on ``root`` permits compacting it, or ``None`` when nothing does.

        ``None`` when an unconditional referrer names ``root`` too, or when no pinned one does. The
        compaction gate asks this about the root :meth:`is_protected` answered, and permits the pass
        only while every tag is on the dataset it opened.
        """
        if root in self.protected or root not in self.pinned:
            return None
        return frozenset(self.pinned[root])


#: The directory a named branch's own files live under — `file_format.md:2763`, "Named branches store
#: their version-specific files under `tree/{branch_name}/`".
_BRANCH_SEGMENT = "/tree/"


def containment_of(location: str, root: str) -> str:
    """How ``location`` sits against the protected ``root``: ``is`` | ``branch`` | ``under`` | ``ancestor``.

    IT DECIDES GC BEHAVIOUR, and one of the four is PERMITTED. `maintenance.services.optimize` reclaims
    a location whose relation to the protected root is ``branch`` and refuses the other three, because a
    branch is Lance's own business inside one root while an external clone is not — `file_format.md:3187`
    disclaims exactly that case ("Source dataset remains immutable and can be garbage collected
    independently"). Observed on the deployed estate 2026-09-18 when the permit landed: `relation='branch'`
    refusals went 105 -> 0 and 61 branch datasets reclaimed 97 versions in one tick, with the parent's
    `data/` unchanged and still time-travelling.

    It began as diagnosis, because the four are not the same situation and one sentence described all of
    them:
    measured on the live estate 2026-09-17, 129 of 246 refused datasets were ``branch`` and were told
    "another dataset resolves its files through <root>", which is the PARENT's situation stated about
    the child.

    A branch is the REFERRER, not part of the referent. `file_format.md:2744` — "Each branch dataset is
    technically a shallow clone of the source dataset" — and the layout at `:2746-2761` gives
    ``tree/{branch}/`` its own ``_versions/``/``_transactions/``/``_deletions/``/``_indices/`` and no
    ``data/``, so it resolves its data through the parent. That is what makes the parent protected, and
    it is why naming the branch as a referenced root inverts the fact an operator needs.

    ``location`` is a spelling and is compared as the path it decodes to (:func:`decoded_path`); ``root``
    is one :meth:`BaseRefs.is_protected` answered, already in that form, and is not decoded again.
    """
    here, there = decoded_path(location), _normalise(root)
    if here == there:
        return "is"
    if here.startswith(f"{there}/"):
        return "branch" if _BRANCH_SEGMENT in here[len(there) :] else "under"
    return "ancestor"


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
#: a discovered dataset URI. It drops the scheme and the edge slashes and decodes nothing, so two
#: spellings of one path that differ in percent-encoding compare equal only as :func:`decoded_path`,
#: the form every base-reference verdict here compares and keys in.
#: Re-implementing the compare at the call site is how a guard silently never matches, which this
#: function's own docstring names as the failure mode indistinguishable from having no guard at all.
#: The private alias stays so the in-module call sites read unchanged.
_normalise = normalise

#: Path segments that name no object of their own. `..` is the one that matters: pylance 12.0.0
#: resolves ``<attacker>/../victim`` on a local root, and the attacker then reads the victim's rows
#: (lh279 s4) — a base textually UNDER the attacker's root that is not. `.` and the empty segment are
#: refused with it because a comparison on the spelling cannot say where they resolve either.
_NON_LOCATION_SEGMENTS = frozenset({"", ".", ".."})

#: Characters that can divide a path into segments: `/` in every store, and `\\` in a ``file://`` URL or
#: on a Windows filesystem. A decode that adds one to a segment can give the opened path a boundary the
#: spelling does not show. pylance 12.0.0 on Linux opens a decoded `\\` as a name character
#: (lh279 r5 escape: ``x/..%5C..%5Cvictim`` reads nothing), so refusing it is the conservative half.
_SEPARATORS = ("/", "\\")


def _decoded_once(segment: str) -> str | None:
    """``segment`` percent-decoded once, as pylance decodes it; ``None`` when the bytes are not UTF-8.

    pylance 12.0.0 refuses such a path outright ("contained non-unicode characters", over moto and a local
    root, lh279 r5 not_utf8), so it names no location.
    """
    try:
        return unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError:
        return None


def decoded_segments(uri: str) -> list[str] | None:
    """The segments of ``uri``'s path as pylance opens them, or ``None`` when it names no location (:func:`names_a_location`)."""
    path = _normalise(uri)
    if not path:
        return None
    segments: list[str] = []
    for spelled in path.split("/"):
        segment = _decoded_once(spelled)
        if segment is None or segment in _NON_LOCATION_SEGMENTS:
            return None
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in segment):
            return None
        if any(segment.count(separator) > spelled.count(separator) for separator in _SEPARATORS):
            return None
        segments.append(segment)
    return segments


def decoded_path(uri: str) -> str:
    """The path ``uri`` names, as pylance opens it: its :func:`decoded_segments` joined by ``/``.

    THE ONE FORM a base-reference verdict compares and keys a location in. The estate holds one table in
    two spellings: the catalog percent-encodes the name into the location it records and a branch's
    manifest declares (``…$r%C3%A4ksm%C3%B6rg%C3%A5s``), while the store keeps the decoded directory and
    discovery lists that (``…$räksmörgås``; lh279 r6 spellings). Compared as spelled they never match: a
    branch's base on its parent is judged a finding, and nothing protects the parent (lh279 rvw finding1).

    A spelling that names no location keeps its normalised spelling (:func:`normalise`): no decode says
    where it is, and every predicate here already answers it as naming nothing.
    """
    segments = decoded_segments(uri)
    return _normalise(uri) if segments is None else "/".join(segments)


def names_a_location(uri: str) -> bool:
    """Whether ``uri`` spells one place, so that comparing the path it decodes to says where it is.

    Every containment answer below compares decoded paths (:func:`decoded_path`), and that is an answer
    about the place only when the decode is the one the store performs. pylance 12.0.0 decodes a location
    the way a URL is decoded: it percent-decodes each segment ONCE, resolves the ``.`` and ``..`` segments
    that leaves, and drops a tab, line feed or carriage return (measured over moto and a local root, lh279
    r3 a_escape). So each ``/``-separated segment is judged as it reads after one decode, and the spelling
    names no location when any decoded segment is empty, ``.`` or ``..``, holds an ASCII control
    character, is not UTF-8, or holds a ``/`` or ``\\`` the decode added. That refuses
    ``<own>/%2e%2e/<victim>``, ``<own>/.%2e/<victim>``, ``<own>/.\\t./<victim>`` and a configured
    ``<models>/%2e%2e/<victim>``, each spelled inside a sanctioned prefix while opening another table.

    Any other ``%`` escape names the place it decodes to, and must stay a location: the catalog
    percent-encodes a table's name into its location (``räksmörgås`` -> ``r%C3%A4ksm%C3%B6rg%C3%A5s``,
    ``growth%`` -> ``growth%25``, a space -> ``%20``), and a branch of that table declares its root spelled
    so (lh279 r3b names, r5 manifest). A second level of encoding is a name: ``%252e%252e`` decodes once to the literal
    segment ``%2e%2e``, which pylance opens as a directory of that name (lh279 r5 escape).
    """
    return decoded_segments(uri) is not None


def location_within(outer: str, inner: str) -> bool:
    """Whether ``inner`` is the location ``outer`` names or lies beneath it, segment by decoded segment.

    On a path BOUNDARY: ``bucket/t`` does not contain ``bucket/t-evil``. Each side is read as
    :func:`decoded_segments` reads it, so the catalog's ``…$r%C3%A4ksm%C3%B6rg%C3%A5s`` and the
    ``…$räksmörgås`` discovery lists are one location. Refuses to vouch for either side when it does not
    name a location (:func:`names_a_location`), because ``bucket/t/../victim`` starts with ``bucket/t/``
    and is not under it.
    """
    there, here = decoded_segments(outer), decoded_segments(inner)
    return there is not None and here is not None and here[: len(there)] == there


def store_of(uri: str) -> str:
    """The store a spelled location resolves in: its URI scheme, case-folded, and ``file`` for a bare path.

    :func:`normalise` drops the scheme so two spellings of one path compare equal, which is right only
    inside ONE store. A base resolves through the scheme its own spelling names, not the table's —
    measured on pylance 12.0.0 over moto (lh279fx probe_scheme_resolve): on an S3 table a ``file://`` base
    and a bare ``/path`` base both read the process's local filesystem, and ``S3://`` reads the same store
    as ``s3://``. So a comparison that decides whether a base is a table's own, or an operator's, asks
    this as well as the path.
    """
    scheme, separator, _rest = uri.partition("://")
    return scheme.lower() if separator else "file"


def location_in_store(outer: str, inner: str) -> bool:
    """:func:`location_within`, and only when both spellings name the same store (:func:`store_of`)."""
    return store_of(outer) == store_of(inner) and location_within(outer, inner)


def protected_roots(
    dataset_uris: Iterable[str],
    storage_options: StorageOptions | None = None,
    *,
    configured: Sequence[str],
    record_of: Callable[[str], BaseRecord | None],
    session: lance.Session | None = None,
) -> BaseRefs:
    """Collect every root a sanctioned base of any of ``dataset_uris`` names, so callers can refuse to touch them.

    Opens each dataset and reads its manifest's ``base_paths``, then judges every edge with
    :func:`classify_base_refs` against ``configured`` (the operator's external blob bases) and
    ``record_of`` (the catalog's base record for an owning root, which must RAISE when unreadable). A
    base nothing sanctions is a finding and protects nothing ([[LH-279]]): a manifest's base list is a
    writer's claim, and honouring a planted one froze every table beneath it. A dataset that references
    only itself contributes nothing, which is the overwhelmingly common case — the cost is one manifest
    read per dataset, no data files touched, and a record read only for a root with a base outside its
    own and outside the configured ones.

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

    from service_kit.lakehouse.features import manifest_base_path_refs

    if session is None:
        session = lance_session(DEFAULT_METADATA_CACHE_MB << 20, DEFAULT_INDEX_CACHE_MB << 20)
    declared: dict[str, list[BasePathRef]] = {}
    unreadable: list[tuple[str, str]] = []
    for uri in dataset_uris:
        try:
            declared[uri] = manifest_base_path_refs(lance.dataset(uri, storage_options=storage_options, session=session))
        except Exception as exc:
            unreadable.append((uri, f"{type(exc).__name__}: {exc}"))
    refs = classify_base_refs(declared, configured=configured, record_of=record_of)
    refs.unreadable = unreadable
    return refs


def classify_base_refs(
    declared: Mapping[str, Sequence[BasePathRef]],
    *,
    configured: Sequence[str],
    record_of: Callable[[str], BaseRecord | None],
) -> BaseRefs:
    """Which declared bases may protect, and which are findings — over manifests already read ([[LH-279]]).

    ``declared`` maps EVERY discovered dataset to the bases its manifest declares, the ones declaring
    none included: a referrer's OWNING ROOT is the outermost discovered dataset that contains it, so a
    branch at ``<root>/tree/<b>`` is owned by ``<root>`` because ``<root>`` was discovered, not because
    its path carries a ``/tree/`` substring anyone can spell.

    Each non-self edge is judged by :func:`service_kit.lakehouse.base_registry.judge_bases` against the
    owning root — the one predicate the vend, read and register doors share:

    - inside the owning root, in its store (a branch naming its parent, a same-root clone): protects;
    - inside an operator-configured external blob base: protects, and is never a finding — it is a
      pointer base the operator approved, and no vend grants it;
    - named by the owning root's base record: protects — PINNED (:attr:`BaseRefs.pinned`) when the entry
      is a ``derived_from`` relation carrying its source tag, unconditionally otherwise;
    - anything else: a :class:`BaseFinding`, and NEVER protected. A planted base naming another table
      or a bucket root froze everything it named (lh279 p1/p3); a finding reports it and freezes nothing.

    ``record_of`` is asked at most once per owning root, and only for a root with a base the first two
    rules did not settle. It must RAISE when a record cannot be read: "no record" would turn a
    recorded clone source into a finding and hand its bytes to compaction.
    """
    from service_kit.lakehouse import base_registry  # base_registry imports this module's `normalise`

    # Each root is keyed by the path it decodes to and keeps its spelling: the judge compares a base's STORE
    # with the owner's (:func:`store_of`), which only the spelling carries, and the record is read by it.
    roots = sorted({decoded_path(uri): uri for uri in declared if names_a_location(uri)}.items(), key=lambda root: len(root[0]))
    records: dict[str, BaseRecord | None] = {}
    refs = BaseRefs()
    for uri, bases in declared.items():
        own = decoded_path(uri)
        owner, spelled = next(((root, spelling) for root, spelling in roots if location_within(spelling, uri)), (own, uri))
        foreign = [ref for ref in bases if decoded_path(ref.path) != own]
        if not foreign:
            continue

        def _record(owner: str = owner, spelled: str = spelled) -> BaseRecord | None:
            if owner not in records:
                records[owner] = record_of(spelled)
            return records[owner]

        for judgement in base_registry.judge_bases(spelled, foreign, configured=configured, load_record=_record):
            base = decoded_path(judgement.ref.path)
            if judgement.standing is base_registry.BaseStanding.UNRECORDED:
                relation = BaseRelation.ANCESTOR if location_within(judgement.ref.path, spelled) else BaseRelation.FOREIGN
                refs.findings.append(BaseFinding(referrer=uri, base=base, relation=relation))
                continue
            entry = judgement.entry
            if entry is not None and entry.role is base_registry.BaseRole.DERIVED_FROM and entry.tag:
                refs.pinned.setdefault(base, set()).add(entry.tag)
            else:
                refs.protected.add(base)
            log.info("maintenance_base_ref", extra={"referrer": uri, "protects": base, "standing": judgement.standing.value})
    return refs


def sibling_base_refs(
    location: str,
    storage_options: StorageOptions,
    *,
    configured: Sequence[str],
    record_of: Callable[[str], BaseRecord | None],
) -> BaseRefs:
    """Every root a sanctioned base of a dataset laid out ALONGSIDE ``location`` names.

    Judged exactly as :func:`protected_roots` judges the sweep's estate ([[LH-279]]): ``configured`` and
    ``record_of`` are the same two inputs, so the on-demand doors and the sweep cannot disagree about
    whether a planted base freezes its victim.

    Lives here rather than in either consumer because BOTH need the identical refusal: the catalog's
    on-demand maintenance doors (`require_compactable` / `require_reclaimable`) and the maintenance
    service's event lane, which resolves one dataset per write event and cannot run the sweep's
    whole-estate pre-pass. A per-service copy of this would drift, which is the same reasoning that
    put `protected_roots` and the policy registry in service-kit.

    THE BOUND IS ONE DIRECTORY LISTING, and it is stated rather than implied: ``root`` is the dataset's
    own PARENT, so a referrer under any other parent is invisible here — in another warehouse, or
    simply at another depth in the same bucket.

    MEASURED ESTATE-WIDE 2026-09-08, 419 flag-16 datasets over 93 buckets, 539 base references:

        all refs           same-root  11   cross-root 125   CROSS-BUCKET 403
        declared ROOTS      same-root   5   cross-root 115   CROSS-BUCKET   0

    **No declared dataset root crosses a bucket**, which is what makes this bound defensible: the
    hazardous shape is the CLONE, and a clone's source shares its warehouse. The 403 that do cross are
    ``is_dataset_root=False`` external blob bases (409 of them naming one model-artefact prefix that
    holds no Lance dataset at all), which this guard is not protecting and could not damage.

    **The 115 cross-ROOT declared roots are real and this listing cannot see them** — a branch lives at
    ``<dataset>/tree/<name>`` while the dataset it protects sits at the bucket's top level, so neither
    appears in the other's parent listing. That gap is bounded to the callers here: the SWEEP does not
    use this function, `sweep.py::_protected_roots` opens every discovered dataset in every bucket and
    `discover_datasets` descends into ``tree/``. Closing it for the on-demand lane needs the referrer
    edge recorded AT CREATION (§ C3), not a wider listing — walking every warehouse on every event pays
    the sweep's cost on every write.

    Computed PER CALL, never cached: a clone created a minute ago must protect its source on the next
    event, which is also why an hourly backstop cannot stand in for this check.

    The listing is ONE non-recursive call because the layout is flat: the ``dir`` backend does not nest
    a table under its namespace, it encodes both into one directory name
    (``<uuid8>_<namespace>$<table>``) directly under the root. Anything that is not a Lance dataset
    simply fails to open and lands in ``unreadable``, which the caller can see.
    """
    root = location.rstrip("/").rsplit("/", 1)[0]
    fs, base = fs_and_base(root, storage_options)
    siblings = [
        f"{root.rstrip('/')}/{info.path.rstrip('/').rsplit('/', 1)[-1]}"
        for info in fs.get_file_info(pafs.FileSelector(base, recursive=False, allow_not_found=True))
        if info.type == pafs.FileType.Directory
    ]
    return protected_roots(siblings, storage_options, configured=configured, record_of=record_of)
