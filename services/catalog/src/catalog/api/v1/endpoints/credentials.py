"""Credential vending — hand an authorized client scoped ``storage_options`` for DIRECT object I/O.

Track B: instead of every byte flowing through the catalog (Mode B, the server-mediated Arrow-IPC path),
an authorized caller can request short-TTL, per-table, tier-scoped credentials and read/write the Lance
data on object storage itself (LanceDB SDK / lance-ray / pylance). The vendor plug is chosen at boot
(``LANCE_VENDING_MODE``): ``sts`` (AssumeRole + a per-table session policy — the recommended path),
``static`` (per-bucket keys), or ``mode_b`` (vends nothing → the client uses the data endpoints).

Authz: the router-level :func:`catalog.api.fga_deps.authorize` already required ``can_read_data`` on the
table (``credentials`` is mapped to the reader-data rung); a ``tier=write`` request additionally requires
``can_write_data`` here. So a reader gets read-scoped creds and a writer gets write-scoped creds — the
session policy then enforces the same scope at the object store.
"""

from __future__ import annotations

import logging
from typing import Annotated

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DescribeTableRequest,
    DescribeTableResponse,
    PermissionDeniedError,
    ServiceUnavailableError,
    UnauthenticatedError,
)

from catalog.api.dependencies import FgaClientDep, NamespaceDep, SettingsDep, VendorDep
from catalog.api.security import CurrentToken, RawBearerToken
from catalog.core.identifiers import parse_identifier
from catalog.core.vending import Tier, dataset_facts, unsanctioned_bases
from catalog.schemas import CredentialResponse
from catalog.services import native
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, SUCCESS, audit


log = logging.getLogger(__name__)


router = APIRouter(prefix="/v1/table", tags=["credentials"])


@router.post("/{id}/credentials", response_model_exclude_none=True)
async def vend_credentials(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    vendor: VendorDep,
    web_identity_token: RawBearerToken,
    tier: Annotated[Tier, Query()] = "read",
) -> CredentialResponse:
    """Vend scoped ``storage_options`` for direct object-store access to this table at ``tier``.

    ``web_identity_token`` is the caller's raw bearer JWT (via the shared HTTPBearer seam), forwarded to the
    object store for the web_identity flow (AssumeRoleWithWebIdentity exchanges it); other vendors ignore it.
    """
    segments = parse_identifier(id, settings.delimiter)
    # A write-tier vend needs the writer rung on top of the reader rung the router guard enforced.
    if tier == "write" and settings.fga_enabled and token is not None and client is not None:
        obj = f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}"
        # EITHER RUNG OPENS THIS DOOR, and they mean different things. `can_write_data` is a LOGICAL
        # writer — it may change what the table says. `can_maintain` is a PHYSICAL one: compaction,
        # index optimization and version reclamation rewrite files while preserving content, and the
        # model keeps the two apart (a maintainer is denied read, write, drop and promote).
        #
        # The object store cannot hold that distinction — a rewrite and a write are both `PutObject` —
        # so a maintainer necessarily receives a write-TIER credential, scoped to this table's prefix
        # for 900 s. That is the trade the owner ruled on 2026-09-08, and the alternative is what was
        # measured before it: 207 of 285 rewrites a tick signed by the deployment's ROOT key, because a
        # refused vend falls back to the ambient credential (`credentials.write_options_for`). Bounding
        # a maintainer to its own table is the narrower of the two by an enormous margin.
        granted_by: str | None = None
        for relation in ("can_write_data", "can_maintain"):
            try:
                if await fga.check(client, user=token.sub, relation=relation, obj=obj):
                    granted_by = relation
                    break
            except ServiceUnavailableError:  # authz outage during a WRITE-credential request — audit, fail closed
                audit(relation, FAILURE, subject=token.sub, resource=obj, reason="authz_unavailable")
                raise
        # #41 audit the write-tier authz decision — a denied attempt to obtain WRITE creds is high-value.
        # ONE line naming the rung that answered, never one per probe: a maintainer is denied
        # `can_write_data` by design on every single tick, and auditing that would bury the denials that
        # matter under hundreds of routine ones an hour.
        audit(granted_by or "can_write_data", ALLOW if granted_by else DENY, subject=token.sub, resource=obj, tier="write")
        if granted_by is None:
            raise PermissionDeniedError(f"can_write_data or can_maintain required on {obj} for a write-tier credential")
    described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
    if described.location is None:  # no object-store location to scope to → fall back to server-mediated
        return CredentialResponse(mode="server_mediated")
    # The client-direct write target + optimistic-commit base version (a declared-only/new table reads as 0).
    # A tiny ROOT-cred manifest read to learn the version — not the byte-proxy (no data bytes move).
    read_version, declared_bases = await run_in_threadpool(dataset_facts, described.location, settings.storage_options())
    # #3-B ⊥ #2, NARROWED to the bases the policy would actually miss ([[LH-057]]). A multi-base table
    # whose every declared base is sanctioned IS direct-vendable: `build_session_policy` grants each one
    # a `ListBase<n>`/`BaseObjects<n>` pair, so the client reaches its own bytes. Only a base
    # `_base_is_sanctioned` refuses is unreachable, and only that justifies proxying the whole table
    # through the catalog's root credential. Asked off the manifest read above rather than by walking
    # every fragment's data files, and no feature flag is needed: a single-bucket table declares no
    # bases and this costs nothing.
    if missed := unsanctioned_bases(described.location, declared_bases, settings.vend_sanctioned_bases):
        log.info("vend_server_mediated_unreachable_bases", extra={"location": described.location, "bases": list(missed)})
        return CredentialResponse(mode="server_mediated")
    # The blocking STS call (AssumeRole / AssumeRoleWithWebIdentity) runs in the threadpool. A rejected
    # exchange is most often the caller's token (web_identity: expired / untrusted issuer) → 401; otherwise
    # the STS backend is unavailable/misconfigured → 503. Either way a meaningful 4xx/5xx, never a bare 500.
    try:
        creds = await run_in_threadpool(vendor.vend, table_location=described.location, tier=tier, web_identity_token=web_identity_token, bases=declared_bases)
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
    return CredentialResponse(mode=mode, credentials=creds, location=described.location, read_version=read_version)
