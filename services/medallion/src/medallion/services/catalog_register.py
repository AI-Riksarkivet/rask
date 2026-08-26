"""Registration of a stage's written dataset in the catalog — the governance half of every write.

The cascade's writes were governed by PATH CONVENTION only: no `register_table` call existed
anywhere in the medallion, so a tier's output was a dataset the catalog never heard of —
unprotectable, untrashable, and invisible to the FGA doors #90 gated. Registration is what turns
the written bytes into a `table:` object: the catalog's register door seeds ownership tuples, and
every governed read path (the viewer's pages, credentials vending, protection) keys off that
object.

WORKLOAD-NEUTRAL BY CONSTRUCTION, and that is the point of the file's name. This shipped named for
one workload and called from that workload's stage alone, so the lane built first was governed and
every other lane wrote ungoverned bytes. Nothing in the logic was ever workload-specific: it takes
an id and a URI. Governance belongs to the CASCADE, or every new workload starts ungoverned by
default — the exact opposite of an agnostic platform.

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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

import httpx
import pyarrow as pa
from pydantic import BaseModel, Field


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


def _credential(
    *,
    token: str | None,
    app_token: str | None,
    service_identity: str | None,
    dedicated_token: Callable[[str], str | None] | None = None,
) -> dict[str, str]:
    """The credential a mover presents to the catalog, service door first.

    A service authenticates AS ITSELF — the app token daprd already injects plus the subject it
    claims — and needs no bearer. The bearer path came first here and was the wrong shape: the
    catalog verifies OIDC JWTs, a JWT expires, and a static string in a secret store cannot be one.
    The ingest plane hit this and its fix records the cost — a fail-closed run chasing a
    `catalog-token` secret that never needed to exist.

    Both halves or neither: the door requires the token AND the identity
    (`catalog/api/security.py`), and sending one is refused for a reason invisible from this side.
    A forwarded human bearer stays supported below, because that is a real case a service call
    simply does not have.
    """
    if app_token and service_identity:
        # A PRIVILEGED identity presents its OWN credential, never the estate's shared one. The door
        # (`service_kit.governed.dapr_auth`) binds such a subject to `service-token-<identity>`, and
        # rendering that server-side alone is not enabling the control — it is refusing every
        # privileged caller. Measured 2026-08-26: the catalog began demanding the dedicated token
        # while movers still sent APP_API_TOKEN, and every call 401'd until it was reverted.
        #
        # `None` from the resolver means the bundle was READ and this identity is not privileged, so
        # the shared token is correct. Falling back rather than refusing keeps ONE authority over the
        # decision: the door already hard-refuses a privileged subject presenting the wrong
        # credential, and a refusal here would only reach the same outcome with less information.
        presented = (dedicated_token(service_identity) if dedicated_token else None) or app_token
        return {"dapr-api-token": presented, "x-lance-service-identity": service_identity}
    return {"Authorization": f"Bearer {token}"} if token else {}


def register_stage_output(
    *,
    catalog_url: str,
    catalog_root: str,
    table_id: str,
    to_uri: str,
    delimiter: str = "$",
    token: str | None = None,
    app_token: str | None = None,
    service_identity: str | None = None,
    dedicated_token: Callable[[str], str | None] | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> None:
    """Register the just-written dataset as ``table_id``; 409 means already governed.

    ``catalog_url`` empty raises naming the env var — the same fail-at-the-seam rule as the
    transcribe endpoint: a lane whose output the catalog cannot govern must not report success.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot register its output table")
    location = relative_location(to_uri, catalog_root)
    segments = table_id.split(delimiter)
    headers = _credential(token=token, app_token=app_token, service_identity=service_identity, dedicated_token=dedicated_token)
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        # Creates are top-down (the catalog's require_parent guard, live-verified: registering into
        # an absent namespace answers NamespaceNotFound 404) — and the cascade OWNS its tier
        # namespaces, so the lane ensures its parent exists rather than demanding a manual
        # provisioning step nobody documented. 409 = already there, the steady state.
        # A TOP-LEVEL parent is the WAREHOUSE's to create, never this lane's. `require_warehouse_scoped`
        # refuses one outright — and it runs BEFORE the existence check, so an already-existing `silver`
        # answers 400, not the 409 this treats as the steady state. Measured in-cluster on the first run
        # register ever made: every hop dead-lettered on that 400. A missing top-level namespace is an
        # operator's problem, stated by the register call's own error rather than papered over here.
        parent_segments = segments[:-1]
        parent = delimiter.join(parent_segments) if len(parent_segments) > 1 else ""
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
            # Already registered — every redelivery after the first lands here. But "already
            # registered" is not "registered WHERE I am writing", and the difference is invisible
            # from this side. Measured live: `silver$features` was registered at
            # `s3://bind86-wh/...` (a leftover warehouse) while the mover wrote to
            # `s3://lance-catalog/...`. The 409 read as convergence, the publish that followed
            # opened the stale location, found nothing there and 500'd — and the rows this stage
            # really wrote were governed as living somewhere they did not.
            #
            # Inside the `with`: the check reuses this client rather than opening a second pool.
            _require_same_location(client, table_id, location, catalog_root, headers)
            log.info("stage_output_already_registered", extra={"table_id": table_id})
            return
    if response.status_code >= 400:
        raise RegisterError(f"catalog refused to register {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    log.info("stage_output_registered", extra={"table_id": table_id, "location": location})


class PublishOutcome(BaseModel):
    """What the catalog decided about a version the mover just wrote.

    A REFUSAL is not an error: the run committed its output and did its job, and it is the DATA that
    was refused. `failed_assertions` is what the mover needs to decide whether a person should be
    asked — structural findings are unanswerable, the rest are reviewable.
    """

    published: bool
    from_version: int | None = None
    to_version: int | None = None
    failed_assertions: list[str] = Field(default_factory=list)
    accepted: list[str] = Field(default_factory=list)


def _require_same_location(
    client: httpx.Client,
    table_id: str,
    location: str,
    catalog_root: str,
    headers: dict[str, str],
) -> None:
    """Refuse a 409 whose registration points somewhere other than where this stage wrote.

    An unreadable describe is NOT agreement: a check that cannot be made has not passed, and treating
    it as one restores the exact silence this closes.
    """
    # The CALLER's open client, not a second one: a fresh `httpx.Client` re-opens the connection pool,
    # so verifying a 409 was costing a second pool inside the first one's `with`.
    try:
        described = client.post(f"/v1/table/{table_id}/describe", json={}, headers=headers)
    except httpx.HTTPError as exc:
        raise RegisterError(f"catalog unreachable verifying where {table_id!r} is registered: {exc}") from exc
    if described.status_code >= 400:
        raise RegisterError(
            f"{table_id!r} is already registered but the catalog would not say where "
            f"(HTTP {described.status_code}) — refusing to assume it matches {location!r}"
        )
    registered = str((described.json() or {}).get("location") or "")
    expected = f"{catalog_root.rstrip('/')}/{location.lstrip('/')}"
    if registered.rstrip("/") != expected.rstrip("/"):
        raise RegisterError(
            f"{table_id!r} is registered at {registered!r} but this stage wrote {expected!r} — "
            "the catalog governs a different copy of this table. Re-point the registration (deregister "
            "then register at the written location) rather than letting the two drift."
        )


def publish_stage_output(
    *,
    catalog_url: str,
    table_id: str,
    version: int,
    key_column: str,
    required_columns: Sequence[str] = (),
    accept_assertions: Sequence[str] = (),
    token: str | None = None,
    app_token: str | None = None,
    service_identity: str | None = None,
    dedicated_token: Callable[[str], str | None] | None = None,
    timeout_seconds: float = 30.0,
    gate_only: bool = False,
    cascade_id: str = "",
    client: httpx.Client | None = None,
) -> PublishOutcome:
    """Ask the catalog to gate `version` and, if it passes, advance the `published` tag.

    With ``gate_only`` it asks for the VERDICT and nothing else: the same assertions on the same
    version, `published` false and the tag untouched. That is what lets a caller decide a promotion
    review BEFORE promoting — under a publish-driven cascade the tag move IS the promotion, so a
    review that runs after it has nothing left to withhold, and one that runs before it cannot name
    the assertions unless it can ask first. Same door and same rung either way: only a caller who
    could publish has any business asking whether this door would accept a version.

    THE OTHER HALF OF REGISTERING. A commit makes the output readable; this is what makes it READY,
    and it is the catalog's operation so that every writer — this mover, a Ray job, a backfill —
    publishes identically and meets the same rung and the same assertions.

    It replaces the mover's own gate rather than joining it. Both ran the same checks; the local one
    withheld only the next TRIGGER, so a refused batch was already committed into the tier and visible
    to anyone reading `latest`. Only the tag is a boundary.

    Raises on an unreachable or refusing catalog — an outage or a 403 is not a data verdict, and
    reading one as a quality refusal would report a governance failure as a bad batch.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot publish its output table")
    headers = _credential(token=token, app_token=app_token, service_identity=service_identity, dedicated_token=dedicated_token)
    body = {
        "version": version,
        "key_column": key_column,
        "required_columns": list(required_columns),
        "accept_assertions": list(accept_assertions),
        "gate_only": gate_only,
        # Echoed by the catalog onto `table_published`, which is the ONE hop where a batch identity
        # would otherwise be lost — the publication head mints the next token from the event id.
        "cascade_id": cascade_id,
    }
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            response = client.post(f"/v1/table/{table_id}/publish", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable publishing {table_id!r}: {exc}") from exc
    if response.status_code >= 400:
        raise RegisterError(f"catalog refused the publish of {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    payload = response.json()
    return PublishOutcome(
        published=bool(payload.get("published")),
        from_version=payload.get("from_version"),
        to_version=payload.get("to_version"),
        failed_assertions=[a["assertion"] for a in payload.get("assertions") or [] if not a.get("success")],
        accepted=list(payload.get("accepted") or []),
    )


@contextmanager
def _catalog_client(catalog_url: str, timeout_seconds: float, client: httpx.Client | None) -> Iterator[httpx.Client]:
    """The shared client when the caller has one, otherwise a per-call client it owns.

    `fastapi` -> `production-patterns.md` § Lifespan wants one client built once and injected; the
    mover's lifespan now builds it. The fallback is not laziness — every OTHER caller of these helpers
    (the tests, `scripts/`, any direct use) has no app and no lifespan, and making the client mandatory
    would break them to satisfy a rule about the hot path. A caller that passes one must keep owning
    it: closing it here would shut the app's client after the first stage.
    """
    if client is not None:
        yield client
        return
    with httpx.Client(base_url=catalog_url.rstrip("/"), timeout=timeout_seconds) as owned:
        yield owned


def ensure_stage_output(
    *,
    catalog_url: str,
    table_id: str,
    schema: pa.Schema,
    delimiter: str = "$",
    token: str | None = None,
    app_token: str | None = None,
    service_identity: str | None = None,
    dedicated_token: Callable[[str], str | None] | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> str:
    """Ask the catalog where this stage's output lives, creating the table if it does not exist yet.

    THE MOVER ASKS INSTEAD OF TELLING, which is rule I2 applied to the write side. `transform.py` says
    the quiet part: I2 was "read from the consuming end. Only the READ side: the mover still owns where
    it WRITES." That half is the defect. The mover composed `{root}/medallion/{tier}` — a layout the
    catalog has never vended — wrote there, and then registered that path. The catalog's binding said
    somewhere else, so the publish that followed opened the catalog's answer and found nothing.

    The shape is the ingest plane's, which has been doing this correctly all along
    (`ingest.catalog_service.CatalogServiceClient.ensure`): describe, create when absent, and take the
    location from the CREATE's own response rather than re-asking a read door — `describe` answers 403
    for an absent table, so believing it is what made a new table impossible to create at all.

    ``schema`` only has to be A schema, not the output's: the empty table exists so the catalog mints
    and governs a URI, and the mover's `overwrite` replaces the schema wholesale afterwards. A stage
    does not know its output schema until it has computed, and does not need to.

    Never falls back to a composed path. A catalog that vends no location is an error — guessing one
    is precisely the defect this replaces.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot resolve where to write")
    headers = _credential(token=token, app_token=app_token, service_identity=service_identity, dedicated_token=dedicated_token)
    segments = table_id.split(delimiter)
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            described = client.post(f"/v1/table/{table_id}/describe", json={}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable resolving {table_id!r}: {exc}") from exc
        if described.status_code == 200:
            return _vended(described, table_id)

        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, schema) as writer:
            writer.write_table(schema.empty_table())
        try:
            created = client.post(
                f"/v1/table/{table_id}/create",
                content=sink.getvalue().to_pybytes(),
                headers={**headers, "Content-Type": "application/vnd.apache.arrow.stream", "x-lance-table-id": delimiter.join(segments)},
            )
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable creating {table_id!r}: {exc}") from exc
        if created.status_code >= 400:
            raise RegisterError(f"catalog refused to create {table_id!r}: HTTP {created.status_code} — {created.text[:300]}")
        return _vended(created, table_id)


def _vended(response: httpx.Response, table_id: str) -> str:
    """The location the catalog stated, or an error naming that it stated none."""
    payload = response.json() or {}
    location = str(payload.get("table_uri") or payload.get("location") or "")
    if not location:
        raise RegisterError(f"the catalog returned no location for {table_id!r} — refusing to compose one, which is the defect this call replaces")
    return location
