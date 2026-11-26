"""Authentication dependency for the lineage read + ingest endpoints.

When OIDC is disabled (the default) this is a no-op and all routes stay open. When enabled, every caller presents a
verified bearer, and the bearer's unverified issuer decides which verifier answers it:

- the cluster's service-account issuer (`RASK_SA_ISSUER`): a pod's projected token, verified offline against the
  cluster's key set; whatever its namespace it is the subject `kubernetes~system:serviceaccount:<ns>:<sa>`
  (`lakehouse.governed.machine_identity`, [[LH-220]], ADR 0156 C5). It comes back a `ServicePrincipal` and never
  falls through to the IdP;
- anything else: the IdP's verifier (`IdpVerifier`, the same one the catalog uses), which answers a person as the
  principal the deployment's naming rule reads from the token, `<idp-id>~<subject>` ([[XC-214]], [[LH-063]]), and a
  client signed in for itself as the `ServicePrincipal` `<idp-id>-client~<client id>` (ADR 0181, ADR 0208).

Either way the caller is authorized by FGA as its principal key, `.subject`. Nothing a caller asserts in a header names it,
and a service-account token's audience is lineage's own (`rask-lineage`), so a token minted for another door is
refused here.

Fail-closed invariant: if OIDC is enabled but the verifier that would answer a bearer was never wired onto
``app.state`` (startup/discovery skew), the door raises ``ServiceUnavailableError`` (503) rather than letting the
request through or handing it to the other verifier.
"""

from typing import Annotated

from fastapi import Depends

from lakehouse.governed.admission import Caller, door_gate
from lineage.api.dependencies import request_settings


#: Lineage's gate: every door depending on `authenticate` judges its caller from the headers before its body is read
#: (`lakehouse.governed.admission`): the intake's JSON body is never read, let alone parsed, for a caller with no token.
_GATE = door_gate(request_settings)
authenticate = _GATE.authenticate
judge = _GATE.judge

#: The authenticated caller — a ``PersonPrincipal``, a ``ServicePrincipal``, or ``None`` when OIDC is off. Authorization
#: reads its principal key, ``.subject``.
CurrentToken = Annotated[Caller | None, Depends(authenticate)]

__all__ = ["Caller", "CurrentToken", "authenticate", "judge"]
