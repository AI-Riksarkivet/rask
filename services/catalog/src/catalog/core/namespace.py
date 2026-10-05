"""Backend namespace construction and dataset resolution.

The REST server is an adapter over a native ``LanceNamespace`` backend
(``DirectoryNamespace`` on MinIO/S3 by default). ``open_dataset`` resolves a
table's storage location via the namespace and opens it with pylance — used by
the data-plane service for operations the native backend does not implement.
"""

from __future__ import annotations

import logging

import lance
from lance_namespace import (
    DescribeTableRequest,
    LanceNamespace,
    ServiceUnavailableError,
    TableBranchNotFoundError,
    TableNotFoundError,
    TableVersionNotFoundError,
    UnsupportedOperationError,
    connect,
)
from pydantic import BaseModel, Field

from catalog.core.base_judge import installed_judge, require_sanctioned_bases
from catalog.core.config import Settings, shared_lance_session
from catalog.core.store_endpoint import require_estate_store
from service_kit.lakehouse.features import BasePathRef, flags_from_open_error, manifest_base_path_refs, manifest_feature_flags, mixes_data_file_versions
from service_kit.lancekit.absence import reads_as_absent


log = logging.getLogger(__name__)


def build_namespace(settings: Settings) -> LanceNamespace:
    return connect(settings.impl, settings.namespace_properties())


def build_namespace_for_root(settings: Settings, root_uri: str, *, endpoint: str | None = None) -> LanceNamespace:
    """A namespace backend rooted at ``root_uri`` instead of the default ``settings.root`` (#3-A).

    Same impl, endpoint and CREDENTIALS as the default connection. ``endpoint`` is the warehouse
    record's ([[LH-067]]), and it is JUDGED here rather than applied ([[LH-205]]): the connection carries
    the estate's own key pair, so a record naming another store is refused before anything connects,
    or every open under that warehouse would sign toward a host a project admin chose. Every
    warehouse-rooted connection is built here, so this is the one place the refusal has to hold.

    Callers cache the result per (root, endpoint), so a corrected endpoint is judged again rather than
    served from the connection built before the correction.

    Raises:
        UnsupportedOperationError: ``endpoint`` names a store other than the estate's.
        ServiceUnavailableError: The estate's store did not answer the connection.
    """
    require_estate_store(endpoint, estate=settings.s3_endpoint, subject=f"the warehouse rooted at {root_uri!r}")
    target = settings.s3_endpoint
    try:
        return connect(settings.impl, settings.namespace_properties(root=root_uri))
    except ValueError as exc:
        # A STORE THAT WILL NOT ANSWER IS AN OUTAGE, NOT A BROKEN CATALOG. pylance raises a bare
        # `ValueError` for every construction failure, so an unreachable warehouse store surfaced as
        # `500 InternalError` — measured on the deployed catalog 2026-09-20, a warehouse pointed at a
        # dead endpoint — which sends whoever is on call to the wrong system. Same conversion, and the
        # same reasoning, as the missing-dataset case below.
        #
        # READ THE ERROR, never just the type: `LanceError(IO)` marks the transport failure, and a
        # construction failure WITHOUT it is not an outage (measured: a bogus impl says "No module
        # named 'no'"). Telling an operator to wait for a store to come back when the configuration
        # names a module that does not exist is the wrong answer delivered confidently.
        #
        # WHICH STORE reaches the LOG, not the caller, and that split is the estate's rule rather than
        # an oversight: `ns_errors` redacts every 5xx detail to "Internal Server Error" because those
        # are faults whose text leaks — and an endpoint is exactly the internal hostname that must not
        # go out on the wire. So the caller learns the store is unavailable (which is what 503 is FOR,
        # and what a 500 got wrong) and the operator gets the address from here.
        if "LanceError(IO)" not in str(exc):
            raise
        log.warning("warehouse_store_unreachable", extra={"root": root_uri, "endpoint": target, "error": str(exc)[:200]})
        raise ServiceUnavailableError(f"the object store for {root_uri!r} is unreachable at {target!r}") from exc


