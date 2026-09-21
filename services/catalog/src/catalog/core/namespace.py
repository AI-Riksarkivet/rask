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
    connect,
)

from catalog.core.config import Settings, shared_lance_session


log = logging.getLogger(__name__)


def build_namespace(settings: Settings) -> LanceNamespace:
    return connect(settings.impl, settings.namespace_properties())


def build_namespace_for_root(settings: Settings, root_uri: str, *, endpoint: str | None = None) -> LanceNamespace:
    """A namespace backend rooted at ``root_uri`` instead of the default ``settings.root`` (#3-A).

    Same impl and object-store CREDENTIALS as the default connection; ``endpoint`` overrides the target
    when the warehouse record names one ([[LH-067]]). Without it a warehouse is reachable only at the
    estate's single endpoint — fine while every bucket lives in one store, and the reason a second one
    could not be registered at all.

    ``allow_http`` IS RE-DERIVED from whichever endpoint is actually used, and that is the half worth
    stating: the property is a function of the scheme, so leaving it on the estate's default gives an
    ``http://`` warehouse behind an ``https://`` estate ``allow_http=false``, and every open fails with
    a TLS error that names the store rather than the configuration.

    CREDENTIALS ARE NOT OVERRIDABLE HERE, deliberately. Material never travels in a warehouse record —
    the estate resolves its S3 secret from the Dapr secret store, and a second store's material belongs
    behind that same door under its own key, with the record naming a reference rather than carrying a
    key pair. An endpoint is not a secret, and it is what a second store needs first.

    Callers cache the result per (root, endpoint) — a warehouse's root never changes, but its endpoint
    is caller-owned and may be corrected."""
    target = endpoint or settings.s3_endpoint
    try:
        return connect(settings.impl, settings.namespace_properties(root=root_uri, endpoint=endpoint))
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
    base_store_params: dict[str, dict[str, str]] | None = None,
) -> lance.LanceDataset:
    """Open the table's Lance dataset — on ``branch`` when the request names one, otherwise on main.

    A named branch is a whole parallel dataset under ``tree/{branch}/`` with its own version history, and
    ``lance.dataset(uri)`` opens ONLY main — so a request carrying ``branch`` used to be answered by a write
    to main (verified live: ``add_columns`` with ``branch=dev`` returned 200 with the column on main and
    ``dev`` untouched). ``checkout_version((branch, version))`` is pylance's own branch reference — the same
    tuple form ``dataplane._tag_reference`` uses — and the handle it returns is WRITABLE: schema evolution
    through it commits to the branch and leaves main at its version. ``version`` pins within whichever ref
    is selected (branch-local numbering when a branch is named).

    ``base_store_params`` carries the object-store options for a registered data base needing DIFFERENT
    credentials or endpoint from the estate's ([[LH-067]]). Without it, a base written with its own
    credentials is unreadable — `dataplane._write_blob` composes these on the write side and records
    that consequence in its own comment, which is the worst shape a storage bug takes: the write
    succeeds and the data is unreachable later by a component that looks correct.

    Keyed by BASE PATH URI and **runtime-only** — pylance 11.0.0 states these are "not persisted to the
    manifest", which is what makes this the only form a credential may take here, as against the
    `base_<id>.<key>` spelling in ``storage_options`` which carries no such guarantee. ``None`` is
    byte-identical to passing nothing: pylance falls back to the top-level options for any base with no
    entry.
    """
    resp = ns.describe_table(DescribeTableRequest(id=list(table_id), with_table_uri=True))
    location = getattr(resp, "table_uri", None) or getattr(resp, "location", None)
    if not location:
        raise TableNotFoundError(f"Table not found: {table_id}")
    if branch is None:
        try:
            return lance.dataset(
                location,
                storage_options=storage_options,
                version=version,
                session=shared_lance_session(),
                base_store_params=base_store_params,
            )
        except ValueError as exc:
            # pylance raises a bare ValueError for BOTH "no dataset here" and "no such version", and
            # letting either through produced a 500 — which says "the catalog is broken" when the
            # catalog is fine. Measured live: a table registered at a retired warehouse 500'd every
            # publish, sending whoever was on call to the wrong system.
            #
            # The two are DIFFERENT facts and keep different codes: a missing version is
            # `TableVersionNotFoundError` (the table is fine, that version is not), a missing dataset
            # is `TableNotFoundError` — the same error this function already raises when the
            # registration names no location at all, found one step later.
            #
            # The location rides the message, because knowing the table is missing does not tell
            # anyone which bucket to go and look at.
            # Same wording and same split as `dataplane.py`, which had this conversion at ONE caller
            # while every other caller — `publish` among them — leaked the ValueError as a 500. Doing
            # it here covers them all; the message is kept identical so nothing that already depended
            # on it moves.
            message = str(exc).lower()
            if version is not None and "_versions/" in message and ".manifest" in message:
                raise TableVersionNotFoundError(f"table version {version} was not found") from exc
            raise TableNotFoundError(f"table has no readable dataset at its declared location ({location!r})") from exc
    # BOTH LEGS, because a branch read opens the dataset before checking out — forwarding on only the
    # main leg would leave branch reads of a foreign-credentialled base failing exactly as before.
    dataset = lance.dataset(location, storage_options=storage_options, session=shared_lance_session(), base_store_params=base_store_params)
    try:
        return dataset.checkout_version((branch, version))
    except (ValueError, OSError) as exc:
        error = _branch_checkout_error(dataset, table_id, branch, version, exc)
        if error is exc:
            raise
        raise error from exc
