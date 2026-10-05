"""Whether a warehouse's object-store endpoint is the estate's own, and the refusal when it is not ([[LH-205]]).

THE CATALOG HOLDS ONE CREDENTIAL PAIR, the estate's, and every connection it builds signs with it. A
warehouse record may name an ``endpoint``, but no door consumes a per-warehouse credential: no request
sets ``credential_ref`` and nothing reads it. So a record naming another store would have every open
under it sign toward that host with the estate's access-key id, its bucket provisioned on the estate's
store, and its vend AssumeRole at the estate's STS. Until a reference is resolved and consumed end to
end, an endpoint other than the estate's is refused at create, and an existing record naming one is
refused at every door that would reach a store for it.

"Resolvable" is answered by what the estate consumes, not by what it could fetch.
``catalog.services.warehouse_credentials.resolve`` can fetch a bundle from the Dapr secret store, but
only the multibase write path uses it; open, provision and vend never do, so a warehouse reference
resolves to nothing any of them would sign with. Lakekeeper makes the same call at the same door: it
validates a warehouse's storage credential at create and refuses the warehouse when it cannot
(``crates/lakekeeper/src/api/management/v1/warehouse/mod.rs:97-110``).

"The estate's endpoint" is ``Settings.s3_endpoint``, compared after normalisation so a respelling of it
is still the estate (scheme and host case, a default port, a trailing slash or dot) while anything
carrying userinfo, a query, a fragment or an unparseable port is another store.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from lance_namespace import UnsupportedOperationError


log = logging.getLogger(__name__)

#: The port a scheme implies when the endpoint names none, so ``http://store`` and ``http://store:80``
#: compare equal. A scheme outside this map with no explicit port has no comparable form.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _comparable(endpoint: str) -> tuple[str, str, int, str] | None:
    """``(scheme, host, port, path)`` with the trivial spelling differences removed, or ``None`` when ``endpoint`` has no such form."""
    parts = urlsplit(endpoint.strip())
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment or not parts.hostname:
        return None
    port = port if port is not None else _DEFAULT_PORTS.get(parts.scheme)
    if port is None:
        return None
    return parts.scheme, parts.hostname.rstrip("."), port, parts.path.rstrip("/")


def names_the_estate_store(endpoint: str | None, *, estate: str) -> bool:
    """True when ``endpoint`` is absent or is ``estate`` respelled; False for any other store.

    An absent or empty endpoint is the estate's by the record's own contract. An empty ``estate`` is AWS
    proper, and every explicit endpoint is then another store: the comparison fails closed rather than
    guessing which regional host counts as the estate's.
    """
    if not endpoint:
        return True
    mine = _comparable(endpoint)
    return mine is not None and bool(estate) and mine == _comparable(estate)


def require_estate_store(endpoint: str | None, *, estate: str, subject: str) -> None:
    """Refuse an operation on ``subject`` whose store is not the estate's.

    ``UnsupportedOperationError`` (spec code 0, HTTP 406): the request is well formed and the record is
    one the catalog cannot yet serve. The endpoint goes to the log and not to the caller, because the
    caller of an open is often a reader who was never shown the warehouse's configuration.

    Raises:
        UnsupportedOperationError: ``endpoint`` names a store other than ``estate``.
    """
    if names_the_estate_store(endpoint, estate=estate):
        return
    log.warning("warehouse_foreign_store_refused", extra={"subject": subject, "endpoint": endpoint})
    raise UnsupportedOperationError(
        f"{subject} is reached at an object store other than the estate's, and the catalog consumes no per-warehouse "
        "credential, so it refuses to sign with the estate's own key toward that store. Re-POST the warehouse with "
        '`"endpoint": ""` to return it to the estate\'s store, or delete it.'
    )