def _branch_checkout_error(
    dataset: lance.LanceDataset,
    table_id: list[str],
    branch: str,
    version: int | None,
    exc: Exception,
) -> Exception:
    """Classify a failed branch checkout, returning the exception the caller should raise.

    pylance reports both "no such branch" and "no such version ON that branch" as a bare
    ``ValueError``/``OSError`` whose text is a STORAGE PATH — ``Not found: <root>/tree/dev/_versions`` —
    so neither the class nor the message can be handed to a client: it answers 500 and publishes the
    bucket layout doing it. Discriminate by asking the dataset which branches exist (one extra read, on
    the failure path only) and mint the spec's own code: 22 ``TableBranchNotFound`` (404) for an unknown
    branch, ``TableVersionNotFound`` (404) when the branch is real but the pinned version is not.
    A failure we cannot attribute — including one where listing branches ALSO fails — stays itself, so a
    genuine object-store outage is still a 5xx rather than "your branch is wrong".
    """
    try:
        known = set(dataset.branches.list() or {})
    except Exception:  # noqa: BLE001 - cannot discriminate; the original failure stands on its own
        return exc
    if branch not in known:
        return TableBranchNotFoundError(f"branch {branch!r} not found on table {'.'.join(table_id)}")
    if version is not None:
        return TableVersionNotFoundError(f"version {version} not found on branch {branch!r} of table {'.'.join(table_id)}")
    return exc


def open_dataset(
    ns: LanceNamespace,
    storage_options: dict[str, str],
    table_id: list[str],
    *,
    version: int | None = None,
    branch: str | None = None,
) -> lance.LanceDataset:
    """Open the table's Lance dataset on the ref the request names, refusing one declaring a base nothing sanctioned.

    THE CHECKED OPEN ([[LH-279]]): the handle's own ``base_paths`` — the ref actually opened, after the
    version pin and the branch checkout, read in memory at no request — are judged by
    :func:`catalog.core.base_judge.require_sanctioned_bases`, which raises ``InvalidTableStateError``
    (code 19) for a base that is not the table's own, not configured and not recorded. A branch is judged
    as well as main: a base planted on ``tree/<b>`` is invisible from main (lh279 m7).

    Every door that reads or writes a table's ROWS opens through here. A door that reads or changes only
    its version and ref metadata — the repairs an operator needs on exactly such a table — opens through
    :func:`open_dataset_unchecked`. Both carry each data base's own ``base_store_params``
    (:func:`open_location`).

    Raises:
        InvalidTableStateError: The opened ref declares a base the catalog did not sanction.
    """
    location = _table_location(ns, table_id)
    dataset = _open_ref(location, storage_options, table_id, version=version, branch=branch)
    require_sanctioned_bases(location, manifest_base_path_refs(dataset), judge=None)
    return dataset


def open_dataset_unchecked(
    ns: LanceNamespace,
    storage_options: dict[str, str],
    table_id: list[str],
    *,
    version: int | None = None,
    branch: str | None = None,
) -> lance.LanceDataset:
    """:func:`open_dataset` WITHOUT the base judge — for a door that reads no row through the handle.

    Describing a version, listing versions, tags and branches, restoring and deleting them are how an
    operator sees and repairs a table whose manifest declares a base nobody sanctioned; refusing them
    would leave no way out but a drop. None of them reads a data file, so none of them reads through the
    base, and a door that does read rows must not call this.
    """
    return _open_ref(_table_location(ns, table_id), storage_options, table_id, version=version, branch=branch)


