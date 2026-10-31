"""Credential vending — hand an authorized client scoped ``storage_options`` for DIRECT object I/O.

Track B: instead of every byte flowing through the catalog (Mode B, the server-mediated Arrow-IPC path),
an authorized caller can request short-TTL, per-table, tier-scoped credentials and read/write the Lance
data on object storage itself (LanceDB SDK / lance-ray / pylance). The vendor plug is chosen at boot
(``LANCE_VENDING_MODE``): ``web_identity`` (the catalog's own token exchanged for a session narrowed by a
per-table session policy, ADR 0170) or ``mode_b`` (vends nothing → the client uses the data endpoints).

Authz: the router-level :func:`catalog.api.fga_deps.authorize` decides before this door runs. A read-tier
vend needs ``can_read_data`` (or, for a maintainer, ``can_maintain``); a writing tier is a write, so the
tier judge asks the rung that tier opens (``fga_deps.VEND_TIER_RUNGS``, ADR 0160). Each tier's session
policy then grants at the object store only what that rung may change (``vending.build_session_policy``,
[[LH-202]]).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    TableBranchNotFoundError,
)

from catalog.api.dependencies import DoorTableDep, FgaClientDep, SettingsDep, VendorDep
from catalog.api.security import CurrentSubject, CurrentToken
from catalog.core.base_judge import BaseJudge
from catalog.core.vending import Tier, dataset_facts, require_vendable_bases, role_session_name, table_has_branch, unsanctioned_bases, vend_or_refuse
from catalog.schemas import CredentialResponse, VendedCredentials
from service_kit.governed import fga
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.lakehouse.version_retention import recorded_branch


log = logging.getLogger(__name__)


#: THE MANAGEMENT SURFACE ([[LH-021]]). These are rask's own operations, not Lance namespace ones —
#: a spec client discovering them on `/v1/table` meets verbs the document never defines. They mount at
#: `/management/v1` and inherit the same three rungs from `api/v1/router.py`.
router = APIRouter(prefix="/management/v1/table", tags=["credentials"])


@router.post("/{id}/credentials", response_model_exclude_none=True)
async def vend_credentials(
    id: str,
    table: DoorTableDep,
    settings: SettingsDep,
    token: CurrentToken,
    subject: CurrentSubject,
    client: FgaClientDep,
    vendor: VendorDep,
    tier: Annotated[
        Tier,
        Query(
            description="`read` (can_read_data), `write` (can_write_data: read the table, put data, transaction and deletion files and a staged manifest for CreateTableVersion to finalize) or `maintain` (can_vend_maintain, the maintenance identity's alone: get, put and delete the whole prefix)."
        ),
    ] = "read",
    # [[LH-055]] WHICH branch this credential is for. Naming one narrows the grant rather than widening
    # it — writes land under that branch's own `<table>/tree/<branch>/` (its `data/` at the write tier, its
    # file directories at the maintain tier) and main drops to read — which is the isolation the LanceDB blog's
    # branching post (https://www.lancedb.com/blog/branching-and-shallow-clone) says the `tree/` layout exists to give:
    # "Storage-level access controls can enforce read-only on the main dataset and write-only on the branch directory".
    # Absent = main, exactly as before.
    branch: Annotated[
        str,
        Query(
            description="The branch this credential is for. Naming one NARROWS the grant: writes land under that branch's own `<table>/tree/<branch>/` (`data/` at the write tier; `_versions`, `_transactions`, `_deletions`, `_indices` and `data` at the maintain tier) and main drops to read-only. Omit for main."
        ),
    ] = "",
) -> CredentialResponse:
    """Vend scoped ``storage_options`` for direct object-store access to this table at ``tier``.

    The credential is a session the catalog's own token is exchanged for, named at the store for the verified caller
    it is vended to; the caller's own token never leaves the catalog.

    It is scoped to the location the door resolved, never to the name, and answered only while that location still
    holds the table authorized: a credential for a location is a grant on whatever table holds it ([[LH-530]]).
    """
    location = table.location
    # `main` is the table itself, which no branch list names; the credential for it is main's.
    branch = recorded_branch(branch) or ""
    try:
        # The client-direct write target + optimistic-commit base version (a declared-only/new table reads as 0).
        # A tiny manifest read under the catalog's own credential to learn the version — not the byte-proxy (no data bytes move).
        # THE BRANCH MUST BE ONE THE TABLE HAS ([[LH-056]]), checked before anything is minted. `branch` is
        # caller-chosen and `build_session_policy` guards only `*`, `?` and `..` — the format allows `/`
        # inside a branch name, so a shape rule cannot answer this and a lookup has to. Unchecked, the grant
        # is a 900 s write credential for a prefix no manifest references: storage charged to a table for
        # objects nothing will read, under a name somebody may create later. Creating a branch needs no
        # credential, so there is no flow that must vend for one before it exists.
        if branch and not await run_in_threadpool(table_has_branch, location, settings.storage_options(), branch):
            raise TableBranchNotFoundError(f"branch {branch!r} not found on this table")
        facts = await run_in_threadpool(partial(dataset_facts, location, settings.storage_options(), branch=branch))
        # [[LH-279]] A BASE NOTHING SANCTIONED REFUSES THE VEND, before the classification answer below can
        # route it anywhere: a writer can plant any root in its manifest, and a credential granting READ on
        # it — or a server-mediated read through it — hands over another table's bytes. 409, not a fallback.
        bases = await run_in_threadpool(require_vendable_bases, location, facts, BaseJudge.from_settings(settings))
    except Exception:
        await run_in_threadpool(table.require_held)
        raise
    await run_in_threadpool(table.require_held)
    # [[LH-058]] A CLASSIFIED COLUMN MAKES A TABLE UNVENDABLE RAW, and it is a property of the TABLE
    # rather than of the caller — the same shape as the unsanctioned base below, for a reason that was
    # measured rather than chosen.
    #
    # A vended credential's unit is an OBJECT; Lance's field-to-file mapping is write-order dependent.
    # Measured on pylance 2026-09-22: two columns written together share ONE data file (`field ids:
    # [0, 1]`), while a column added later gets its own. So whether a classified column's bytes are
    # separable at all is an accident of the table's write history, and NO session policy can express
    # "this prefix except that column". The row's open choice was "refuse or narrow"; narrowing is not
    # expressible, which settles it.
    #
    # `server_mediated`, never a 403: the Arrow-IPC data endpoints CAN project, so this routes the
    # caller to the one path where a column-level rule can ever be applied instead of denying a read
    # they may well be entitled to. That is the door's existing answer for "a direct credential would
    # be wrong here", and reusing it keeps one behaviour rather than two.
    #
    # WHAT THIS DOES NOT YET DO, stated so it is not mistaken for more: the server-mediated path does
    # not mask either. This stops the raw bytes leaving under a 900 s credential the caller holds,
    # which is the precondition for masking rather than masking itself.
    if facts.classified:
        log.info("vend_server_mediated_classified_columns", extra={"location": location, "columns": list(facts.classified)})
        return CredentialResponse(mode="server_mediated")
    # #3-B ⊥ #2, NARROWED to the bases the policy would actually miss ([[LH-057]]). Every base here is
    # already one the catalog sanctioned (above); what remains is whether the session policy can ADDRESS
    # it — `build_session_policy` grants each allowlisted base a `ListBase<n>`/`BaseObjects<n>` pair, so the
    # client reaches its own bytes. A sanctioned base the policy cannot grant is reachable only through
    # the catalog's own credential, which is what the server-mediated answer routes the caller to.
    if missed := unsanctioned_bases(location, bases.bases, settings.multibase_data_base_list):
        log.info("vend_server_mediated_unreachable_bases", extra={"location": location, "bases": list(missed)})
        return CredentialResponse(mode="server_mediated")
    # The blocking STS exchange runs in the threadpool; a refused or unreachable exchange answers 503
    # (`vend_or_refuse`), never a bare 500.
    vended = await run_in_threadpool(
        partial(
            vend_or_refuse,
            vendor,
            table_location=location,
            tier=tier,
            session_name=role_session_name(subject),
            bases=bases.bases,
            data_directories=bases.data_directories,
            branch=branch,
        )
    )
    # The secret and the session token leave the catalog in this body and nowhere else ([[LH-072]]), built before the
    # issuance is recorded: a vend whose options the builder refuses was never handed over.
    creds = None if vended is None else VendedCredentials(storage_options=vended.as_storage_options().in_clear(), expires_at_millis=vended.expires_at_millis)
    mode = "server_mediated" if creds is None else "direct"
    if mode == "direct":  # #41 audit the actual issuance of direct object-store credentials (who, what, tier)
        obj = fga.table_object(table.resolved.require_uuid())
        audit(
            AuditAction.VEND_CREDENTIALS,
            AuditOutcome.SUCCESS,
            subject=token.subject if token is not None else None,
            resource=obj,
            tier=tier,
            door="credentials",
        )
    return CredentialResponse(mode=mode, credentials=creds, location=location, read_version=facts.read_version)
