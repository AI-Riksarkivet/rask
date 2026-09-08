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

import lance
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
from catalog.core.vending import Tier, has_external_bases
from catalog.schemas import CredentialResponse
from catalog.services import native
from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, SUCCESS, audit
from service_kit.lakehouse.features import manifest_base_path_refs
from service_kit.lakehouse.objectfs import same_store_uri


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
        try:
            ok = await fga.check(client, user=token.sub, relation="can_write_data", obj=obj)
        except ServiceUnavailableError:  # authz outage during a WRITE-credential request — audit, fail closed
            audit("can_write_data", FAILURE, subject=token.sub, resource=obj, reason="authz_unavailable")
            raise
        # #41 audit the write-tier authz decision — a denied attempt to obtain WRITE creds is high-value.
        audit("can_write_data", ALLOW if ok else DENY, subject=token.sub, resource=obj, tier="write")
        if not ok:
            raise PermissionDeniedError(f"can_write_data required on {obj} for a write-tier credential")
    described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
    if described.location is None:  # no object-store location to scope to → fall back to server-mediated
        return CredentialResponse(mode="server_mediated")
    # #3-B ⊥ #2: a multi-base table's fragments live in registered DATA bases the vended STS session policy
    # (scoped to the primary root bucket only) cannot reach — a direct-vended client would be DENIED at the
    # object store reading/writing them. Fall back to server-mediated IO (the catalog's root creds reach all
    # bases). Gated on the feature flag so a single-bucket deployment never pays the fragment scan.
    if settings.multibase_data_base_list and await run_in_threadpool(has_external_bases, described.location, settings.storage_options()):
        return CredentialResponse(mode="server_mediated")
    # The client-direct write target + optimistic-commit base version (a declared-only/new table reads as 0).
    # A tiny ROOT-cred manifest read to learn the version — not the byte-proxy (no data bytes move).
    read_version, declared_bases = await run_in_threadpool(_dataset_facts, described.location, settings.storage_options())
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


def _dataset_facts(location: str, storage_options: dict[str, str]) -> tuple[int, tuple[str, ...]]:
    """``(current version, declared base paths)`` from ONE root-cred manifest read.

    The version is the client's optimistic-append base; 0 for a declared-only/new table with no
    readable dataset yet. The bases are what the vended policy must also be able to READ — a table
    whose fragments carry a ``base_id`` resolves them through those paths, so a credential scoped to
    the table prefix alone is scoped to less than the table is (§ H12, measured: 69 datasets a tick
    refused compaction because the maintainer could not probe a declared base).

    Both facts come off the same handle deliberately: this read already existed for the version, and a
    second open to learn the bases would double the manifest reads on every vend.

    Base spellings are normalised through :func:`same_store_uri` because a manifest states a base in
    the manifest's own spelling, which may be schemeless — the policy needs a bucket and a key.
    """
    try:
        ds = lance.dataset(location, storage_options=storage_options)
    except (ValueError, OSError):
        return 0, ()
    bases: list[str] = []
    try:
        for ref in manifest_base_path_refs(ds):
            bases.append(same_store_uri(location, ref.path))
    except Exception:
        # A base we cannot SPELL is one the policy must not guess at. Vending without it yields exactly
        # today's behaviour — the narrower credential — rather than a wrong grant.
        log.warning("vend_base_paths_unreadable", extra={"location": location}, exc_info=True)
        bases = []
    return int(ds.version), tuple(bases)
