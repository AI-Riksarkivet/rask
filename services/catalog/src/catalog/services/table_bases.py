"""What a create or a register may leave a table declaring, and where a table may be registered ([[LH-279]]).

The catalog's two table-making doors are the only writers of the ``_bases/`` record
(:mod:`service_kit.lakehouse.base_registry`), and this is their half of it: the entries each door records,
the register door's judge of the bases a dataset arrives declaring, and the location-exclusivity rule
that keeps every table prefix — and so every vended session policy — off the control root.

LOCATION-EXCLUSIVITY is Lakekeeper's rule (``tabular/mod.rs`` and ``sign.rs``: no table location may equal,
contain or sit under another table, the namespace root, or a control prefix) translated onto the ``dir``
backend, which enforces none of it: measured on pylance 12.0.0 (lh279 m4) it accepts a register at
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
from lance_namespace import DescribeTableRequest, DescribeTableResponse, InvalidInputError, LanceNamespace, ServiceUnavailableError, TableNotFoundError
from pydantic import BaseModel, Field

from catalog.core.base_judge import BaseJudge, GovernedStorage
from catalog.core.config import shared_lance_session
from catalog.core.namespace import registered_dataset_facts
from catalog.services import native, table_claims
from service_kit.lakehouse import base_registry, blobs, location_claims
from service_kit.lakehouse.base_refs import decoded_segments, location_in_store, location_within, names_a_location, normalise
from service_kit.lakehouse.features import FLAG_MIXED_DATA_FILE_VERSIONS, BasePathRef
from service_kit.lakehouse.objectfs import StorageOptions, fs_and_base


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
    ``lance_docs/guide.md`` § blob v2: "external blob URIs must map to a registered non-dataset-root base
    path"). Registering every ``LANCE_EXTERNAL_BLOB_BASES`` entry on every create would let every table
    point at every object under them, and the default entry is the model-artifact tree of every project.

    The base is authorized by WHERE IT IS as well as by the allowlist, because Lance resolves every
    pointer as a path relative to it (``blob_id`` > 0), so whatever the base covers, every pointer
    through it may name. Approval says the operator trusts the prefix as an external source; it does not
    say the prefix holds no tenant's bytes. So a base is refused when it overlaps a governed root (the
    catalog root, the control root, the model registry or the model-artifact tree, each either way) or
    sits in a bucket the catalog governs (a reserved bucket or a warehouse's claimed one): no pointer
    under a registered base can then reach a table, a model artifact or a record.

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


def require_registrable_location(location: str | None, *, root: str, control_root: str, configured: Sequence[str], model_roots: Sequence[str]) -> str:
    """The relative ``location`` a register names, refused when its shape alone breaks exclusivity.

    ``root`` is the namespace root the backend resolves the location against. Answered from the path
    alone, BEFORE anything is attached:

    - it must be relative (the backend refuses an absolute one too; this door refuses it first);
    - its spelling must name the location the store opens, by the rule the base judge applies
      (:func:`~service_kit.lakehouse.base_refs.names_a_location`): each segment, percent-decoded once, is
      not empty, ``.`` or ``..`` (which also refuses the namespace root itself), holds no ASCII control
      character and no ``/`` or ``\\`` the decode added. Measured over moto on pylance 12.0.0 (lh279 r4
      register_probe): the backend refuses ``\\t..`` and ``..\\t`` but accepts ``.\\t.``, ``.\\n.`` and
      ``.\\r.``, and pylance drops the control character and resolves the ``..`` left behind, so
      ``x/.\\t./<victim>`` opens the victim; any other control character leaves a location the store will not
      open. A ``%`` that leaves only ordinary characters after one decode is part of a name (``growth%``,
      ``r%C3%A4ksm%C3%B6rg%C3%A5s``): the backend stores it re-encoded, so the table opens the spelling
      literally (lh279 r5 register_probe: ``<victim>%20`` is described as ``<victim>%2520`` and reads
      nothing of the victim). Every check below compares spellings, and a comparison of spellings answers
      only for one that names its location;
    - no segment may begin with ``_`` once decoded: every control-root prefix does (``_bases``,
      ``_protection``, ``_trash``, ``__manifest`` …), and a glob keeps a prefix the next feature adds out of
      every vend without anyone remembering to list it. Judged decoded, so a ``%5F`` is refused on this
      door's own word rather than on the backend re-encoding it;
    - it may not equal or contain ``control_root``. Sitting UNDER it is how every table lives when the
      control root is the namespace root (the shipped chart), and the prefix rule above already keeps
      a table off the records beneath it;
    - it may not equal, contain or sit under a ``configured`` base (the external blob and approved data
      bases). A table containing one is vended write access to the bytes that base's pointers name; a
      table beneath one is readable through every table that registers that base as its external blob base;
    - it may not equal, contain or sit under a ``model_roots`` entry (the model registry and the model
      artifact tree, [[LH-204]]). Those datasets and objects are opened by explicit URI and never
      registered, so a table over one would be vended write access to every model's weights and history.

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
            "or '..', or hold a control character or a separator its escapes add, because such a spelling does not name the location "
            "the store would open"
        )
    if any(segment.startswith("_") for segment in decoded):
        raise InvalidInputError(f"register location {stated!r} has a segment beginning with '_', which is reserved for the catalog's own records")
    absolute = f"{root.rstrip('/')}/{relative}"
    if location_within(absolute, control_root):
        raise InvalidInputError(f"register location {stated!r} contains the catalog's control root")
    for base in configured:
        if location_within(absolute, base) or location_within(base, absolute):
            raise InvalidInputError(f"register location {stated!r} overlaps a configured base, which every vended credential may read")
    for model_root in model_roots:
        if location_within(absolute, model_root) or location_within(model_root, absolute):
            raise InvalidInputError(f"register location {stated!r} overlaps the model registry, which no table may be vended")
    return relative


