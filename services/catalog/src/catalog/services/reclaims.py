"""The catalog's side of every reclaim (ADR 0246): each batch a reclaimer asks for, run here under the catalog's own
identity, the only one that writes a table's condemned versions or a pin (:mod:`lakehouse.condemned_versions`), and the
credential the catalog's own reclaims delete under.

A batch condemns, reads the pins, then reads the policy that governs the table (:meth:`Governance.held_by`), then
promotes. The policy is read AFTER the condemn and the levels it is read under are resolved then too, from the table's
location claim as it stands (PolicyAfterCondemn): a policy door, a rename into a namespace that holds among them, writes,
then reads the condemned versions, so of the door and the batch at least one sees the other. A record that cannot be
read keeps the table whole (``maintenance_policies.governing_policy``): fail closed. An erasure's reclaims read no
maintenance hold (``holds=False``): which of an erasure and a retention hold wins is the owner's open question, and this
design leaves it as it was (ADR 0246 §14).

Every function is blocking IO; callers threadpool it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Final

import lance
from lance_namespace import ConcurrentModificationError, LanceNamespace, ServiceUnavailableError, TableBranchNotFoundError
from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from catalog.core.base_judge import BaseJudge
from catalog.core.config import Settings
from catalog.core.vending import dataset_facts, require_vendable_bases, role_session_name, vend_or_refuse, vendor_for
from catalog.services import object_uuids, table_claims
from lakehouse import condemned_versions, location_claims, maintenance_policies, records
from lakehouse.condemned_versions import Credential, RefReclaim, Verdict
from lakehouse.naming import CATALOG_DELIMITER
from lakehouse.storage_options import StorageOptions
from lakehouse.version_retention import recorded_branch


log = logging.getLogger(__name__)

#: What one batch is asked: the versions to condemn, and whether a version a branch is cut from may be among them.
type Condemn = Callable[[list[int]], Verdict]

#: The name the catalog's own reclaim sessions carry at the store, whose trail records it (:func:`role_session_name`).
_SESSION_NAME: Final = role_session_name("catalog-reclaim")

#: The most condemned versions a policy door names; past it the answer counts the rest (``condemned_beyond``).
CONDEMNED_SHOWN: Final = 100


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
    listed_on: str,
    versions: Sequence[int],
    forks: bool = False,
    holds: bool = True,
) -> Verdict:
    """One batch of ``ref``'s reclaim, run to its verdict (:class:`lakehouse.condemned_versions.Batch`): ``versions``
    condemned with whatever a dead batch or ``version/delete`` left on the ref, the pins read, the policy read where
    ``holds`` asks for it (an erasure's reclaims do not), the entries promoted. ``main`` is a handle of the table's main,
    under the catalog's own credential. ``listed_on`` is the ref's identity as the reclaimer read it before it opened the
    handle it listed ``versions`` on (:func:`lakehouse.condemned_versions.identity_of`).

    Raises:
        TableBranchNotFoundError: ``ref`` is a branch the table does not have (code 22).
        ConcurrentModificationError: ``ref`` is now another incarnation than ``listed_on``, or another registration than
            the one ``governance`` names holds the location (code 14, ListedOn): the reclaimer lists its versions again
            and asks again; nothing was condemned.
        ServiceUnavailableError: The store did not answer, the record does not parse, or every rewrite of it lost to
            another writer (contention among batches, never a conflict the caller made); nothing is licensed (503).
    """
    try:
        run = condemned_versions.Batch(
            control_root,
            storage_options,
            table_uuid=governance.table_uuid,
            location=governance.location,
            dataset=main,
            ref=ref,
            listed_on=listed_on,
            registered=partial(condemned_versions.registration_at, governance.claims, governance.location),
        )
        run.condemn(versions, forks=forks)
        run.read_pins()
        verdict = run.promote(governance.held_by() if holds else None)
    except condemned_versions.RefGoneError as exc:
        raise TableBranchNotFoundError(str(exc)) from exc
    except condemned_versions.ListedElsewhereError as exc:
        raise ConcurrentModificationError(f"{exc}; nothing was condemned") from exc
    except records.RecordChangedError as exc:
        raise ServiceUnavailableError(f"the table's condemned versions kept changing under this batch; nothing was licensed, retry: {exc}") from exc
    except OSError as exc:
        raise ServiceUnavailableError(f"the table's condemned versions or refs could not be read, so nothing was licensed: {exc}"[:600]) from exc
    log.info(
        "reclaim_batch",
        extra={"table_uuid": governance.table_uuid, "ref": ref, "asked": list(versions)[:20], "go": verdict.versions[:20], "held_by": verdict.held_by},
    )
    return verdict


def minter(settings: Settings, location: str, ref: str | None, main: lance.LanceDataset) -> Callable[[], Credential]:
    """How a reclaim the catalog runs in process mints each batch's credential (:func:`condemned_versions.reclaim`): a
    maintain-tier session for the table, minted for the batch through the catalog's web identity before the batch is
    asked and lasting at most :data:`condemned_versions.CREDENTIAL_LIFETIME`, with the grants the maintain vend gives a pass
    of the ref (none on the control root), and the ref opened under it, so a reclaim frozen past its licence's instant
    deletes nothing more. Where the catalog has no web identity (``LANCE_VENDING_MODE=mode_b``, the development fixtures on
    a key pair) nothing is minted: the batch deletes through the ref opened from ``main``, under the catalog's own key
    pair, which no store expires (the residual of ADR 0246 §14).

    Raises:
        ServiceUnavailableError: The store refused, or could not answer, the exchange or a read of the ref (503).
        InvalidTableStateError: A base the catalog did not sanction (409).
    """
    branch = recorded_branch(ref) or ""

    def own() -> Credential:
        return Credential(dataset=main if branch == "" else main.checkout_version((branch, None)), expires_at=None)

    if settings.vending_mode != "web_identity":
        return own
    vendor = vendor_for(settings)
    facts = dataset_facts(location, settings.storage_options(), branch=branch)
    bases = require_vendable_bases(location, facts, BaseJudge.from_settings(settings))

    def mint() -> Credential:
        vended = vend_or_refuse(
            vendor,
            table_location=location,
            tier="maintain",
            session_name=_SESSION_NAME,
            bases=bases.bases,
            data_directories=bases.data_directories,
            branch=branch,
            at_most_seconds=int(condemned_versions.CREDENTIAL_LIFETIME.total_seconds()),
        )
        if vended is None:
            return own()
        opened = lance.dataset(location, storage_options=vended.as_storage_options().in_clear())
        return Credential(
            dataset=opened if branch == "" else opened.checkout_version((branch, None)),
            expires_at=datetime.fromtimestamp(vended.expires_at_millis / 1000, UTC),
        )

    return mint


def in_process(governance: Governance, settings: Settings, main: lance.LanceDataset, *, ref: str | None, forks: bool = False, holds: bool = True) -> RefReclaim:
    """How a reclaim of ``ref`` the catalog runs in process (``maintenance/run``, an erasure's verification and its
    release) asks for each batch and mints each batch's credential. The ref's identity is read HERE, and every batch names
    it (ListedOn): a caller asks for this before it opens the handle it lists the ref's victims on.

    Raises:
        TableBranchNotFoundError: ``ref`` is a branch the table does not have (code 22).
        ServiceUnavailableError: The store refused, or could not answer, a read of the ref or the exchange (503).
        InvalidTableStateError: A base the catalog did not sanction (409).
    """
    try:
        listed_on = condemned_versions.identity_of(main, ref, table_uuid=governance.table_uuid)
    except condemned_versions.RefGoneError as exc:
        raise TableBranchNotFoundError(str(exc)) from exc
    return RefReclaim(
        condemn=lambda versions: batch(
            governance, settings.registry_root, settings.storage_options(), main, ref=ref, listed_on=listed_on, versions=versions, forks=forks, holds=holds
        ),
        mint=minter(settings, governance.location, ref, main),
    )


def erasure_reclaims(governance: Governance, settings: Settings, main: lance.LanceDataset) -> Callable[[str | None], RefReclaim]:
    """For each ref of an erasure's reclaim, how it asks for each batch and mints each batch's credential, asked for
    before the ref's handle is opened (:func:`in_process`): a version a branch is cut from among what it may condemn,
    since the erasure takes the files only that version names (ADR 0243 R1), and no maintenance hold read
    (``holds=False``, ADR 0246 §14)."""
    return lambda ref: in_process(governance, settings, main, ref=ref, forks=True, holds=False)


class Found(BaseModel):
    """What a policy door finds condemned beneath it, read after its write."""

    #: ``(table, branch, branch identifier, version)`` of each version found, by table, branch and version; the identifier
    #: names the incarnation of the branch (``None`` for main), since a branch made again under its name repeats numbers.
    named: list[tuple[str, str | None, str | None, int]] = Field(default_factory=list)
    #: What could not be read: each a record (or the listing of them) that may name a version beneath the door.
    unread: list[str] = Field(default_factory=list)


def condemned_versions_of(
    registry_root: str,
    storage_options: StorageOptions,
    claims: location_claims.ClaimStore,
    *,
    location: str | None = None,
    within: Callable[[list[str]], bool] | None = None,
) -> Found:
    """Every condemned version of the table at ``location`` (one read of its record, whichever registration of the
    location wrote it: main's versions there are the same bytes under every one), or of every table whose name now
    ``within`` takes (one listing of the records standing, then a read of each, each record's table named by the claim
    its location holds now; a location no table holds left out): what a policy's answer does not promise (ADR 0246). A
    record stands only while its table has a reclaim in flight or stuck, so a door reads those and no other. What cannot
    be read is named in :attr:`Found.unread`, never raised: the door's write has landed.
    """
    if location is not None:
        try:
            found, _etag = condemned_versions.read_record(registry_root, storage_options, location)
        except OSError as exc:
            log.warning("condemned_record_unread", extra={"location": location, "error": str(exc)[:300]})
            return Found(unread=[condemned_versions.record_key(location)])
        standing = condemned_versions.Standing(records=[found] if found is not None else [])
    else:
        try:
            standing = condemned_versions.every_record(registry_root, storage_options)
        except OSError as exc:
            log.warning("condemned_records_unlisted", extra={"error": str(exc)[:300]})
            return Found(unread=["the listing of the condemned records"])
    named: set[tuple[str, str | None, str | None, int]] = set()
    unread = list(standing.unread)
    for record in standing.records:
        if not record.entries:
            continue
        try:
            # The table holding the location now, whichever registration it is.
            held = location_claims.holder(claims, record.location)
        except Exception as exc:  # noqa: BLE001 — a claim that cannot be read may name a table beneath the door: named, not raised
            log.warning("condemned_record_claim_unread", extra={"location": record.location, "error": f"{type(exc).__name__}: {exc}"[:300]})
            unread.append(condemned_versions.record_key(record.location))
            continue
        if held is None or (within is not None and not within(held.table.split(CATALOG_DELIMITER))):
            continue
        named.update((held.table, entry.ref, None if entry.ref is None else entry.ref_id, entry.version) for entry in record.entries)
    return Found(named=sorted(named, key=_ordered), unread=unread)


def _ordered(found: tuple[str, str | None, str | None, int]) -> tuple[str, str, str, int]:
    """A condemned version's place in a policy door's answer: by table, branch (main first) and version."""
    return found[0], found[1] or "", found[2] or "", found[3]