def judged_native_version(ns: LanceNamespace, storage_options: dict[str, str], table_id: list[str], *, version: int | None) -> int:
    """The version a NATIVE data op must be pinned to, after judging that version's bases ([[LH-279]]).

    ``DirectoryNamespace`` opens the dataset inside its own Rust call for ``query_table``,
    ``count_table_rows`` and the plan ops, so no Python handle exists to judge (lh279 m2 read the victim
    through each). This opens the ref the request names on the shared session — the version the request
    pinned, else the latest — judges it, and returns its number: the door then pins the native request
    to exactly the version judged, so a commit landing between the two cannot be served unjudged. The
    shared session also carries the manifest the native open reads next (lh279 m2: query 5 -> 4
    requests), so the pre-open costs about what it saves.

    A TABLE WITH A BASE UNDER ITS OWN CREDENTIAL IS REFUSED HERE ([[LH-273]]). The native open takes the
    namespace's storage options and no ``base_store_params``, so it would read that base with the
    estate's key: refused outright where the estate key has no grant on the base, and served under an
    identity nobody configured for it where the key does. The pylance-served doors — ``/changes`` and
    every door through :func:`open_dataset` — read the same table under each base's own key; the vend
    answers ``server_mediated`` for it, because an STS credential is the estate role's.

    Raises:
        InvalidTableStateError: That version declares a base the catalog did not sanction.
        UnsupportedOperationError: That version keeps data on a base under its own credential.
    """
    dataset = open_dataset(ns, storage_options, table_id, version=version)
    if base_store_params_for(dataset, storage_options) is not None:
        raise UnsupportedOperationError(
            f"table {'.'.join(table_id)} keeps data on a base opened under its own credential, which this door's native reader cannot "
            "carry; read it through the catalog's /changes door"
        )
    return int(dataset.version)


def base_store_params_for(dataset: lance.LanceDataset, storage_options: dict[str, str]) -> dict[str, dict[str, str]] | None:
    """The ``base_store_params`` the bases ``dataset``'s manifest declares need, or ``None`` when none needs its own.

    Keyed by each base's path exactly as the manifest states it, because pylance matches the key against
    that string and nothing else (measured, :mod:`catalog.services.base_credentials`). The credential
    references come from the installed judge (:func:`catalog.core.base_judge.installed_judge`), the same
    deployment configuration the bases are judged against; a process with no judge installed answers
    ``None``, and :func:`open_dataset` then refuses the foreign base before any row is read.
    """
    paths = [ref.path for ref in manifest_base_path_refs(dataset)]
    if not paths:
        return None
    try:
        credentials = installed_judge().credentials
    except ServiceUnavailableError:
        return None
    return credentials.store_params(paths, storage_options)


def open_location(location: str, storage_options: dict[str, str], *, version: int | None = None, branch: str | None = None) -> lance.LanceDataset:
    """Open the dataset at ``location`` on the ref named, with each declared base under its own credential ([[LH-273]]).

    A base's ``base_store_params`` are known only once its manifest is read, so the first open carries
    none: it reads the manifest from the table's own root and no data file. When a declared base lies
    under a referenced one, the dataset is opened again with the composed map, on the shared session
    that already holds that manifest. A branch is checked out from that handle, which carries the map on
    (measured on pylance 12.0.0: the checkout of a handle opened with a base's entry reads that base's
    rows, and the checkout of one opened without it is refused), and the map is composed from the
    BRANCH's manifest, whose base list is its own.

    Raises whatever pylance raises — a caller translates it (:func:`_open_ref`) or degrades on it
    (:func:`catalog.core.vending.dataset_facts`).
    """
    first = lance.dataset(location, storage_options=storage_options, version=None if branch else version, session=shared_lance_session())
    ref = first.checkout_version((branch, version)) if branch else first
    params = base_store_params_for(ref, storage_options)
    if params is None:
        return ref
    reopened = lance.dataset(
        location, storage_options=storage_options, version=None if branch else version, session=shared_lance_session(), base_store_params=params
    )
    return reopened.checkout_version((branch, version)) if branch else reopened


def _table_location(ns: LanceNamespace, table_id: list[str]) -> str:
    resp = ns.describe_table(DescribeTableRequest(id=list(table_id), with_table_uri=True))
    location = getattr(resp, "table_uri", None) or getattr(resp, "location", None)
    if not location:
        raise TableNotFoundError(f"Table not found: {table_id}")
    return str(location)