def _manifest_table_locations(root: str, storage_options: StorageOptions) -> list[str]:
    """Every table location the namespace root's ``__manifest`` records, normalised and absolute."""
    fs, base = fs_and_base(root, storage_options)
    if fs.get_file_info(f"{base}/{_MANIFEST_DIR}/_versions").type != pafs.FileType.Directory:
        return []
    manifest = lance.dataset(f"{root.rstrip('/')}/{_MANIFEST_DIR}", storage_options=storage_options, session=shared_lance_session())
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
        raise InvalidInputError(f"register location {relative!r} equals, contains or sits under another table's location")


def entries_for_registration(
    location: str,
    bases: Sequence[BasePathRef],
    *,
    registry: base_registry.BaseRegistry,
    configured: Sequence[str],
    data_allowlist: Sequence[str],
    governed: GovernedStorage | None = None,
) -> list[base_registry.RecordedBase]:
    """The record entries a registered dataset's bases earn — or a refusal naming the ones nothing sanctions.

    A base is admitted when it is inside the table's own root, inside a configured external blob base and
    outside ``governed`` storage (recorded as ``external_blob``; [[LH-209]]: a configured base over the
    model-artifact tree is not recorded on the dataset's word), or already in the table's record. A plain base BENEATH an
    approved multi-base data base (``LANCE_MULTIBASE_DATA_BASES``), in its store, is admitted as ``data`` only while
    the table has NO record yet: the allowlist is a gate at the moment the record is first written, and
    once written the record is the authority — an allowlisted base appearing later is a plant. The approved
    base itself is refused ([[LH-252]]): it is every table's prefix, and a table declaring it reads every
    sibling's fragments. So is a directory deeper than one level beneath it, and a directory another table
    holds is refused when its claim is taken (:func:`judge_registered_table`).

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
    for judgement in judged:
        ref = judgement.ref
        if judgement.standing is base_registry.BaseStanding.CONFIGURED:
            entries.append(
                base_registry.RecordedBase(path=ref.path, role=base_registry.BaseRole.EXTERNAL_BLOB, name=ref.name, origin=base_registry.BaseOrigin.REGISTER)
            )
        elif judgement.standing is base_registry.BaseStanding.UNRECORDED:
            first_record = (state["record"] if "record" in state else _load()) is None
            if first_record and not ref.is_dataset_root and names_a_location(ref.path) and _beneath_an_approved_base(ref.path, data_allowlist):
                entries.append(
                    base_registry.RecordedBase(path=ref.path, role=base_registry.BaseRole.DATA, name=ref.name, origin=base_registry.BaseOrigin.REGISTER)
                )
            else:
                refused.append(ref.path)
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
    """A registration the door admitted: where it resolved, and the record entries it claimed."""

    location: str
    #: ``None`` when no dataset is at the location — there were no bases to judge or record.
    claim: base_registry.BaseClaim | None = None
    #: The data directories claimed for the table ([[LH-252]]), released by the door's undo.
    directories: list[str] = Field(default_factory=list)


def judge_registered_table(ns: LanceNamespace, so: dict[str, str], segments: list[str], relative: str, context: RegistrationContext) -> RegistrationVerdict:
    """Judge the dataset a registration just attached, and record the bases it is admitted with ([[LH-279]]).

    In order: the location the backend RESOLVED, which is right for a warehouse-bound table too; no other
    registered table may overlap it (:func:`require_exclusive_location`, asked after the
    registration so a concurrent overlap is caught by whichever lands second); the dataset must not carry
    reader flag 256 and must have stable row ids; and every base it declares must be its own, configured, approved or already recorded
    (:func:`entries_for_registration`). Only then is the record claimed, so a refused dataset
    leaves no entry behind.

    Only bit 256 is read: flag 16 is what an externally based ingest dataset carries, and it is no reason
    to refuse one. A location holding no dataset has no data files or bases to judge, and registers as the
    backend allows, with no record. Two stated bounds: only the MAIN branch is judged (a mixed or based
    ``tree/<b>`` registers, and the maintenance alert pages on the first), and the open uses the estate's
    default storage options, so for a warehouse whose record names its own endpoint ([[LH-067]]; none
    does today) a missing bucket is refused 503 while a same-named bucket without the dataset reads as
    absent and registers unjudged.

    Raises:
        InvalidInputError: Another table overlaps the location, the dataset mixes data file versions, it
            has no stable row ids, or it declares a base nothing sanctions.
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
            "create-time-only (lance_docs/file_format.md:4011-4015); recreate it with enable_stable_row_ids=True, then register that."
        )
    entries = entries_for_registration(
        location, facts.bases, registry=context.registry, configured=context.configured, data_allowlist=context.data_allowlist, governed=context.governed
    )
    directories = claim_registered_directories(ns, context.registry, entries, context.holder, segments)
    try:
        claim = base_registry.claim_bases(context.registry, location, entries)
    except Exception:
        base_registry.release_data_directories(context.registry, directories, context.holder)
        raise
    return RegistrationVerdict(location=location, claim=claim, directories=directories)


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
            f"the dataset declares the data directory {held.claim.location!r}, which table {held.claim.table!r} holds; a table's data "
            "directory is its own, so the catalog does not admit a second table reading through it"
        ) from None
