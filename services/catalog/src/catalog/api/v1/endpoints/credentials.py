"""Credential vending — hand an authorized client scoped ``storage_options`` for DIRECT object I/O.

Track B: instead of every byte flowing through the catalog (Mode B, the server-mediated Arrow-IPC path),
an authorized caller can request short-TTL, per-table, tier-scoped credentials and read/write the Lance
data on object storage itself (LanceDB SDK / lance-ray / pylance). The vendor plug is chosen at boot
(``LANCE_VENDING_MODE``): ``sts`` (AssumeRole + a per-table session policy — the recommended path),
``static`` (per-bucket keys), or ``mode_b`` (vends nothing → the client uses the data endpoints).

Authz: the router-level :func:`catalog.api.fga_deps.authorize` already required ``can_read_data`` (or, for
a maintainer, ``can_maintain``) on the table; a ``tier=write`` request additionally requires
``can_write_data`` here and a ``tier=maintain`` request ``can_maintain``. Each tier's session policy then
grants at the object store only what that rung may change (``vending.build_session_policy``, [[LH-202]]).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated, Final

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DescribeTableRequest,
    DescribeTableResponse,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableBranchNotFoundError,
    UnauthenticatedError,
)

from catalog.api.dependencies import FgaClientDep, NamespaceDep, SettingsDep, VendorDep
from catalog.api.security import CurrentToken, RawBearerToken
from catalog.core.base_judge import BaseJudge
from catalog.core.identifiers import parse_identifier
from catalog.core.vending import Tier, dataset_facts, require_vendable_bases, table_has_branch, unsanctioned_bases
from catalog.schemas import CredentialResponse
from catalog.services import native
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, SUCCESS, audit


log = logging.getLogger(__name__)


#: THE MANAGEMENT SURFACE ([[LH-021]]). These are rask's own operations, not Lance namespace ones —
#: a spec client discovering them on `/v1/table` meets verbs the document never defines. They mount at
#: `/management/v1` and inherit the same authn/authz and delimiter guard from `api/v1/router.py`.
router = APIRouter(prefix="/management/v1/table", tags=["credentials"])

#: The relation each writing tier requires, checked on its own.
_TIER_RUNG: Final[dict[Tier, str]] = {"write": "can_write_data", "maintain": "can_maintain"}


@router.post("/{id}/credentials", response_model_exclude_none=True)
async def vend_credentials(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    vendor: VendorDep,
    web_identity_token: RawBearerToken,
    tier: Annotated[
        Tier,
        Query(
            description="`read` (can_read_data), `write` (can_write_data: read the table, put files under `data/` for `/commit` to fold in) or `maintain` (can_maintain: get, put and delete the whole prefix)."
        ),
    ] = "read",
    # [[LH-055]] WHICH branch this credential is for. Naming one narrows the grant rather than widening
    # it — writes land under that branch's own `<table>/tree/<branch>/` (its `data/` at the write tier, its
    # file directories at the maintain tier) and main drops to read — which is the isolation
    # `lancemultibasebranchingblobv2.md` says the `tree/` layout exists to give: "storage ACLs can be
    # read-only on main and write-only on the branch". Absent = main, exactly as before.
    branch: Annotated[
        str,
        Query(
            description="The branch this credential is for. Naming one NARROWS the grant: writes land under that branch's own `<table>/tree/<branch>/` (`data/` at the write tier; `_versions`, `_transactions`, `_deletions`, `_indices` and `data` at the maintain tier) and main drops to read-only. Omit for main."
        ),
    ] = "",
) -> CredentialResponse:
    """Vend scoped ``storage_options`` for direct object-store access to this table at ``tier``.

    ``web_identity_token`` is the caller's raw bearer JWT (via the shared HTTPBearer seam), forwarded to the
    object store for the web_identity flow (AssumeRoleWithWebIdentity exchanges it); other vendors ignore it.
    """
    segments = parse_identifier(id, settings.delimiter)
    # A writing tier needs ITS rung on top of the one the router guard enforced.
    if tier != "read" and settings.fga_enabled and token is not None and client is not None:
        obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
        # ONE RUNG PER TIER, never either-of ([[LH-202]]). `can_write_data` is a LOGICAL writer — it may
        # change what the table says, by appending files `/commit` folds in. `can_maintain` is a PHYSICAL
        # one: compaction, index optimization and version reclamation rewrite and delete files while
        # preserving content, and the model keeps the two apart (a maintainer is denied read, write, drop
        # and promote). The policies differ to match: a writer's reaches `data/` only, a maintainer's the
        # whole prefix. Accepting either rung for either tier would hand a writer the maintainer's
        # policy, which can commit any transaction around the door.
        #
        # The maintainer's whole-prefix credential, scoped to this table for 900 s, is the trade the owner
        # ruled on 2026-09-08; the alternative measured before it was 207 of 285 rewrites a tick signed by
        # the deployment's ROOT key, because a refused vend falls back to the ambient credential
        # (`credentials.write_options_for`).
        relation = _TIER_RUNG[tier]
        try:
            granted = await fga.check(client, user=token.sub, relation=relation, obj=obj)
        except ServiceUnavailableError:  # authz outage during a WRITE-credential request — audit, fail closed
            audit(relation, FAILURE, subject=token.sub, resource=obj, reason="authz_unavailable")
            raise
        # #41 audit the authz decision — a denied attempt to obtain writing creds is high-value.
        audit(relation, ALLOW if granted else DENY, subject=token.sub, resource=obj, tier=tier)
        if not granted:
            raise PermissionDeniedError(f"{relation} required on {obj} for a {tier}-tier credential")
    described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
    if described.location is None:  # no object-store location to scope to → fall back to server-mediated
        return CredentialResponse(mode="server_mediated")
    # The client-direct write target + optimistic-commit base version (a declared-only/new table reads as 0).
    # A tiny ROOT-cred manifest read to learn the version — not the byte-proxy (no data bytes move).
    # THE BRANCH MUST BE ONE THE TABLE HAS ([[LH-056]]), checked before anything is minted. `branch` is
    # caller-chosen and `build_session_policy` guards only `*`, `?` and `..` — the format allows `/`
    # inside a branch name, so a shape rule cannot answer this and a lookup has to. Unchecked, the grant
    # is a 900 s write credential for a prefix no manifest references: storage charged to a table for
    # objects nothing will read, under a name somebody may create later. Creating a branch needs no
    # credential, so there is no flow that must vend for one before it exists.
    if branch and not await run_in_threadpool(table_has_branch, described.location, settings.storage_options(), branch):
        raise TableBranchNotFoundError(f"branch {branch!r} not found on this table")
    facts = await run_in_threadpool(partial(dataset_facts, described.location, settings.storage_options(), branch=branch))
    # [[LH-279]] A BASE NOTHING SANCTIONED REFUSES THE VEND, before the classification answer below can
    # route it anywhere: a writer can plant any root in its manifest, and a credential granting READ on
    # it — or a server-mediated read through it — hands over another table's bytes. 409, not a fallback.
    bases = await run_in_threadpool(require_vendable_bases, described.location, facts, BaseJudge.from_settings(settings))
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
        log.info("vend_server_mediated_classified_columns", extra={"location": described.location, "columns": list(facts.classified)})
        return CredentialResponse(mode="server_mediated")
    # #3-B ⊥ #2, NARROWED to the bases the policy would actually miss ([[LH-057]]). Every base here is
    # already one the catalog sanctioned (above); what remains is whether the session policy can ADDRESS
    # it — `build_session_policy` grants each allowlisted base a `ListBase<n>`/`BaseObjects<n>` pair, so the
    # client reaches its own bytes. A sanctioned base the policy cannot grant is reachable only through
    # the catalog's root credential, which is what the server-mediated answer routes the caller to.
    if missed := unsanctioned_bases(described.location, bases, settings.multibase_data_base_list):
        log.info("vend_server_mediated_unreachable_bases", extra={"location": described.location, "bases": list(missed)})
        return CredentialResponse(mode="server_mediated")
    # The blocking STS call (AssumeRole / AssumeRoleWithWebIdentity) runs in the threadpool. A rejected
    # exchange is most often the caller's token (web_identity: expired / untrusted issuer) → 401; otherwise
    # the STS backend is unavailable/misconfigured → 503. Either way a meaningful 4xx/5xx, never a bare 500.
    try:
        creds = await run_in_threadpool(
            vendor.vend, table_location=described.location, tier=tier, web_identity_token=web_identity_token, bases=bases, branch=branch
        )
    except ClientError as exc:
        # A REJECTED exchange (the STS backend refused the request). Only web_identity re-presents the
        # caller's token, so only there is a rejection an AUTH problem (401); a rejection in any other mode
        # is a backend/config fault (503).
        if settings.vending_mode == "web_identity":
            raise UnauthenticatedError("credential exchange rejected — token invalid or untrusted") from exc
        raise ServiceUnavailableError("credential vending backend rejected the request") from exc
    except BotoCoreError as exc:
        # A TRANSPORT failure (endpoint unreachable / timeout) is a store OUTAGE in every mode, never an auth
        # problem — reporting it as 401 would send an authorized caller into a futile re-login loop (audit).
        raise ServiceUnavailableError("credential vending backend unavailable") from exc
    mode = "server_mediated" if creds is None else "direct"
    if mode == "direct":  # #41 audit the actual issuance of direct object-store credentials (who, what, tier)
        obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
        audit(
            "vend_credentials",
            SUCCESS,
            subject=token.sub if token is not None else None,
            resource=obj,
            tier=tier,
        )
    return CredentialResponse(mode=mode, credentials=creds, location=described.location, read_version=facts.read_version)
