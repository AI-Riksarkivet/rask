"""The catalog's side of every reclaim (ADR 0246): each batch a reclaimer asks for, run here under the catalog's own
identity, the only one that writes a table's condemned versions or a pin (:mod:`lakehouse.condemned_versions`).

A batch condemns, reads the pins, then reads the policy that governs the table (:meth:`Governance.held_by`), then
promotes. The policy is read AFTER the condemn and the levels it is read under are resolved then too, from the table's
location claim as it stands (PolicyAfterCondemn): a policy door, a rename into a namespace that holds among them, writes,
then reads the condemned versions, so of the door and the batch at least one sees the other. A record that cannot be
read keeps the table whole (``maintenance_policies.governing_policy``): fail closed.

Every function is blocking IO; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import lance
from lance_namespace import LanceNamespace, ServiceUnavailableError, TableBranchNotFoundError
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.services import object_uuids, table_claims
from lakehouse import condemned_versions, location_claims, maintenance_policies, records
from lakehouse.condemned_versions import Verdict
from lakehouse.naming import CATALOG_DELIMITER
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: What one batch is asked: the versions to condemn, and whether a version a branch is cut from may be among them.
type Condemn = Callable[[list[int]], Verdict]


class Governance(BaseModel):
    """Where a batch reads the policy that governs its table: the registry, the table's claim, and the connection its
    namespaces are named through."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    registry_root: str
    storage_options: StorageOptions
    claims: location_claims.ClaimStore
    location: str
    table_uuid: str
    #: The table's warehouse's connection, as the door holds it (a fenced one among them): what its namespaces are named by.
    namespace: SkipValidation[LanceNamespace]

    @classmethod
    def of(cls, table: table_claims.DoorTable, registry_root: str, storage_options: StorageOptions) -> Governance:
        """A door's table's governance."""
        return cls(
            registry_root=registry_root,
            storage_options=storage_options,
            claims=table.claims,
            location=table.location,
            table_uuid=table.resolved.require_uuid(),
            namespace=table.namespace,
        )

    def held_by(self) -> str | None:
        """What keeps the table's whole history, read now; ``None`` where nothing does. The levels are the namespaces of
        the name its location's claim holds now. A claim that no longer names the table, a record that cannot be read
        and one that is no policy all keep it whole: any of them may be a legal hold."""
        try:
            claim = location_claims.holder(self.claims, self.location)
            if claim is None or claim.uuid != self.table_uuid:
                return "the table no longer holds its location, so the policy that governs it is unknown"
            segments = list(claim.segments)
            uuids = [found for depth in range(1, len(segments)) if (found := object_uuids.resolve_namespace_uuid(self.namespace, segments[:depth])) is not None]
            policy = maintenance_policies.governing_policy(
                self.registry_root,
                self.storage_options,
                claims_root=self.registry_root,
                location=self.location,
                table_uuid=self.table_uuid,
                namespace_uuids=uuids,
            )
            if not maintenance_policies.keeps_whole_history(policy):
                return None
        except maintenance_policies.MalformedPolicyError as exc:
            log.warning("reclaim_policy_malformed", extra={"table_uuid": self.table_uuid, "error": str(exc)[:300]})
            return "a maintenance policy that may govern the table cannot be read as one"
        except Exception as exc:  # noqa: BLE001 — every read that fails keeps the table whole, logged
            log.warning("reclaim_policy_unread", extra={"table_uuid": self.table_uuid, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return "the maintenance policies that may govern the table could not be read"
        held = policy or {}
        return str(held.get("held_by") or f"{held.get('kind')}:{held.get('id')}")


def batch(
    governance: Governance,
    control_root: str,
    storage_options: StorageOptions,
    main: lance.LanceDataset,
    *,
    ref: str | None,
    versions: Sequence[int],
    forks: bool = False,
) -> Verdict:
    """One batch of ``ref``'s reclaim, run to its verdict (:class:`lakehouse.condemned_versions.Batch`): ``versions``
    condemned with whatever a dead batch or ``version/delete`` left on the ref, the pins read, the policy read, the
    entries promoted. ``main`` is a handle of the table's main, under the catalog's own credential.

    Raises:
        TableBranchNotFoundError: ``ref`` is a branch the table does not have (code 22).
        ServiceUnavailableError: The store did not answer, the record does not parse, or every rewrite of it lost to
            another writer (contention among batches, never a conflict the caller made); nothing is licensed (503).
    """
    try:
        run = condemned_versions.Batch(control_root, storage_options, table_uuid=governance.table_uuid, location=governance.location, dataset=main, ref=ref)
        run.condemn(versions, forks=forks)
        run.read_pins()
        verdict = run.promote(governance.held_by())
    except condemned_versions.RefGoneError as exc:
        raise TableBranchNotFoundError(str(exc)) from exc
    except records.RecordChangedError as exc:
        raise ServiceUnavailableError(f"the table's condemned versions kept changing under this batch; nothing was licensed, retry: {exc}") from exc
    except OSError as exc:
        raise ServiceUnavailableError(f"the table's condemned versions or refs could not be read, so nothing was licensed: {exc}"[:600]) from exc
    log.info(
        "reclaim_batch",
        extra={"table_uuid": governance.table_uuid, "ref": ref, "asked": list(versions)[:20], "go": verdict.versions[:20], "held_by": verdict.held_by},
    )
    return verdict


def condemner(
    governance: Governance, control_root: str, storage_options: StorageOptions, main: lance.LanceDataset, *, ref: str | None, forks: bool = False
) -> Condemn:
    """How an in-process reclaim of ``ref`` asks for each batch (:func:`lakehouse.condemned_versions.reclaim`)."""
    return lambda versions: batch(governance, control_root, storage_options, main, ref=ref, versions=versions, forks=forks)


def condemners(governance: Governance, control_root: str, storage_options: StorageOptions, main: lance.LanceDataset) -> Callable[[str | None], Condemn]:
    """For each ref of an erasure's reclaim, how it asks for each batch: a version a branch is cut from among what it may
    condemn, since the erasure takes the files only that version names (ADR 0243 R1)."""
    return lambda ref: condemner(governance, control_root, storage_options, main, ref=ref, forks=True)


def condemned_versions_of(
    registry_root: str,
    storage_options: StorageOptions,
    claims: location_claims.ClaimStore,
    *,
    table_uuid: str | None = None,
    within: Callable[[list[str]], bool] | None = None,
) -> list[tuple[str, str | None, int]]:
    """``(table, branch, version)`` of every condemned version of the table ``table_uuid``, or of every table whose name
    now ``within`` takes (each record's table named by its location's claim; a gone table's left out): what a policy's
    answer does not promise (ADR 0246).

    Raises:
        OSError: The store did not answer, or a record does not parse.
    """
    if table_uuid is not None:
        found, _etag = condemned_versions.read_record(registry_root, storage_options, table_uuid)
        held = [found] if found is not None else []
    else:
        held = condemned_versions.every_record(registry_root, storage_options)
    named: list[tuple[str, str | None, int]] = []
    for record in held:
        if not record.entries or (name := table_claims.current_name(claims, record.location, record.uuid)) is None:
            continue
        if within is not None and not within(name.split(CATALOG_DELIMITER)):
            continue
        named.extend((name, entry.ref, entry.version) for entry in record.entries)
    return sorted(named, key=lambda found: (found[0], found[1] or "", found[2]))
