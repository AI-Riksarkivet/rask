"""How maintenance identifies itself to the catalog: ONE builder, for every door it calls.

There are two: the compaction plan/commit pair and credential vending. One builder serves both, because
a credential control applied to one of them is not a control. Both doors are read alike: a 401 raises
`MaintenanceUnauthenticated`, a 403 `MaintenanceDenied`, and a 404 naming no table or namespace
`TableNotGoverned`, so a credential this builder gets wrong stops the unit rather than being signed around.
A vend door's 5xx stops the unit too (`VendUndecided`). Any other failure of the vend door to answer
degrades to the ambient credential, with an `info` log and `compaction.credential.tier` tier=ambient.

THE CREDENTIAL IS THE POD'S OWN SERVICE-ACCOUNT TOKEN (D1), projected by the kubelet with audience
`rask-catalog` and read from the file on EVERY call. The kubelet rewrites it at ~515 s of a 600 s
lifetime (measured, LH-220 probe d), so a token held for the life of the process is refused ten minutes
after boot. Nothing else names the caller: the catalog maps the verified service account to a subject,
so no header this pod sends can claim to be anyone else.

AN UNREADABLE TOKEN STOPS THE UNIT AS UNAUTHENTICATED. It is this service's credential, absent at every
table alike, and the request is not sent: a call without it would be answered as anonymous, and the
ambient key standing in for a refused vend would sign what the catalog never authorized.
"""

from __future__ import annotations

from maintenance.core.config import MaintenanceSettings
from maintenance.services.compaction_executor import MaintenanceUnauthenticated
from service_kit.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer


def service_headers(settings: MaintenanceSettings) -> dict[str, str]:
    """The `Authorization` header carrying this pod's `rask-catalog` token, read now.

    Raises `MaintenanceUnauthenticated` when the projected file cannot be read or is empty.
    """
    try:
        return identity_bearer(settings.catalog_identity_token_file)
    except IdentityTokenUnavailableError as exc:
        raise MaintenanceUnauthenticated(f"maintenance cannot present its catalog credential: {exc}") from exc
