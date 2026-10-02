"""How ingest identifies itself upstream: the projected service-account token for the door it calls ([[LH-220]], D1).

Ingest calls two governed doors on two services, the catalog (`catalog_service.py`) and the lineage graph
(`provenance.py`). Each verifies a projected token whose audience is its own, so each door has its own file, and the
caller is whoever the cluster says the token's account is. Nothing else on the request names the caller.

THE FILE IS READ ON EVERY CALL. The kubelet replaces a 600 s token at ~515 s (measured 2026-10-02, LH-220 P5.3 d), so
a token held for the life of the process is refused within ten minutes. An unreadable or empty file raises
`IdentityTokenUnavailableError` rather than letting the request go out anonymous: an anonymous call to a governed door
is refused for a reason invisible from this side, and at the lineage door the refusal is swallowed by design (a landed
commit must not become a failed run), which leaves a gap in the graph that looks like a healthy estate.
"""

from __future__ import annotations

from ingest.config import settings
from service_kit.governed.machine_identity import identity_bearer


def catalog_bearer() -> dict[str, str]:
    """The `Authorization` header carrying this pod's `rask-catalog` token, read now."""
    return identity_bearer(settings().catalog_identity_token_file)


def lineage_bearer() -> dict[str, str]:
    """The `Authorization` header carrying this pod's `rask-lineage` token, read now."""
    return identity_bearer(settings().lineage_identity_token_file)
