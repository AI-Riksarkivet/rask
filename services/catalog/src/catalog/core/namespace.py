"""Backend namespace construction and dataset resolution.

The REST server is an adapter over a native ``LanceNamespace`` backend
(``DirectoryNamespace`` on MinIO/S3 by default). ``open_dataset`` resolves a
table's storage location via the namespace and opens it with pylance — used by
the data-plane service for operations the native backend does not implement.
"""

from __future__ import annotations

import lance
from lance_namespace import (
    DescribeTableRequest,
    LanceNamespace,
    TableBranchNotFoundError,
    TableNotFoundError,
    TableVersionNotFoundError,
    connect,
)

from catalog.core.config import Settings


def build_namespace(settings: Settings) -> LanceNamespace:
    return connect(settings.impl, settings.namespace_properties())


def build_namespace_for_root(settings: Settings, root_uri: str) -> LanceNamespace:
    """A namespace backend rooted at ``root_uri`` instead of the default ``settings.root`` (#3-A).

    Same impl + object-store credentials/endpoint as the default connection — only the ``root`` (the S3
    bucket) differs, since a warehouse is physically a separate bucket and the creds are bucket-agnostic on
    the S3 target. Callers cache the result per root (a warehouse's root never changes)."""
    properties = settings.namespace_properties()
    properties["root"] = root_uri
    return connect(settings.impl, properties)


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
    """Open the table's Lance dataset — on ``branch`` when the request names one, otherwise on main.

    A named branch is a whole parallel dataset under ``tree/{branch}/`` with its own version history, and
    ``lance.dataset(uri)`` opens ONLY main — so a request carrying ``branch`` used to be answered by a write
    to main (verified live: ``add_columns`` with ``branch=dev`` returned 200 with the column on main and
    ``dev`` untouched). ``checkout_version((branch, version))`` is pylance's own branch reference — the same
    tuple form ``dataplane._tag_reference`` uses — and the handle it returns is WRITABLE: schema evolution
    through it commits to the branch and leaves main at its version. ``version`` pins within whichever ref
    is selected (branch-local numbering when a branch is named).
    """
    resp = ns.describe_table(DescribeTableRequest(id=list(table_id), with_table_uri=True))
    location = getattr(resp, "table_uri", None) or getattr(resp, "location", None)
    if not location:
        raise TableNotFoundError(f"Table not found: {table_id}")
    if branch is None:
        return lance.dataset(location, storage_options=storage_options, version=version)
    dataset = lance.dataset(location, storage_options=storage_options)
    try:
        return dataset.checkout_version((branch, version))
    except (ValueError, OSError) as exc:
        error = _branch_checkout_error(dataset, table_id, branch, version, exc)
        if error is exc:
            raise
        raise error from exc