def _open_ref(location: str, storage_options: dict[str, str], table_id: list[str], *, version: int | None, branch: str | None) -> lance.LanceDataset:
    """:func:`open_location`, with pylance's bare failures translated into the spec's codes.

    A named branch is a whole parallel dataset under ``tree/{branch}/`` with its own version history, and
    ``lance.dataset(uri)`` opens ONLY main, so a branch is reached through
    ``checkout_version((branch, version))`` — pylance's own branch reference, the same tuple form
    ``dataplane._tag_reference`` uses — and the handle it returns is WRITABLE: schema evolution through
    it commits to the branch and leaves main at its version. ``version`` pins within whichever ref is
    selected (branch-local numbering when a branch is named).
    """
    if branch is None:
        try:
            return open_location(location, storage_options, version=version)
        except ValueError as exc:
            # pylance raises a bare ValueError for BOTH "no dataset here" and "no such version", and
            # letting either through answered 500 — which says "the catalog is broken" when the catalog
            # is fine. Measured live: a table registered at a retired warehouse 500'd every publish,
            # sending whoever was on call to the wrong system.
            #
            # The two are DIFFERENT facts and keep different codes: a missing version is
            # `TableVersionNotFoundError` (the table is fine, that version is not), a missing dataset
            # is `TableNotFoundError` — the same error this function raises when the registration names
            # no location at all, found one step later. The location rides the message, because knowing
            # the table is missing does not tell anyone which bucket to go and look at.
            message = str(exc).lower()
            if version is not None and "_versions/" in message and ".manifest" in message:
                raise TableVersionNotFoundError(f"table version {version} was not found") from exc
            raise TableNotFoundError(f"table has no readable dataset at its declared location ({location!r})") from exc
    main = lance.dataset(location, storage_options=storage_options, session=shared_lance_session())
    try:
        return open_location(location, storage_options, version=version, branch=branch)
    except (ValueError, OSError) as exc:
        error = _branch_checkout_error(main, table_id, branch, version, exc)
        if error is exc:
            raise
        raise error from exc


class RegisteredDataset(BaseModel):
    """What the register door judges on a dataset it attached, read from ONE open."""

    #: Reader flag 256 (mixed data file versions) is set.
    mixed: bool
    #: The dataset tracks stable row ids (`has_stable_row_ids`). Unread, and False, when the open itself
    #: was refused over flag 256, which the door refuses first.
    stable_row_ids: bool
    #: Every base the manifest declares. Empty when the open itself was refused over flag 256, which
    #: the door refuses before it looks at a base.
    bases: list[BasePathRef] = Field(default_factory=list)


def registered_dataset_facts(location: str, storage_options: dict[str, str]) -> RegisteredDataset | None:
    """The flags and declared bases of the dataset at ``location``; ``None`` when no dataset is there.

    A pylance that refuses the open over flag 256 answers ``mixed=True``, because 11 and older cannot
    open such a table and 12 opens it; either way the fact is the same. Any other failed open raises
    unchanged, so a caller that has to fail closed can.
    """
    try:
        dataset = lance.dataset(location, storage_options=storage_options, session=shared_lance_session())
    except (ValueError, OSError) as exc:
        refused = flags_from_open_error(exc)
        if refused is not None and mixes_data_file_versions(refused):
            return RegisteredDataset(mixed=True, stable_row_ids=False)
        if refused is None and reads_as_absent(exc):
            return None
        raise
    reader, _writer = manifest_feature_flags(dataset)
    return RegisteredDataset(mixed=mixes_data_file_versions(reader), stable_row_ids=dataset.has_stable_row_ids, bases=manifest_base_path_refs(dataset))


def warn_if_mixed_file_versions(location: str, storage_options: dict[str, str], *, table: str) -> None:
    """Log a WARN when a table the catalog re-registers carries reader flag 256. Never raises.

    An undrop restores a table the catalog already governed, so refusing it would strand the table in
    the trash with its bytes intact. The public register door refuses the same dataset instead.
    """
    try:
        facts = registered_dataset_facts(location, storage_options)
    except Exception as exc:  # noqa: BLE001 — a restore is never failed over a flag read
        log.warning("restored_table_flags_unreadable", extra={"table": table, "location": location, "error": str(exc)[:300]})
        return
    if facts is not None and facts.mixed:
        log.warning("restored_mixed_file_versions", extra={"table": table, "location": location})
