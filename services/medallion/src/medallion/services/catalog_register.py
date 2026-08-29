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

THE MOVER ASKS THE CATALOG, IT DOES NOT TELL IT — and this paragraph said the opposite for as long
as that was true. It read "Register — not create-through-the-catalog. The mover owns where it
WRITES", which described the ORIGINAL `register_stage_output` shape: compose `{root}/medallion/
{tier}`, write there, then register the path after the fact. That ordering was the defect (I2 on the
write side): the catalog's binding said somewhere else, so the publish that followed opened the
catalog's answer and found nothing. `ensure_stage_output` — the seam every mover calls now — DOES
create through the catalog's own door (`POST /v1/table/{id}/create` when `describe` 404s) and takes
the location from that response, and `transform.py` calls it BEFORE the write. Registration is still
the goal; asking first is how the goal is reached.

TWO DOORS, AND WHICH ONE A WRITER USES IS DECIDED BY WHO OWNS ITS LOCATION. A MOVER asks
(`ensure_stage_output`): nothing else names where its output lives, so the catalog's answer is the
only answer. The CASCADE HEAD tells (`register_written_dataset`): `POST /produce` writes to
`MEDALLION_BRONZE_URI`, which `chart/templates/medallion.yaml` renders from the same expression as the
bronze->silver mover's `MEDALLION_FROM_URI`, and the `medallion.bronze` trigger carries no `from_uri`
for that mover to follow — so a head that took a vended location would leave the cascade's first leg
opening a path nothing writes to, with nothing red. `register_table` is the door built for exactly
that case (bytes written outside the catalog's own doors), and it needs no WAREHOUSE, which is why it
reaches the medallion path in the reserved platform bucket that no warehouse may ever claim.

This paragraph used to say the telling form was GONE, and for a while it was: `register_stage_output`
outlived its last caller when the movers' ordering was fixed, and three suites went on stubbing a door
nothing opened. What was actually wrong with it was never the direction — it was that a MOVER used it.
The form is back, once, for the one writer whose location is a deployment contract rather than a guess,
and the two lessons it paid for are kept: it mints no namespace (a top-level parent belongs to the
warehouse, and `require_warehouse_scoped` refuses one outright — measured in-cluster, every hop
dead-lettered on that 400), and a 409 is convergence only after the catalog CONFIRMS it governs the
location the caller wrote.

Ordering is unchanged either way: registration strictly precedes the first row, so there is no window
in which rows exist that the catalog has no record of.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

import httpx
import pyarrow as pa
from pydantic import BaseModel, Field

from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


log = logging.getLogger(__name__)


class RegisterError(RuntimeError):
    """The catalog refused or could not be reached — the stage must NOT report success.

    An unregistered gold table is #88's defect intact, so this propagates and the mover RETRYs.
    That re-runs the (expensive) transcribe too — stated cost: the overwrite is idempotent and a
    catalog outage is rarer than a Serve one; splitting the stage into resumable halves is P7b's
    re-cut, not a quiet retry layer here.
    """


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
    originator: str = "",
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
        # THE HUMAN THE BATCH IS FOR, across the same lost hop and for the same reason. A mover
        # authenticates to this door AS ITSELF (`_credential` above), so the control event's actor is
        # `service-<mover>` — an inbox actor named after a mover, which is worse than silence because
        # it looks delivered. The person is only in this body, and the catalog decides what to do with
        # the claim (`publication_originator`): it authorizes nothing here and the notifications plane
        # re-derives every recipient's visibility at delivery.
        "originator": originator,
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
    delimiter: str = CATALOG_DELIMITER,
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

        try:
            created = client.post(
                f"/v1/table/{table_id}/create",
                content=encode_arrow_stream(schema.empty_table()),
                headers={**headers, "Content-Type": ARROW_STREAM_MEDIA_TYPE, "x-lance-table-id": delimiter.join(segments)},
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


def relative_location(dataset_uri: str, catalog_root: str) -> str:
    """``dataset_uri`` expressed relative to the catalog's connection root, or raise naming both.

    `register_table` addresses a location INSIDE the root it is connected to and nowhere else —
    measured against the real door: an absolute path answers *"Absolute paths are not allowed for
    register_table"* (400) and an absolute URI the same. So a dataset zoned into its own bucket is
    unregisterable through this door, and that is a refusal rather than a fallback: writing bytes the
    catalog cannot name is precisely the ungoverned state the caller is trying to leave.
    """
    root = catalog_root.rstrip("/")
    if not root:
        raise RegisterError(
            f"cannot register {dataset_uri!r}: MEDALLION_CATALOG_ROOT is unset, so no location relative to the catalog's connection root can be formed"
        )
    if not dataset_uri.startswith(root + "/"):
        raise RegisterError(
            f"cannot register {dataset_uri!r}: it is not under the catalog root {catalog_root!r} "
            "(register_table addresses only paths inside the root it is connected to — a dataset zoned into "
            "another bucket cannot be governed through this door)"
        )
    return dataset_uri[len(root) + 1 :]


def register_written_dataset(
    *,
    catalog_url: str,
    catalog_root: str,
    table_id: str,
    dataset_uri: str,
    delimiter: str = CATALOG_DELIMITER,
    token: str | None = None,
    app_token: str | None = None,
    service_identity: str | None = None,
    dedicated_token: Callable[[str], str | None] | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> None:
    """Attach the dataset at ``dataset_uri`` to the catalog as ``table_id``; 409 means already governed.

    THE DOOR FOR A WRITER THAT OWNS ITS OWN LOCATION — see this module's header for why the cascade
    head is one and a mover is not. Registering is what turns written bytes into a ``table:`` object:
    it seeds the caller's FGA ownership through the catalog's own door, and every governed path —
    the maintenance policy, the protection record, trash/undrop, credential vending, the FGA doors —
    keys off that object rather than off the bytes.

    Workload-neutral by construction, like everything else here: an id and a URI. It creates no
    namespace — a top-level parent is the WAREHOUSE's to make, and asking for one is refused 400 by
    `require_warehouse_scoped` before the existence check ever runs, so a lane that tried it
    dead-lettered every hop.

    Raises :class:`RegisterError` on anything short of success, ``catalog_url`` unset included: a tier
    the catalog cannot govern must not report success.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this writer cannot register the dataset it lands")
    location = relative_location(dataset_uri, catalog_root)
    segments = table_id.split(delimiter)
    headers = _credential(token=token, app_token=app_token, service_identity=service_identity, dedicated_token=dedicated_token)
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            response = client.post(f"/v1/table/{table_id}/register", json={"id": segments, "location": location}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable registering {table_id!r}: {exc}") from exc
        if response.status_code == 409:
            # Already registered — every call after the first lands here. But "already registered" is
            # not "registered WHERE I write", and the difference is invisible from this side. Measured
            # live once: a table was registered against a leftover warehouse while its writer wrote
            # elsewhere; the 409 read as convergence, and the publish that followed opened the stale
            # location and found nothing. Inside the `with`, so the check reuses this client.
            _require_same_location(client, table_id, location, catalog_root, headers)
            log.info("written_dataset_already_registered", extra={"table_id": table_id, "location": location})
            return
        if response.status_code >= 400:
            raise RegisterError(f"catalog refused to register {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    log.info("written_dataset_registered", extra={"table_id": table_id, "location": location})


def _require_same_location(client: httpx.Client, table_id: str, location: str, catalog_root: str, headers: dict[str, str]) -> None:
    """Refuse a 409 whose registration points somewhere other than where this writer writes.

    An unreadable describe is NOT agreement: a check that cannot be made has not passed, and treating
    it as one restores the exact silence this closes.
    """
    try:
        described = client.post(f"/v1/table/{table_id}/describe", json={}, headers=headers)
    except httpx.HTTPError as exc:
        raise RegisterError(f"catalog unreachable verifying where {table_id!r} is registered: {exc}") from exc
    if described.status_code >= 400:
        raise RegisterError(
            f"{table_id!r} is already registered but the catalog would not say where (HTTP {described.status_code}) — refusing to assume it matches {location!r}"
        )
    registered = str((described.json() or {}).get("location") or "")
    expected = f"{catalog_root.rstrip('/')}/{location.lstrip('/')}"
    if registered.rstrip("/") != expected.rstrip("/"):
        raise RegisterError(
            f"{table_id!r} is registered at {registered!r} but this writer writes {expected!r} — the catalog governs a different copy of this table. "
            "Re-point the registration (deregister, then register at the written location) rather than letting the two drift."
        )
