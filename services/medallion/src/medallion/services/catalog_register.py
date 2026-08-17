"""Registration of a stage's written dataset in the catalog — the governance half of every write.

The cascade's writes were governed by PATH CONVENTION only: no `register_table` call existed
anywhere in the medallion, so a tier's output was a dataset the catalog never heard of —
unprotectable, untrashable, and invisible to the FGA doors #90 gated. Registration is what turns
the written bytes into a `table:` object: the catalog's register door seeds ownership tuples, and
every governed read path (the viewer's pages, credentials vending, protection) keys off that
object.

MODALITY-NEUTRAL BY CONSTRUCTION, and that is the point of the file's name. This shipped as
`htr_register.register_gold_table`, called only from the HTR stage — so the one lane that had it
was governed and every other lane wrote ungoverned bytes. Nothing in the logic was ever
HTR-specific: it takes an id and a URI. Governance belongs to the CASCADE, not to whichever
workload was built first, or every new modality starts ungoverned by default — the exact opposite
of an agnostic platform.

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


def register_stage_output(
    *,
    catalog_url: str,
    catalog_root: str,
    table_id: str,
    to_uri: str,
    delimiter: str = "$",
    token: str | None = None,
    timeout_seconds: float = 30.0,
) -> None:
    """Register the just-written dataset as ``table_id``; 409 means already governed.

    ``catalog_url`` empty raises naming the env var — the same fail-at-the-seam rule as the
    transcribe endpoint: a lane whose output the catalog cannot govern must not report success.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot register its output table")
    location = relative_location(to_uri, catalog_root)
    segments = table_id.split(delimiter)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=catalog_url.rstrip("/"), timeout=timeout_seconds) as client:
        # Creates are top-down (the catalog's require_parent guard, live-verified: registering into
        # an absent namespace answers NamespaceNotFound 404) — and the cascade OWNS its tier
        # namespaces, so the lane ensures its parent exists rather than demanding a manual
        # provisioning step nobody documented. 409 = already there, the steady state.
        parent = delimiter.join(segments[:-1])
        if parent:
            try:
                ns = client.post(f"/v1/namespace/{parent}/create", json={"id": segments[:-1]}, headers=headers)
            except httpx.HTTPError as exc:
                raise RegisterError(f"catalog unreachable creating parent namespace {parent!r}: {exc}") from exc
            if ns.status_code not in (200, 201, 409):
                raise RegisterError(f"catalog refused parent namespace {parent!r}: HTTP {ns.status_code} — {ns.text[:200]}")
        try:
            response = client.post(f"/v1/table/{table_id}/register", json={"id": segments, "location": location}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable registering {table_id!r}: {exc}") from exc
    if response.status_code == 409:
        # Already registered — every redelivery after the first lands here. The ownership tuples
        # were seeded by the first registration; nothing to do, and saying otherwise would make an
        # idempotent stage look like it failed.
        log.info("stage_output_already_registered", extra={"table_id": table_id})
        return
    if response.status_code >= 400:
        raise RegisterError(f"catalog refused to register {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    log.info("stage_output_registered", extra={"table_id": table_id, "location": location})
