"""Vend a WRITE credential for the estate's lineage outbox — the crash-recovery seam's one door.

WHY THIS EXISTS AT ALL. The outbox is where a producer stages the full `RunEvent` JSON *before* it
publishes, so a crash between the commit and a durable delivery is recoverable: the relay re-ingests
whatever survived. Ingest could not stage into it — it holds no S3 key by design (STS-only, which is
the stronger posture), so `_outbox_storage_options` handed `stage_event` an endpoint and no
credential. Measured twice inside one real run (2026-09-10): the primary emit was refused AND its
backstop could not write, so the run lost its lineage event outright and still reported COMPLETE.

THE MECHANISM WAS NOT DECIDED HERE. The estate's standing rule is "STS for STORAGE — a scoped static
key is not a fix", which rules out minting ingest a RustFS user the way the medallion has one. And the
primitive already expresses it: `vending.build_session_policy(bucket, prefix, tier, bases)` is
prefix-GENERIC rather than table-keyed, so `(lance-catalog, _lineage_outbox, write)` needs nothing new
from it, and as an STS *session* policy it can only RESTRICT the catalog's role, never widen it. What
was missing was a door: the sole vending route was `POST /v1/table/{id}/credentials`, and a control
prefix is not a table.

THE DOOR TAKES NO PATH, and that is the property that keeps a new vending surface from being a new
attack surface. It vends for `settings.lineage_outbox_uri` — the estate's own configured outbox — so
there is no caller-supplied prefix to traverse, no way to ask it for a tenant's bucket, and nothing to
validate against IAM metacharacters that `build_session_policy` would have to reject.

THE RUNG IS `can_stage_events`, minted for this and checked on the ESTATE ROOT. Staging is not a data
act: the object is a bookkeeping record under a control prefix, so no existing rung expresses it and
reusing one (`can_write_data`, say) would hand every tenant writer a platform capability. Owner-tier
would be wrong in the other direction — the holder is a SERVICE, and a service must never be an estate
owner. So `event_stager` is its own assignable role, exactly the shape `can_maintain` and `publisher`
were minted for, and `model.fga.yaml` pins the refusals that make it worth having: the TRAINER
(privileged at both doors, does not stage) and the web BFF (the anonymous read identity) are denied.

INGEST IS THE ONLY HOLDER the chart grants (`bootstrap-admin.yaml`), and that is the rung working
rather than a gap. Five services stage to `_lineage_outbox`; four of them sign with a key they already
hold and never reach this door, so granting them the rung would leave a standing tuple nothing
exercises. Ingest holds no S3 key by design, which is why it is the one that needs a credential.
"""

from __future__ import annotations

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError, UnauthenticatedError

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, SettingsDep, VendorDep
from catalog.api.security import CurrentToken, RawBearerToken
from catalog.schemas import CredentialResponse
from service_kit.governed.audit import SUCCESS, audit


router = APIRouter(prefix="/v1/outbox", tags=["credentials"])


@router.post("/credentials", response_model_exclude_none=True)
async def vend_outbox_credentials(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    vendor: VendorDep,
    web_identity_token: RawBearerToken,
) -> CredentialResponse:
    """Vend a write-tier credential scoped to the estate's lineage outbox prefix.

    Answers `server_mediated` when no outbox is configured or the vendor declines — the same shape the
    table door uses, so a caller has one response contract rather than two. A caller that receives it
    stages nothing and its events stay on the primary publish path, which is the honest degradation:
    the backstop is absent, not silently broken.
    """
    outbox = settings.lineage_outbox_uri
    if not outbox:
        # Not an error. An estate with no outbox configured has no recovery seam to credential, and
        # saying so beats vending a credential for a prefix nothing drains.
        return CredentialResponse(mode="server_mediated")

    # THE ESTATE ROOT, never a tenant object. `fga_root_object` is the same object `POST /v1/projects`
    # and the events feed gate on, so "may stage" means one thing estate-wide rather than one thing per
    # warehouse — and a stager holding it cannot reach a tenant's data with it (pinned in model.fga.yaml).
    #
    # THROUGH `require_relation`, not a bare `fga.check`, and that is a convention rather than a style
    # preference: it is the estate's one fail-closed ladder (FGA off → no-op, unwired client → 503,
    # unauthenticated → 401, deny → 403, OpenFGA outage → 503 and never allow), and
    # `test_stores_reads_are_gated.py` refuses a route whose gate it cannot see. A hand-rolled check
    # reads as authn-only to that gate — which is exactly what it flagged when this was written the
    # other way, and the ladder is the half a hand-rolled check gets subtly wrong.
    await fga_deps.require_relation(client, settings, token, relation="can_stage_events", obj=settings.fga_root_object)

    try:
        creds = await run_in_threadpool(vendor.vend, table_location=outbox, tier="write", web_identity_token=web_identity_token, bases=())
    except ClientError as exc:
        # Same split the table door makes: only web_identity re-presents the caller's token, so only
        # there is a rejection an AUTH problem; anywhere else it is a backend fault.
        if settings.vending_mode == "web_identity":
            raise UnauthenticatedError("credential exchange rejected — token invalid or untrusted") from exc
        raise ServiceUnavailableError("credential vending backend rejected the request") from exc
    except BotoCoreError as exc:
        raise ServiceUnavailableError("credential vending backend unavailable") from exc

    mode = "server_mediated" if creds is None else "direct"
    if mode == "direct":
        audit("vend_outbox_credentials", SUCCESS, subject=token.sub if token is not None else None, resource=outbox, tier="write")
    return CredentialResponse(mode=mode, credentials=creds, location=outbox)
