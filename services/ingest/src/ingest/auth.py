"""The door on the ingest control API — dual-auth, fail-closed.

`POST /v1/ingests` shipped with NO authentication at all. That is worse than an ordinary missing
gate, because the endpoint takes a caller-supplied source: with `local-dir` it was one unauthenticated
request to read the ingest pod's own filesystem into a governed table, and with `s3-prefix` it is an
unauthenticated writer into any project's bronze tier. The path confinement in `adapters.py` removed
the file-read primitive; this removes the open door.

Deliberately the SAME shape as `medallion/api/produce_auth.py::authorize_produce`, because it is the
same question — "may this caller drive a write into this project's tiers?" — and two different answers
to one question is how an estate ends up with a weak door and a strong one. The differences are named
where they exist, not invented.

**Two doors, both closed by default:**

* the Dapr app-api-token — service-to-service, and project-BLIND. The shared token authenticates a
  service, not a tenant, so it may only ingest into the CONFIGURED project. Honouring an arbitrary
  requested project would let any token holder write into every tenant, which is the escalation the
  per-project check exists to prevent.
* a signed-in OIDC principal holding `can_administer` on `project:{requested}` — the human door, and
  the only one that may cross tenants. The check targets the project the request NAMES, not a fixed
  one: authorization scope must equal write scope, or an admin of project A passes the gate while the
  rows land in project B.

**Fail-closed at every step.** No service token configured is dev-open, matching the estate's other
doors. OIDC enabled with no verifier wired is 503 — an infrastructure fault, not a caller verdict, so
a valid admin bearer is never misreported as denied. An FGA outage is 503, never a silent allow. A
request carrying neither credential is 403.

Every decision is audited on `lance.audit`: an ingest is exactly the operation the admin audit viewer
reviews, and a door whose allows are invisible cannot be reviewed at all.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from pydantic_settings import BaseSettings, SettingsConfigDict

from service_kit.governed import fga
from service_kit.governed.audit import ALLOW, DENY, FAILURE, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.settings import GovernedAuthSettings


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient

    from service_kit.governed.oidc import OIDCVerifier


class IngestAuthSettings(GovernedAuthSettings, BaseSettings):
    """The auth half of the ingest service's config.

    A separate model rather than fields on the fleet `Settings`, because `GovernedAuthSettings` is the
    estate's shared vocabulary (`LANCE_OIDC_*`, `LANCE_FGA_*`) and re-spelling those names under a
    `RASK_INGEST_` prefix would give this one service its own dialect for settings every other
    governed service already reads.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, populate_by_name=True)

    #: The project the SERVICE token may ingest into. The shared token carries no tenant identity, so
    #: it is pinned here; crossing tenants requires a user bearer and its per-project FGA check.
    service_project: str = "demo"


def get_auth_settings() -> IngestAuthSettings:
    settings = IngestAuthSettings()
    settings.service_project = os.environ.get("RASK_INGEST_SERVICE_PROJECT", settings.service_project)
    return settings


AuthSettingsDep = Annotated[IngestAuthSettings, Depends(get_auth_settings)]


async def _require_admin(client: OpenFgaClient, *, user: str, obj: str) -> None:
    """`can_administer` on the project, audited, 403 on denial and 503 on outage.

    The outage case is separated on purpose: reporting an unreachable OpenFGA as a denial tells the
    caller they lack a permission they may well hold, and hides an incident behind a 403 that nobody
    pages on.
    """
    try:
        allowed = await fga.check(client, user=user, relation="can_administer", obj=obj)
    except ServiceUnavailableError:
        audit("can_administer", FAILURE, subject=user, resource=obj, reason="authz_unavailable")
        raise ServiceUnavailableError("authorization service is not available") from None
    audit("can_administer", ALLOW if allowed else DENY, subject=user, resource=obj)
    if not allowed:
        raise PermissionDeniedError("ingest needs project admin (can_administer) or the service token")


