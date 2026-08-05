"""Registration of the HTR gold table in the catalog (#88 step 5) — the governance half of the write.

The cascade's writes were governed by PATH CONVENTION only: no `register_table` call existed
anywhere in the medallion, so `gold$htr` would have been a dataset the catalog never heard of —
unprotectable, untrashable, and invisible to the FGA doors #90 gated. Registration is what turns
the written bytes into a `table:` object: the catalog's register door seeds ownership tuples, and
every governed read path (the viewer's pages, credentials vending, protection) keys off that
object.

Register — not create-through-the-catalog. The mover owns where it WRITES (the cascade's standing
rule) and `register_table` exists precisely for data written outside the catalog's own doors.
Idempotent by treating 409 as success: the stage is overwrite-idempotent and re-registers on every
redelivery; the FIRST registration is the one that seeds ownership.

The location must be RELATIVE to the catalog's connection root — the #75 undrop lesson, learned
the hard way: `register_table` refuses absolute URIs on the `dir` backend. A `to_uri` outside the
root cannot be expressed relatively and raises here, naming both, rather than letting the catalog
answer an opaque 400.
"""

from __future__ import annotations

import logging

import httpx


log = logging.getLogger(__name__)


class RegisterError(RuntimeError):
    """The catalog refused or could not be reached — the stage must NOT report success.

    An unregistered gold table is #88's defect intact, so this propagates and the mover RETRYs.
    That re-runs the (expensive) transcribe too — stated cost: the overwrite is idempotent and a
    catalog outage is rarer than a Serve one; splitting the stage into resumable halves is P7b's
    re-cut, not a quiet retry layer here.
    """


def relative_location(to_uri: str, catalog_root: str) -> str:
    """``to_uri`` expressed relative to the catalog's connection root, or raise naming both."""
    root = catalog_root.rstrip("/")
    if not root or not to_uri.startswith(root + "/"):
        raise RegisterError(
            f"cannot register {to_uri!r}: not under the catalog root {catalog_root!r} "
            "(MEDALLION_CATALOG_ROOT must be the catalog's own connection root — the dir backend refuses absolute locations)"
        )
    return to_uri[len(root) + 1 :]


def register_gold_table(
    *,
    catalog_url: str,
    catalog_root: str,
    table_id: str,
    to_uri: str,
    delimiter: str = "$",
    token: str | None = None,
    timeout_seconds: float = 30.0,
) -> None:
    """Register the just-written gold dataset as ``table_id``; 409 means already governed.

    ``catalog_url`` empty raises naming the env var — the same fail-at-the-seam rule as the
    transcribe endpoint: a lane whose output the catalog cannot govern must not report success.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — the HTR lane cannot register its gold table")
    location = relative_location(to_uri, catalog_root)
    segments = table_id.split(delimiter)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=catalog_url.rstrip("/"), timeout=timeout_seconds) as client:
        try:
            response = client.post(f"/v1/table/{table_id}/register", json={"id": segments, "location": location}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable registering {table_id!r}: {exc}") from exc
    if response.status_code == 409:
        # Already registered — every redelivery after the first lands here. The ownership tuples
        # were seeded by the first registration; nothing to do, and saying otherwise would make an
        # idempotent stage look like it failed.
        log.info("htr_gold_already_registered", extra={"table_id": table_id})
        return
    if response.status_code >= 400:
        raise RegisterError(f"catalog refused to register {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    log.info("htr_gold_registered", extra={"table_id": table_id, "location": location})
