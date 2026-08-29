"""``GET /v1/me`` — the signed-in caller's self-describe: verified identity claims + effective standing.

The console shell needs one cheap call to render *who am I and what am I* — the verified subject, the
display claims, whether the caller is an estate admin, and which tenants they belong to (and at what
role). Everything here is DERIVED from the verified token + OpenFGA; nothing is caller-supplied, and the
response never discloses anything about *other* principals (unlike ``/v1/projects``, which is
estate-observer gated for exactly that reason — this route needs no FGA gate because its data scope is
the caller themself).

**Authenticated-only, no authorization gate.** Any VERIFIED token gets a 200; anonymous is 401 (there is
no self to describe). ``/v1/me`` matches no resource prefix in ``fga_deps.authorize``, so the router-wide
gate contributes only its 401/503 pre-path checks when FGA is on — the 401 floor is enforced here
explicitly so it also holds with FGA off.

**Display-only FGA lookups, degrading — never a 5xx.** ``estate_admin`` is ``can_observe_events`` on the
fixed root object (``settings.fga_root_object``) — the same estate-OBSERVER action that gates
``/v1/events`` + ``/v1/projects``, so the shell can decide whether those surfaces are even reachable.
``projects`` comes from the #51 ``fga.list_objects`` wrapper on the ``project`` type — ``can_administer``
first, then ``member`` (the model folds admins into members, so the admin-first pass wins the dedupe).
All lookups share one 2s wall-clock budget and each degrades on its own outage/expiry: an OpenFGA
brownout costs display fidelity, never the whole self-describe. None of them are audited — the #41 trail
records authorization *decisions* (allow/deny gates), and these gate nothing (the ``/v1/projects``
``_admins_for`` precedent). FGA off → ``estate_admin=true``, ``projects=[]`` (dev parity: every
``require_*`` gate is a no-op there, so the caller genuinely can do everything).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from lance_namespace import ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient

from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.schemas import MeProject, MeResponse
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/me", tags=["me"])

# ONE wall-clock budget for all the display-only FGA lookups (estate check + the two project lists,
# sequential). Same rationale as /v1/projects' _ADMINS_TIMEOUT_SECONDS: the BFF gives the whole request
# ~8s, and the wrapper's default bounded-retry ladder alone can exceed it — so each call runs a single
# attempt (retry_attempts=1) under the shared deadline and degrades on expiry.
_LOOKUP_BUDGET_SECONDS = 2.0


def _claim(token: IDToken, name: str) -> str | None:
    """A non-empty string claim off the verified token (``extra='allow'`` keeps them), else ``None``."""
    value = getattr(token, name, None)
    return value if isinstance(value, str) and value else None


async def _estate_admin(client: OpenFgaClient, settings: Settings, sub: str, deadline: float) -> bool:
    """``can_observe_events`` on the fixed root object — the estate-OBSERVER standing, or ``False``.

    Display-only (gates nothing here), so it is not audited and degrades to ``False`` on an OpenFGA
    outage or budget expiry — fail-safe: a broken authz layer must never paint admin surfaces onto the
    shell (the real gates on /v1/events//v1/projects still fail closed on their own).
    """
    try:
        async with asyncio.timeout_at(deadline):
            return await fga.check(
                client,
                user=sub,
                relation="can_observe_events",
                obj=settings.fga_root_object,
                retry_attempts=1,
            )
    except (ServiceUnavailableError, TimeoutError):
        log.warning("me_estate_admin_unavailable", extra={"sub": sub})
        return False


async def _project_ids(client: OpenFgaClient, sub: str, relation: str, deadline: float) -> list[str]:
    """The project ids the caller holds ``relation`` on, via ``fga.list_objects`` (#51) — sorted.

    Degrades to ``[]`` PER RELATION on an outage or budget expiry (never 5xxes the endpoint): a missing
    member list still leaves the admin list serving, and vice versa.
    """
    try:
        async with asyncio.timeout_at(deadline):
            objects = (await fga.list_objects(client, user=sub, relation=relation, object_type="project", retry_attempts=1)).objects
    except (ServiceUnavailableError, TimeoutError):
        log.warning("me_projects_unavailable", extra={"sub": sub, "relation": relation})
        return []
    return sorted(o.removeprefix("project:") for o in objects)


@router.get("")
async def get_me(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> MeResponse:
    """Describe the signed-in caller: verified claims, estate standing, and tenant memberships.

    Any verified token → 200; anonymous → 401. FGA off → ``estate_admin=true`` + ``projects=[]`` (dev
    parity). With FGA on, the lookups are display-only and DEGRADE per relation on an OpenFGA
    outage/brownout — this endpoint never 5xxes over authz-lookup health."""
    # Authn floor: with FGA on the router-wide authorize already 401'd an anonymous call; with FGA off it
    # is a no-op, so the contract's 401 is enforced here — there is no self to describe without a token.
    if token is None:
        raise UnauthenticatedError("authentication required")
    name, email = _claim(token, "name"), _claim(token, "email")
    if not (settings.fga_enabled and client is not None):
        # Dev parity (FGA off): every require_* gate is a no-op, so the caller effectively holds full
        # standing — say so honestly rather than painting a locked-down shell over an open catalog. (The
        # enabled-but-unwired client case never reaches here: the router-wide authorize 503s it first.)
        return MeResponse(sub=token.sub, name=name, email=email, estate_admin=True, projects=[])
    # One shared deadline across the three sequential lookups — admin BEFORE member, so the stronger
    # role wins the dedupe below and a budget expiry costs the weaker list first.
    deadline = asyncio.get_running_loop().time() + _LOOKUP_BUDGET_SECONDS
    estate_admin = await _estate_admin(client, settings, token.sub, deadline)
    admin_ids = await _project_ids(client, token.sub, "can_administer", deadline)
    member_ids = await _project_ids(client, token.sub, "member", deadline)
    # The model folds admins into members (`member: ... or admin`), so every admin project also shows up
    # in the member list — keep the admin row, drop the duplicate.
    projects = [MeProject(project=p, role="admin") for p in admin_ids]
    admin_set = set(admin_ids)
    projects += [MeProject(project=p, role="member") for p in member_ids if p not in admin_set]
    return MeResponse(sub=token.sub, name=name, email=email, estate_admin=estate_admin, projects=projects)
