"""What a create or a register may leave a table declaring, and where a table may be registered ([[LH-279]]).

The catalog's two table-making doors are the only writers of the ``_bases/`` record
(:mod:`lakehouse.base_registry`), and this is their half of it: the entries each door records,
the register door's judge of the bases a dataset arrives declaring, and the location-exclusivity rule
that keeps every table prefix — and so every vended session policy — off the control root.

LOCATION-EXCLUSIVITY (no table location may equal, contain or sit under another table, the namespace root, or a
control prefix) is enforced here because the ``dir`` backend enforces none of it: measured on pylance 12.0.0 (lh279 m4) it accepts a register at
``.``, at ``_bases`` and at another table's directory. A write vend is scoped to the table's prefix, so a
table registered at ``.`` is vended ``<bucket>/*`` and one at ``_bases`` is vended the record store itself
(lh279 s1). The rule is what makes the record unreachable by any vend.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import lance
import pyarrow as pa
import pyarrow.fs as pafs
from lance_namespace import (
    ConcurrentModificationError,
    DescribeTableRequest,
    DescribeTableResponse,
    InvalidInputError,
    LanceNamespace,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from pydantic import BaseModel, ConfigDict, Field

from catalog.core.base_judge import BaseJudge, GovernedStorage
from catalog.core.config import shared_lance_session
from catalog.core.identifiers import is_spelled_as_a_uuid
from catalog.core.namespace import registered_dataset_facts
from catalog.core.run_context import RunInput
from catalog.core.vending import IAM_POLICY_SYNTAX
from catalog.services import native, table_claims, table_inputs
from lakehouse import base_registry, blobs, clone_sources, location_claims
from lakehouse.base_refs import decoded_segments, location_in_store, location_within, names_a_location, normalise, store_of
from lakehouse.features import FLAG_MIXED_DATA_FILE_VERSIONS, BasePathRef
from lakehouse.history_fences import HistoryFencedError
from lakehouse.objectfs import fs_and_base
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The spec's object index on a ``dir`` root (``<root>/__manifest``): one row per namespace and table,
#: and for a table its ``location`` relative to the root (lh279 s1). The only location index the
#: exclusivity rule needs.
_MANIFEST_DIR = "__manifest"
_TABLE_ROW = "table"

#: Rows a namespace root's manifest can hold for one location are the registering table's own and,
#: if the rule is being broken, somebody else's.
_ONE_TABLE = 1


def create_entries(external_blob_bases: Sequence[str], data_bases: Sequence[tuple[str, str]]) -> list[base_registry.RecordedBase]:
    """The record a CREATE writes: every base it registers through ``initial_bases``, by role.

    ``data_bases`` pairs each approved data base with the name the manifest registers it under.
    """
    blobs = [
        base_registry.RecordedBase(path=base, role=base_registry.BaseRole.EXTERNAL_BLOB, origin=base_registry.BaseOrigin.CREATE) for base in external_blob_bases
    ]
    data = [
        base_registry.RecordedBase(path=base, role=base_registry.BaseRole.DATA, name=name, origin=base_registry.BaseOrigin.CREATE) for base, name in data_bases
    ]
    return [*blobs, *data]


def requested_external_blob_base(
    requested: str | None,
    schema: pa.Schema,
    *,
    approved: Sequence[str],
    governed: GovernedStorage,
) -> str | None:
    """The one external blob base a create registers, or ``None`` when it asks for none ([[LH-209]]).

    A base is registered ONLY on request, and the request is the create door's ``external_blob_base``
    extension parameter (``catalog.api.rask_params.RaskExternalBlobBase``), which ingest's catalog client
    sends. It is a request the catalog judges and records, never a key the writer stamps on the payload:
    the table's schema metadata is the catalog's to write ([[LH-208]]). A create that names no base
    registers none, so its blob columns hold managed bytes and a ``Blob.from_uri`` pointer
    in its rows is refused by Lance at the write ("outside registered external bases",
    ``lance_docs/lance/guide/blob.md`` § blob v2: "external blob URIs must map to a registered non-dataset-root base
    path"). Registering every ``LANCE_EXTERNAL_BLOB_BASES`` entry on every create would let every table
    point at every object under them.

    The base is authorized by WHERE IT IS as well as by the allowlist, because Lance resolves every
    pointer as a path relative to it (``blob_id`` > 0), so whatever the base covers, every pointer
    through it may name. Approval says the operator trusts the prefix as an external source; it does not
    say the prefix holds no tenant's bytes. So a base is refused when it overlaps a governed root (the
    catalog root or the control root, each either way) or sits in a bucket the catalog governs (a reserved
    bucket or a warehouse's claimed one): no pointer under a registered base can then reach a table or a
    record.

    Raises:
        InvalidInputError: A base is named on a schema with no blob-v2 column, does not name one location,
            is outside every approved external blob base, or overlaps governed storage. Nothing was written.
    """
    if not requested:
        return None
    base = requested.strip()
    if not blobs.schema_has_blob(schema):
        raise InvalidInputError(f"the create names external blob base {base!r} and the table has no blob column to point through it")
    if not names_a_location(base) or not any(location_in_store(entry, base) for entry in approved):
        raise InvalidInputError(f"external blob base {base!r} is not inside an approved external blob base (LANCE_EXTERNAL_BLOB_BASES)")
    if governed.covers(base):
        raise InvalidInputError(
            f"external blob base {base!r} overlaps storage the catalog governs, so a pointer through it could name another table's, model's or tenant's bytes"
        )
    return base


def require_registrable_location(location: str | None, *, root: str, control_root: str, configured: Sequence[str]) -> str:
    """The relative ``location`` a register names, refused when its shape alone breaks exclusivity.

    ``root`` is the namespace root the backend resolves the location against. Answered from the path
    alone, BEFORE anything is attached:

    - it must be relative (the backend refuses an absolute one too; this door refuses it first);
    - its spelling must name the location the store opens, by the rule the base judge applies
      (:func:`~lakehouse.base_refs.names_a_location`): each segment, percent-decoded once, is
      not empty, ``.`` or ``..`` (which also refuses the namespace root itself), holds no ASCII control
      character, no ``?`` and no ``/`` or ``\\`` the decode added. The store keeps a ``?`` under a re-encoded name
      that holds no table, so an id registered there finds no table on query or count (measured on pylance 13.0.0
      over the dir backend, [[LH-435]]). Measured over moto on pylance 12.0.0 (lh279 r4
      register_probe): the backend refuses ``\\t..`` and ``..\\t`` but accepts ``.\\t.``, ``.\\n.`` and
      ``.\\r.``, and pylance drops the control character and resolves the ``..`` left behind, so
      ``x/.\\t./<victim>`` opens the victim; any other control character leaves a location the store will not
      open. A ``%`` that leaves only ordinary characters after one decode is part of a name (``growth%``,
      ``r%C3%A4ksm%C3%B6rg%C3%A5s``): the backend stores it re-encoded, so the table opens the spelling
      literally (lh279 r5 register_probe: ``<victim>%20`` is described as ``<victim>%2520`` and reads
      nothing of the victim). Every check below compares spellings, and a comparison of spellings answers
      only for one that names its location;
    - no segment may hold IAM policy syntax once decoded (``*`` or ``${``; a ``?`` names no location): the location is
      the table's object-store prefix, which every vended session policy names, where IAM reads a wildcard as widening
      the credential to sibling objects and ``${`` as a policy variable it substitutes
      (:data:`catalog.core.vending.IAM_POLICY_SYNTAX`, [[LH-437]], [[LH-526]]);
    - no segment may begin with ``_`` once decoded: every control-root prefix does (``_bases``,
      ``_protection``, ``_trash``, ``__manifest`` …), and a glob keeps a prefix the next feature adds out of
      every vend without anyone remembering to list it. Judged decoded, so a ``%5F`` is refused on this
      door's own word rather than on the backend re-encoding it;
    - its first segment, the directory directly under the namespace root, may not be spelled as a uuid once decoded
      ([[LH-520]]): that is where the catalog lays out the tables it makes, each directory named after its table's uuid
      (ADR 0173 §5). A table registered there would be read as one the catalog made, and one inside such a directory
      would sit in a catalog-made table's bytes, which no row in the namespace's object index names once that table is
      trashed. The catalog's own re-registers, a rename's and both undrops', do not pass this door;
    - it may not equal or contain ``control_root``. Sitting UNDER it is how every table lives when the
      control root is the namespace root (the shipped chart), and the prefix rule above already keeps
      a table off the records beneath it;
    - it may not equal, contain or sit under a ``configured`` base (the external blob and approved data
      bases). A table containing one is vended write access to the bytes that base's pointers name; a
      table beneath one is readable through every table that registers that base as its external blob base.

    Raises:
        InvalidInputError: The location breaks the rule; the message says which part.
    """
    stated = (location or "").strip()
    if "://" in stated or stated.startswith("/"):
        raise InvalidInputError(f"register location {stated!r} must be relative to the namespace root")
    relative = stated.strip("/")
    decoded = decoded_segments(relative)
    if decoded is None:
        raise InvalidInputError(
            f"register location {stated!r} must name one directory beneath the namespace root: no segment may decode to nothing, '.' "
            "or '..', or hold a control character, a '?' or a separator its escapes add, because such a spelling does not name the location "
            "the store would open"
        )
    if held := [token for token in IAM_POLICY_SYNTAX if any(token in segment for segment in decoded)]:
        raise InvalidInputError(
            f"register location {stated!r} holds IAM policy syntax {held}: every credential vended for the table names its location, "
            "where IAM reads '*' and '?' as wildcards that widen the credential to sibling objects and '${' as a policy variable"
        )
    if any(segment.startswith("_") for segment in decoded):
        raise InvalidInputError(f"register location {stated!r} has a segment beginning with '_', which is reserved for the catalog's own records")
    if is_spelled_as_a_uuid(decoded[0]):
        raise InvalidInputError(
            f"register location {stated!r} is in a uuid directory directly under the namespace root, where the catalog lays out the tables it makes"
        )
    absolute = f"{root.rstrip('/')}/{relative}"
    if location_within(absolute, control_root):
        raise InvalidInputError(f"register location {stated!r} contains the catalog's control root")
    for base in configured:
        if location_within(absolute, base) or location_within(base, absolute):
            raise InvalidInputError(f"register location {stated!r} overlaps a configured base, which every vended credential may read")
    return relative


def _manifest_table_locations(root: str, storage_options: StorageOptions) -> list[str]:
    """Every table location the namespace root's ``__manifest`` records, normalised and absolute."""
    fs, base = fs_and_base(root, storage_options)
    if fs.get_file_info(f"{base}/{_MANIFEST_DIR}/_versions").type != pafs.FileType.Directory:
        return []
    manifest = lance.dataset(f"{root.rstrip('/')}/{_MANIFEST_DIR}", storage_options=storage_options.in_clear(), session=shared_lance_session())
    rows = manifest.to_table(columns=["object_type", "location"])
    locations: list[str] = []
    for kind, stored in zip(rows.column("object_type").to_pylist(), rows.column("location").to_pylist(), strict=True):
        if kind != _TABLE_ROW or stored is None:
            continue
        text = str(stored).strip()
        if "://" in text:
            locations.append(normalise(text))
        elif text.strip("/") in ("", "."):
            locations.append(normalise(root))
        else:
            locations.append(normalise(f"{root.rstrip('/')}/{text.strip('/')}"))
    return locations


def occupied_location_refusal(relative: str) -> InvalidInputError:
    """The refusal of a register at a location another table resolves to or holds.

    One sentence for the overlap check and the location claim alike, so a caller cannot tell which of the
    two answered, and neither names the other table: the caller may hold no grant on it ([[LH-436]]).
    """
    return InvalidInputError(f"register location {relative!r} equals, contains or sits under another table's location")


def require_exclusive_location(root: str, relative: str, storage_options: StorageOptions) -> None:
    """Refuse a registered location that equals, contains or sits under another registered table's.

    Asked AFTER the backend has recorded this registration, and that order is the concurrency answer:
    the manifest commits are serialised by the store, so of two registrations that overlap, the later
    one always reads the earlier one's row and refuses. Asked before, both could read a manifest
    holding neither and both succeed.

    Raises:
        InvalidInputError: Another table's location overlaps this one.
        ServiceUnavailableError: The manifest could not be read, so nothing was judged.
    """
    mine = normalise(f"{root.rstrip('/')}/{relative}")
    try:
        locations = _manifest_table_locations(root, storage_options)
    except Exception as exc:
        log.warning("register_manifest_unreadable", extra={"root": root, "error": str(exc)[:300]})
        raise ServiceUnavailableError("the namespace's object index could not be read, so the registered location was not judged") from exc
    same = sum(1 for location in locations if location == mine)
    overlapping = [location for location in locations if location != mine and (location_within(location, mine) or location_within(mine, location))]
    if same > _ONE_TABLE or overlapping:
        log.warning("register_refused_overlapping_location", extra={"location": mine, "overlaps": overlapping[:5], "same": same})
        raise occupied_location_refusal(relative)


class CloneAdmission(BaseModel):
    """What a registered shallow clone is admitted with: the sources to tag, and the record entries its roots earn."""

    #: Every table the clone reads through, each at the version its tag pins (:mod:`lakehouse.clone_sources`): the
    #: source its first transaction names, and each table that source itself reads through (a clone of a clone reads
    #: the first source's files where they are, measured on pylance 13.0.0).
    sources: list[clone_sources.CloneSource] = Field(default_factory=list)
    #: A ``derived_from`` entry per dataset root the clone declares through those sources: each source's root, carrying
    #: the version and branch pinned there, and a source branch's own directory, which lies inside its table's root.
    entries: list[base_registry.RecordedBase] = Field(default_factory=list)
    #: The root, as the clone's own manifest spells it, of a source no table the catalog governs holds; ``None`` when
    #: every source is governed.
    ungoverned: str | None = None

    def admits(self, ref: BasePathRef) -> base_registry.RecordedBase | None:
        """The entry admitting the declared base ``ref``, or ``None`` when this admission does not cover it."""
        key = (normalise(ref.path), store_of(ref.path), ref.is_dataset_root)
        return next((entry for entry in self.entries if (entry.path, entry.store, entry.is_dataset_root) == key), None)


#: What registering a shallow clone asks of its caller on each source, in the order asked. The clone's owner reads the
#: source's rows through it, so it asks what a read asks; its registration writes a tag there (ADR 0198 §1), so it asks
#: what the tag door asks as well (owner, 2026-11-09). The model grants neither to a holder of the other: a publisher
#: moves a pointer and reads nothing.
CLONE_RELATIONS = ("can_read_data", "can_create_tag")


class _Pin(BaseModel):
    """One dataset root a clone reads through, and the version and branch of it the clone reads."""

    model_config = ConfigDict(frozen=True)

    ref: BasePathRef
    version: int
    branch: str | None = None


def clone_read(location: str, so: StorageOptions, context: RegistrationContext) -> CloneAdmission | None:
    """What the dataset at ``location`` reads through as a shallow clone, read before anything is registered so its caller
    can be asked for each source first; ``None`` when no dataset is there. A source no table holds is named in
    ``ungoverned`` rather than refused here, so the door refuses it as it refuses a source its caller may not tag.

    Raises:
        ServiceUnavailableError: The dataset, a claim or a record could not be read, so nothing was judged.
    """
    try:
        facts = registered_dataset_facts(location, so)
    except Exception as exc:
        log.warning("register_clone_unreadable", extra={"location": location, "error": str(exc)[:300]})
        raise ServiceUnavailableError("the dataset at the registered location could not be opened to read what it clones") from exc
    return None if facts is None else clone_of(facts.clone, facts.bases, location=location, context=context)


def _same_place(left: str, right: str) -> bool:
    return location_in_store(left, right) and location_in_store(right, left)


def _pins(reference: clone_sources.CloneReference | None, foreign: Sequence[BasePathRef], *, location: str, judge: BaseJudge) -> list[_Pin]:
    """Each foreign dataset root the clone reads through as a table of its own, at the version it reads there.

    The source the clone's first transaction names comes first, and then each root that source's record holds as one it
    reads through at a pinned version: a clone of a clone inherits its source's bases. A clone whose own version expiry
    has removed that first transaction (measured on pylance 13.0.0: ``cleanup_old_versions`` takes it with the clone's
    first version) is read from its own record instead, which a registration of it wrote.

    Raises:
        ServiceUnavailableError: A record could not be read.
    """
    if reference is None:
        return _recorded_pins(judge.read_record(location), foreign)
    direct = next((ref for ref in foreign if _same_place(ref.path, reference.root)), None)
    if direct is None:
        return []
    beyond = [ref for ref in foreign if ref is not direct and not location_in_store(direct.path, ref.path)]
    inherited = _recorded_pins(judge.read_record(direct.path), beyond) if beyond else []
    return [_Pin(ref=direct, version=reference.version, branch=reference.branch), *inherited]


def _recorded_pins(record: base_registry.BaseRecord | None, refs: Sequence[BasePathRef]) -> list[_Pin]:
    """Those of ``refs`` that ``record`` holds as a source table's own root read at a pinned version."""
    pins: list[_Pin] = []
    for ref in refs:
        key = (normalise(ref.path), store_of(ref.path))
        entry = next((held for held in (record.entries if record is not None else []) if _pins_a_table(held) and (held.path, held.store) == key), None)
        if entry is not None and entry.version is not None:
            pins.append(_Pin(ref=ref, version=entry.version, branch=entry.branch))
    return pins


def _pins_a_table(entry: base_registry.RecordedBase) -> bool:
    """Whether ``entry`` records a source table's own root at a pinned version, rather than a branch's directory in it."""
    return entry.role is base_registry.BaseRole.DERIVED_FROM and entry.version is not None and entry.source_table in (None, entry.path)


def _governed_source(pin: _Pin, context: RegistrationContext) -> clone_sources.CloneSource | None:
    """The table the catalog governs at the source root ``pin`` names, as the clone reads it, or ``None`` when no claim
    holds it.

    Raises:
        ServiceUnavailableError: The claim could not be read.
    """
    claims = location_claims.ClaimStore(control_root=context.registry.control_root, storage_options=context.registry.storage_options)
    try:
        held = location_claims.holder(claims, pin.ref.path)
    except (ValueError, OSError) as exc:
        raise ServiceUnavailableError("the claim of a clone's source could not be read, so the clone was not judged") from exc
    if held is None or held.uuid is None:
        return None
    return clone_sources.CloneSource(
        location=pin.ref.path, table_uuid=held.uuid, table=held.table, segments=held.segments, version=pin.version, branch=pin.branch
    )


def refused_clone(path: str) -> PermissionDeniedError:
    """The refusal of a clone whose source its caller holds nothing on, the same whether a governed table is at ``path``
    or not: it names the first relation asked and only the path the caller's own manifest holds ([[LH-436]])."""
    return PermissionDeniedError(f"{CLONE_RELATIONS[0]} required on {path}")


def clone_of(reference: clone_sources.CloneReference | None, bases: Sequence[BasePathRef], *, location: str, context: RegistrationContext) -> CloneAdmission:
    """What a dataset declaring ``bases`` reads through as a shallow clone; an empty admission for one that is none, and
    one naming ``ungoverned`` for the first source no table holds, which :func:`admit_clone` refuses. The sources read
    before that one stay in the admission, in the order the clone reads through them, so its caller is asked for each
    in that order and a refusal never skips past a governed source to name what lies behind it ([[LH-436]]).

    Raises:
        ServiceUnavailableError: A claim or a record could not be read.
    """
    foreign = [ref for ref in bases if ref.is_dataset_root and not base_registry.is_own(location, ref)]
    if not foreign:
        return CloneAdmission()
    judge = BaseJudge(registry=context.registry, configured=list(context.configured), governed=context.governed)
    pins = _pins(reference, foreign, location=location, judge=judge)
    sources: list[clone_sources.CloneSource] = []
    for pin in pins:
        if (source := _governed_source(pin, context)) is None:
            return CloneAdmission(sources=sources, ungoverned=pin.ref.path)
        sources.append(source)
    entries = [_derived_from(ref, pin) for pin in pins for ref in foreign if location_in_store(pin.ref.path, ref.path)]
    return CloneAdmission(sources=sources, entries=entries)


def admit_clone(reference: clone_sources.CloneReference | None, bases: Sequence[BasePathRef], *, location: str, context: RegistrationContext) -> CloneAdmission:
    """The sources a dataset reads through as a shallow clone of tables the catalog governs (ADR 0198 §1).

    A clone's source is a dataset-root base outside its own root (`lance_docs/lance/format/table/layout.md` § Shallow
    Clone), and it is admitted only where the dataset's own first transaction is a shallow ``Clone`` naming that root
    (:func:`clone_sources.clone_reference`), or the catalog's record of an earlier registration of it says so:
    ``add_bases`` writes the same base without one, which is a plant ([[LH-279]]). Each source must be a table the
    catalog holds a claim for, so the version is tagged on a table whose history fence the catalog keeps. A clone of a
    clone reads its source's own sources too, each admitted where that source's record pins it, and tagged for this
    clone as well: the first clone's end releases only its own tag. A dataset-root base this admits nothing for is left
    to the base judge, which refuses it.

    Raises:
        PermissionDeniedError: A source is no table the catalog governs, refused as one its caller may not tag
            (:func:`refused_clone`).
        ServiceUnavailableError: A claim or a record could not be read.
    """
    admission = clone_of(reference, bases, location=location, context=context)
    if admission.ungoverned is not None:
        log.warning("register_refused_ungoverned_clone_source", extra={"location": location, "source": admission.ungoverned})
        raise refused_clone(admission.ungoverned)
    return admission


def _derived_from(ref: BasePathRef, pin: _Pin) -> base_registry.RecordedBase:
    """The record entry of the declared root ``ref``, read through the source table ``pin`` names."""
    return base_registry.RecordedBase(
        path=ref.path,
        role=base_registry.BaseRole.DERIVED_FROM,
        is_dataset_root=True,
        name=ref.name,
        origin=base_registry.BaseOrigin.REGISTER,
        source_table=pin.ref.path,
        version=pin.version,
        branch=pin.branch,
    )


def _read_through_with_no_tag_to_take(judgement: base_registry.BaseJudgement) -> bool:
    """Whether the record holds the base as a root read through another table. Asked of a base the clone's admission does
    not cover, which is one the registration could take no tag for: the record pins no version of it, or no table the
    catalog governs holds it any more. A clone governed with no tag is broken by its source's next cleanup."""
    entry = judgement.entry
    return judgement.standing is base_registry.BaseStanding.RECORDED and entry is not None and entry.role is base_registry.BaseRole.DERIVED_FROM


def entries_for_registration(
    location: str,
    bases: Sequence[BasePathRef],
    *,
    registry: base_registry.BaseRegistry,
    configured: Sequence[str],
    data_allowlist: Sequence[str],
    governed: GovernedStorage | None = None,
    clone: CloneAdmission | None = None,
) -> list[base_registry.RecordedBase]:
    """The record entries a registered dataset's bases earn — or a refusal naming the ones nothing sanctions.

    A base is admitted when it is inside the table's own root, inside a configured external blob base and
    outside ``governed`` storage (recorded as ``external_blob``; [[LH-209]]: a configured base over
    governed storage is not recorded on the dataset's word), or already in the table's record. A plain base BENEATH an
    approved multi-base data base (``LANCE_MULTIBASE_DATA_BASES``), in its store, is admitted as ``data`` only while
    the table has NO record yet: the allowlist is a gate at the moment the record is first written, and
    once written the record is the authority — an allowlisted base appearing later is a plant. The approved
    base itself is refused ([[LH-252]]): it is every table's prefix, and a table declaring it reads every
    sibling's fragments. So is a directory deeper than one level beneath it, and a directory another table
    holds is refused when its claim is taken (:func:`admit_registered_table`). A source root ``clone`` admits is recorded
    as ``derived_from`` (:func:`admit_clone`).

    Raises:
        InvalidInputError: A declared base is sanctioned by none of those.
        ServiceUnavailableError: The table's existing record could not be read, or its store did not answer.
    """
    state: dict[str, base_registry.BaseRecord | None] = {}
    judge = BaseJudge(registry=registry, configured=list(configured), governed=governed)

    def _load() -> base_registry.BaseRecord | None:
        # The judge's reader: a record that cannot be read, or a store that does not answer, is a typed 503.
        state["record"] = judge.read_record(location)
        return state["record"]

    judged = judge.judge(location, bases)
    entries: list[base_registry.RecordedBase] = []
    refused: list[str] = []
    untagged: list[str] = []
    for judgement in judged:
        ref = judgement.ref
        if clone is not None and (derived := clone.admits(ref)) is not None:
            entries.append(derived)
        elif judgement.standing is base_registry.BaseStanding.CONFIGURED:
            entries.append(
                base_registry.RecordedBase(path=ref.path, role=base_registry.BaseRole.EXTERNAL_BLOB, name=ref.name, origin=base_registry.BaseOrigin.REGISTER)
            )
        elif _read_through_with_no_tag_to_take(judgement):
            untagged.append(ref.path)
        elif judgement.standing is base_registry.BaseStanding.UNRECORDED:
            first_record = (state["record"] if "record" in state else _load()) is None
            if first_record and not ref.is_dataset_root and names_a_location(ref.path) and _beneath_an_approved_base(ref.path, data_allowlist):
                entries.append(
                    base_registry.RecordedBase(path=ref.path, role=base_registry.BaseRole.DATA, name=ref.name, origin=base_registry.BaseOrigin.REGISTER)
                )
            else:
                refused.append(ref.path)
    if untagged:
        log.warning("register_refused_untaggable_sources", extra={"location": normalise(location), "bases": untagged[:10]})
        raise InvalidInputError(
            f"the dataset reads through {untagged[:10]}, which the catalog's record for it holds as another table's root, and this "
            "registration can keep no version of it: the record pins none, or no table the catalog governs is there any more. "
            "Clone a version of a governed table again and register that."
        )
    if refused:
        log.warning("register_refused_unrecorded_bases", extra={"location": normalise(location), "bases": refused[:10]})
        raise InvalidInputError(
            f"the dataset declares base(s) {refused[:10]} that are not inside the table's own location, not a configured external "
            "blob base, not an approved data base and not in the catalog's record for it. A manifest's base list can be written by "
            "any holder of the table's write credential, so the catalog does not take it on the manifest's word. Recreate the "
            "dataset without the base, or register a location whose bases the operator has approved."
        )
    return entries


def _beneath_an_approved_base(path: str, data_allowlist: Sequence[str]) -> bool:
    """Whether ``path`` is ONE directory beneath an approved data base, in its store — a table's directory, never the base.

    One level, the shape a create mints (``dataplane.table_data_bases``): every table's directory is then
    a sibling of every other's, so whether two tables overlap is whether they name the same directory —
    the question the directory's location claim answers exactly ([[LH-252]]).
    """
    inner = decoded_segments(path)
    for base in data_allowlist:
        outer = decoded_segments(base)
        if outer is not None and inner is not None and location_in_store(base, path) and len(inner) == len(outer) + 1:
            return True
    return False


class RegistrationContext(BaseModel):
    """What the register door judges a registration against, resolved once per request."""

    #: The namespace root the backend resolved the relative location against.
    root: str
    #: Where the base records live.
    registry: base_registry.BaseRegistry
    #: ``LANCE_EXTERNAL_BLOB_BASES`` — sanctioned by configuration.
    configured: list[str]
    #: ``LANCE_MULTIBASE_DATA_BASES`` — admitted while the record is first written.
    data_allowlist: list[str]
    #: Where a configured base earns no standing ([[LH-209]]); ``None`` judges configuration alone.
    governed: GovernedStorage | None = None
    #: The registering table's canonical id: the holder of the data directories it is admitted with ([[LH-252]]).
    holder: str


class RegistrationVerdict(BaseModel):
    """A registration the door judged: where it resolved, the record entries it earns, and the sources a clone reads."""

    location: str
    #: The record entries the dataset's bases earn, claimed by :func:`admit_registered_table`; empty when no dataset is
    #: at the location, which has no bases to judge or record.
    entries: list[base_registry.RecordedBase] = Field(default_factory=list)
    #: The sources the dataset reads through as a shallow clone, each tagged before the record is claimed.
    sources: list[clone_sources.CloneSource] = Field(default_factory=list)
    #: Whether a dataset is at the location: only one that is gets a base record, its entries or none.
    holds_dataset: bool = False


class Admitted(BaseModel):
    """What :func:`admit_registered_table` wrote, which the door's undo releases."""

    #: ``None`` when the dataset had no bases to record.
    claim: base_registry.BaseClaim | None = None
    #: The data directories claimed for the table ([[LH-252]]).
    directories: list[str] = Field(default_factory=list)
    #: The source locations the table's clone tag was taken on.
    tagged: list[str] = Field(default_factory=list)


def judge_registered_table(ns: LanceNamespace, so: StorageOptions, segments: list[str], relative: str, context: RegistrationContext) -> RegistrationVerdict:
    """Judge the dataset a registration just attached ([[LH-279]]); it writes nothing.

    In order: the location the backend RESOLVED, which is right for a warehouse-bound table too; no other
    registered table may overlap it (:func:`require_exclusive_location`, asked after the
    registration so a concurrent overlap is caught by whichever lands second); the dataset must not carry
    reader flag 256, must have stable row ids and must carry no ``lance.compaction.*`` config ([[LH-538]]); and every base it declares must be its own, configured, approved, already recorded,
    or a source it reads through as a shallow clone (:func:`entries_for_registration`, :func:`admit_clone`). The door
    then asks the caller's read on each clone source and admits it (:func:`admit_registered_table`), so a refused
    dataset leaves no entry behind.

    Only bit 256 is read: flag 16 is what an externally based ingest dataset carries, and it is no reason
    to refuse one. A location holding no dataset has no data files or bases to judge, and registers as the
    backend allows, with no record. Two stated bounds: only the MAIN branch is judged (a mixed or based
    ``tree/<b>`` registers, and the maintenance alert pages on the first), and the open uses the estate's
    default storage options, so for a warehouse whose record names its own endpoint ([[LH-067]]; none
    does today) a missing bucket is refused 503 while a same-named bucket without the dataset reads as
    absent and registers unjudged.

    Raises:
        InvalidInputError: Another table overlaps the location, the dataset mixes data file versions, it
            has no stable row ids, it carries compaction config, or it declares a base nothing sanctions.
        ServiceUnavailableError: The location, the object index or the record could not be read, so
            nothing was judged.
    """
    table = ".".join(segments)
    try:
        described: DescribeTableResponse = native.call(ns, "describe_table", DescribeTableRequest(id=segments))
        if not described.location:
            raise TableNotFoundError(f"table {table} describes no location")
        location = described.location
    except Exception as exc:
        log.warning("register_location_unreadable", extra={"table": table, "error": str(exc)[:300]})
        raise ServiceUnavailableError(f"the table registered at {table} could not be described, so its dataset was not judged") from exc
    require_exclusive_location(context.root, relative, so)
    try:
        facts = registered_dataset_facts(location, so)
    except Exception as exc:
        log.warning("register_flags_unreadable", extra={"table": table, "error": str(exc)[:300]})
        raise ServiceUnavailableError(f"the dataset registered at {table} could not be opened to read its feature flags") from exc
    if facts is None:
        return RegistrationVerdict(location=location)
    if facts.mixed:
        log.warning("register_refused_mixed_file_versions", extra={"table": table, "location": location})
        raise InvalidInputError(
            f"the dataset at {location!r} carries reader flag {FLAG_MIXED_DATA_FILE_VERSIONS} (mixed data file versions): its "
            "data files sit at more than one Lance file version, and no operation removes the flag. Maintenance refuses such a table, "
            "and pylance 11 and lancedb 0.34 cannot open it. Recreate it into a new dataset written at one data_storage_version, then "
            "register that."
        )
    if not facts.stable_row_ids:
        log.warning("register_refused_unstable_row_ids", extra={"table": table, "location": location})
        raise InvalidInputError(
            f"the dataset at {location!r} was created without stable row ids: its `_rowid` is a physical address that compaction "
            "rewrites, Lance tracks no row versions for it, and its change feed would answer empty windows. Stable row ids are "
            "create-time-only through pylance (lance_docs/lance/format/table/row_id_lineage.md:73-83; the format's migration is Rust-only); "
            "recreate it with enable_stable_row_ids=True, then register that."
        )
    if facts.compaction_config:
        log.warning("register_refused_compaction_config", extra={"table": table, "location": location, "keys": facts.compaction_config})
        raise InvalidInputError(
            f"the dataset at {location!r} carries {facts.compaction_config} in its manifest config, which Lance would plan a compaction "
            "from in place of the table's maintenance policy. Delete those keys (delete_config_keys), then register it."
        )
    clone = admit_clone(facts.clone, facts.bases, location=location, context=context)
    entries = entries_for_registration(
        location,
        facts.bases,
        registry=context.registry,
        configured=context.configured,
        data_allowlist=context.data_allowlist,
        governed=context.governed,
        clone=clone,
    )
    return RegistrationVerdict(location=location, entries=entries, sources=clone.sources, holds_dataset=True)


def admit_registered_table(
    ns: LanceNamespace, so: StorageOptions, *, segments: list[str], verdict: RegistrationVerdict, clone_uuid: str, context: RegistrationContext
) -> Admitted:
    """Write what a judged registration is admitted with: each clone source's tag, the data directories, the record.

    The tags first, so the record never names a source whose version nothing keeps; the record last, since it is
    what names the directories. Whatever this wrote is undone when a later step fails, and :func:`release_admitted`
    undoes it for the door's own undo.

    Raises:
        InvalidInputError: A source no longer holds the version the clone reads, or another table holds one of the
            data directories.
        ConcurrentModificationError: A reclaim holds a source's history; nothing was written for it.
    """
    admitted = Admitted()
    try:
        for source in verdict.sources:
            _tag(source, clone_location=verdict.location, so=so, context=context)
            admitted.tagged.append(source.location)
        admitted.directories = claim_registered_directories(ns, context.registry, verdict.entries, context.holder, segments)
        admitted.claim = base_registry.claim_bases(context.registry, verdict.location, verdict.entries) if verdict.holds_dataset else None
        if verdict.sources:
            # A clone holds its sources' rows, so an erasure on a source is held until the clone follows it (ADR 0178).
            declared = {source.table_uuid: RunInput(table=source.segments, version=source.version, branch=source.branch) for source in verdict.sources}
            table_inputs.record(context.registry.control_root, context.registry.storage_options, clone_uuid, name=context.holder, declared=declared)
    except Exception:
        release_admitted(admitted, clone_location=verdict.location, so=so, context=context)
        raise
    return admitted


def _tag(source: clone_sources.CloneSource, *, clone_location: str, so: StorageOptions, context: RegistrationContext) -> None:
    try:
        clone_sources.take(source, clone_location=clone_location, control_root=context.registry.control_root, storage_options=so)
    except clone_sources.CloneSourceGoneError as exc:
        raise InvalidInputError(
            f"the clone reads version {source.version} of {source.table}, which the source no longer keeps; clone a version it still holds, "
            "or tag it before cloning"
        ) from exc
    except HistoryFencedError as exc:
        raise ConcurrentModificationError(f"a reclaim holds the history of {source.table}; retry the register") from exc


def release_admitted(admitted: Admitted, *, clone_location: str, so: StorageOptions, context: RegistrationContext) -> None:
    """Undo :func:`admit_registered_table`: the record's entries, the directories' claims, then each source's tag."""
    if admitted.claim is not None:
        base_registry.release_claim(context.registry, admitted.claim)
    base_registry.release_data_directories(context.registry, admitted.directories, context.holder)
    clone_sources.release_all(admitted.tagged, clone_location=clone_location, storage_options=so)


def claim_registered_directories(
    ns: LanceNamespace, registry: base_registry.BaseRegistry, entries: Sequence[base_registry.RecordedBase], holder: str, segments: list[str]
) -> list[str]:
    """Claim each admitted data directory for ``holder``, refusing one another live table holds ([[LH-252]]).

    Raises:
        InvalidInputError: Another table holds one of the directories — a registration declaring it would
            read that table's fragments and be vended GET on them.
    """
    store = location_claims.ClaimStore(control_root=registry.control_root, storage_options=registry.storage_options)
    try:
        return base_registry.claim_data_directories(
            registry, entries, holder, segments, is_gone=lambda held: table_claims.data_directory_holder_is_gone(ns, store, held)
        )
    except location_claims.LocationHeldError as held:
        log.warning("register_refused_held_data_directory", extra={"table": holder, "directory": held.claim.location, "holder": held.claim.table})
        raise InvalidInputError(
            f"the dataset declares the data directory {held.claim.location!r}, which another table holds; a table's data "
            "directory is its own, so the catalog does not admit a second table reading through it"
        ) from None