async def authorize_ingest(
    request: Request,
    settings: AuthSettingsDep,
    project: str | None = None,
    dapr_api_token: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> None:
    """Allow EITHER the Dapr app-api-token (service, configured project only) OR a project admin.

    `project` is the project the REQUEST names — the routes pass `body.project` / the run's recorded
    project — so the admin check always targets what the caller is actually writing into.

    `dapr_caller_app_id` is the invoking Dapr app-id. It is what separates "a service called me" from
    "the public front door called me on behalf of someone anonymous". The list of front doors is the
    ESTATE's, in `service_kit.governed.dapr_auth` — a per-service copy would let a newly added edge be
    refused by one door and trusted by another.
    """
    expected = os.environ.get("APP_API_TOKEN")
    # An absent service token means "this deployment has no SERVICE door" — never "this deployment has
    # no door". Returning on it alone was a full bypass of the user path as well: an estate with
    # `LANCE_OIDC_ENABLED=true`, a live FGA client and a blank or unset `APP_API_TOKEN` accepted every
    # ingest from anyone who could reach the port, while every surface reported authorization as ON.
    # That is open_python-audit's ING-01, and the blank case is the likely one — a secret that renders
    # empty is far more common than one nobody wired, and it fails OPEN rather than loudly.
    #
    # So the open posture is now conditioned on what it always claimed to mean: no service token AND no
    # user authentication configured, i.e. the documented local-dev stack. With either configured, the
    # request falls through to the OIDC + FGA project-admin path and, failing that, to the
    # `PermissionDeniedError` at the end of this function.
    if not expected and not (settings.oidc_enabled or settings.fga_enabled):
        return  # dev: nothing configured to authenticate against, exactly like the estate's other doors

    target = project or settings.service_project
    obj = f"project:{target}"
    # The caller is the PUBLIC front door, so the token daprd stamped on the way in says nothing about
    # who is actually asking. Fall through to the user-bearer path — never allow on the token alone.
    from_public_door = is_public_caller(dapr_caller_app_id)

    # `expected` is now reachable as None (auth on, no service token), so it is tested FIRST — the
    # comparison would otherwise raise AttributeError on `None.encode()` and answer 500 where the
    # honest answer is "this deployment has no service door, use a bearer".
    if expected and not from_public_door and dapr_api_token and secrets.compare_digest(dapr_api_token.encode(), expected.encode()):
        if project and project != settings.service_project:
            audit("ingest_service_token", DENY, subject=f"service:{dapr_caller_app_id or 'direct'}", resource=obj, reason="cross_project")
            raise PermissionDeniedError("the service token cannot ingest into another project; use a project-admin bearer")
        # The caller is recorded, not just the fact that A service called: an audit line that says
        # only "service" cannot answer "which one", which is the question an incident starts from.
        audit("ingest_service_token", ALLOW, subject=f"service:{dapr_caller_app_id or 'direct'}", resource=obj)
        return

    if from_public_door and dapr_api_token and not authorization:
        # Named explicitly so the denial is legible: the request DID carry a valid service token and
        # was refused anyway, which looks like a bug until you know why.
        audit("ingest_service_token", DENY, subject=f"service:{dapr_caller_app_id}", resource=obj, reason="public_caller")
        raise PermissionDeniedError(
            f"{dapr_caller_app_id!r} is a public front door: its Dapr app-token authenticates the proxy, not the caller — sign in and retry"
        )

    verifier: OIDCVerifier | None = getattr(request.app.state, "oidc", None)
    if settings.oidc_enabled and verifier is None and authorization:
        raise ServiceUnavailableError("authentication is enabled but unavailable")
    if settings.oidc_enabled and verifier is not None and authorization:
        scheme, _, raw = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw:
            raise UnauthenticatedError("malformed bearer")
        try:
            # Off the loop: verify() does synchronous OIDC discovery + JWKS fetches (up to 15s) on a
            # cold cache or key rotation — inline it stalled every in-flight request in the pod,
            # probes included (open_python-audit ING-02). Same rule _DaprWorkflowStarter.start
            # already states one module over.
            token = await asyncio.to_thread(verifier.verify, raw)
        except UnauthenticatedError:
            raise UnauthenticatedError("invalid token") from None
        client = getattr(request.app.state, "fga", None)
        if client is None:  # OIDC on but FGA unwired → fail closed, never an unauthorized ingest
            raise ServiceUnavailableError("authorization service is not available")
        await _require_admin(client, user=token.sub, obj=obj)
        return

    raise PermissionDeniedError("invalid or missing ingest credential")
