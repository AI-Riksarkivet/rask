"""Per-table credentials for the maintenance plane, from the catalog's vending door ([[LH-473]]).

Version expiry deletes files: ``cleanup_old_versions`` removes superseded manifests and the data files only they
reference, and disarming commit-path cleanup commits a config change. Both are writes, so what signs them is a
``maintain``-tier credential scoped to ONE table prefix, expiring in 900s, issued against an audited decision
(`vend_credentials` records who asked, for what table, at what tier). That tier opens to this service's identity alone
(``can_vend_maintain``, ADR 0156 C4).

**NO CACHE, deliberately.** Maintenance vends once per dataset per pass, and only for a dataset that has something to
expire (``table_pass._probe_before_vending``); a cache would hold credentials for datasets a pass has finished with.

**WHAT MAINTENANCE'S OWN SESSION SIGNS, stated rather than glossed:** the reads a pass makes before it writes (the
table's claim, its bucket's ``__manifest``, the policies, its datasets and its due evaluation) and the audit's scans.
Those are READS. A rewrite is never signed by anything but the vend for its table ([[LH-219]]).

EVERY ANSWER THAT IS NOT A VEND STOPS THE DATASET, because the only other credential is maintenance's own, whose reach
is the whole estate. A deployment with no vending door, or a door that vends no direct credential (``mode_b``), refuses
every rewrite (`VendingNotDirect`), by design. A door that could not be asked, answered a body that names nothing to
sign with, throttled, or answered 5xx could not decide (`VendUndecided`): the dataset is retried at its next pass, because the
catalog's own fail-closed refusals are 5xx too ([[LH-279]]). An answer refuses: a 401 about this service's own credential
(`MaintenanceUnauthenticated`), and three about the id — a 403 or any other 4xx (`MaintenanceDenied`), a 404 naming no
table or namespace (`TableNotGoverned`), or a location that does not cover the dataset (`GovernedElsewhere`).

THE ESTATE'S GRANTS ARE THE CATALOG'S TO WRITE ([[XC-084]]): maintenance holds no OpenFGA credential, since one could write
any tuple, so its drift report reads the store's grant census through the catalog (:func:`grant_census`) and its repair
and rebuild ask the catalog to revoke a ghost's grants (:func:`revoke_ghosts`) and to write a warehouse's tenancy edge
(:func:`rebuild_tenancy`), each candidate judged again on the catalog's own registries.

THE TRASH IS THE CATALOG'S TO END, record by record ([[XC-084]]): maintenance asks it to release a drop past its deadline
(:func:`release_trash`), to vend the session that deletes one trashed table's bytes (:func:`reclaim_options_for`), to
end a record whose bytes are gone (:func:`end_trash`), and to note a refusal of its own (:func:`note_trash_refusal`).
Each door answers in the same vocabulary, so a refusal there stops the record as a refused vend stops a dataset.

THE CREDENTIAL MAINTENANCE PRESENTS IS THE POD'S OWN SERVICE-ACCOUNT TOKEN, projected by the kubelet with audience
`rask-catalog` and read from the file on EVERY call (:func:`service_headers`): the kubelet rewrites it at ~515 s of a
600 s lifetime (measured, LH-220 probe d). An unreadable token stops the dataset as unauthenticated and nothing is sent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, Literal

import httpx
from lance_namespace.errors import ErrorCode
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lakehouse.base_refs import decoded_path, normalise
from lakehouse.governed import fga
from lakehouse.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer
from lakehouse.storage_options import StorageOptions
from lakehouse.trash import TrashKind


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings


logger = logging.getLogger(__name__)

#: Short. The door does a describe, a manifest read and an STS exchange; a maintenance tick that
#: blocks longer than this on ONE credential retries the dataset next tick rather than stall the
#: datasets behind it.
_TIMEOUT_SECONDS = 10.0

#: The 404 codes that answer about the TABLE rather than the request: spec.yaml `ErrorResponse.code` 1 and 4 name an
#: absence, where 13 is the malformed request and 0 a path no door serves.
_ABSENT: Final = frozenset({ErrorCode.NAMESPACE_NOT_FOUND, ErrorCode.TABLE_NOT_FOUND})


class MaintenanceDenied(RuntimeError):
    """The catalog answered NO — this identity may not maintain this table.

    THE CONDITIONS ARE DIFFERENT QUESTIONS, AND NONE PERMITS SIGNING WITHOUT A VEND:

        401 / 403 / another 4xx       the answer is NO    -> refuse. Doing the work anyway with maintenance's own
                                                             session is an authorization bypass.
        no door / no direct vend      nothing to sign     -> refuse (:class:`VendingNotDirect`).
        timeout / unparseable / 5xx   it could not DECIDE -> stop and retry (:class:`VendUndecided`).

    MEASURED ON THE LIVE ESTATE 2026-09-10: a client classifying on `status_code >= 400` took the outage branch on a
    403, and eight refused tables were then maintained under the root credential, visible only as an INFO line.
    """


class VendingNotDirect(MaintenanceDenied):
    """No direct credential is vended to this deployment: no vending door is configured, or the door answers ``server_mediated``.

    About the deployment, not the table: under ``mode_b`` every table gets this answer, so it is never counted under a
    table's id. Maintenance refuses every rewrite then, by design ([[LH-219]]): the one other credential it holds is its
    own session, which reads the whole estate and writes only its own records.
    """


class MaintenanceUnauthenticated(MaintenanceDenied):
    """A door answered 401, or maintenance could not present its own service credential at all.

    About this service, not the table: a broken, unmapped or unreadable token is refused at every table alike, so it is
    never counted under a table's id. No fallback, because the ambient key would sign what the catalog has not
    authorized.
    """


class GovernedElsewhere(MaintenanceDenied):
    """The catalog vended a credential for this dataset's table id at a location that does not cover the dataset.

    [[LH-141]]: the ``__manifest`` the sweep read named the table at this location, and by the vend it lives elsewhere: a
    rename or a re-registration landed between the two reads. The vend door answered 200 and the table it names is
    healthy, so this is maintenance's own refusal of the DATASET, never counted under the table's id.
    """


class VendUndecided(RuntimeError):
    """A catalog door could not be asked, or was asked and decided nothing: unreachable, a 5xx, a 429, or a body naming
    no answer.

    Neither a credential nor a refusal: the dataset stops as a failure and the next tick retries it. Never maintenance's
    own session: the catalog answers 503 when a table's base record cannot be read ([[LH-279]]), and signing anyway is the
    bypass of the refusal the door would have given.
    """


class TableNotGoverned(RuntimeError):
    """A catalog door answered 404 TableNotFound or NamespaceNotFound for the id it was asked about: a dataset's table, or
    the object a trash record keeps (purged or undropped since it was read).

    An answer about the id, not the request (spec.yaml:2413 and :2416). With authorization on, the catalog's gate runs
    before existence resolution, so an id that holds no tuples is answered 403 and parks as ``vend_denied`` instead
    (measured 2026-09-26, OIDC and FGA on). This 404 then means a table trashed after the tick planned it, a grant that
    outlived its table, or a declared table never written. It stops the dataset for the tick, as a 403 does.
    """


def denial_remedy(*, asked_for: str, identity: str) -> str:
    """The sentence a maintenance 403 ends with, naming BOTH causes, because the sweep cannot tell them apart.

    The catalog's authorization gate runs BEFORE existence resolution, so "this identity holds no `can_maintain`" and
    "no such table exists" are the SAME 403 on the wire. Measured 2026-09-15: a refusal advised *"Grant can_maintain on
    table:lakehouse$silver"*, and `lakehouse$silver` is a NAMESPACE.
    """
    return (
        f"Either {identity!r} holds no can_maintain on table:{asked_for}, or no such table is registered "
        f"and this dataset is ungoverned — the catalog answers both the same way, because its authorization "
        f"gate runs before existence resolution. Check that the id names a TABLE before granting on it."
    )


def reclaim_denial_remedy(*, identity: str) -> str:
    """The sentence a 403 at a trash door ends with: the estate rung every one of them asks."""
    return (
        f"{identity!r} holds no can_reclaim_trash on the estate root: the chart's bootstrap-admin hook grants "
        f"maintenance's ServiceAccount the reclaimer role (lance.fgaEstateRoles); check that {identity!r} is this pod's "
        "service-account subject."
    )


def reconcile_denial_remedy(*, identity: str) -> str:
    """The sentence a 403 at a grant door ends with: the estate rung every one of them asks."""
    return (
        f"{identity!r} holds no can_reconcile_grants on the estate root: the chart's bootstrap-admin hook grants "
        f"maintenance's ServiceAccount the reconciler role (lance.fgaEstateRoles); check that {identity!r} is this pod's "
        "service-account subject."
    )


def unauthenticated_remedy(*, asked_for: str, identity: str) -> str:
    """The sentence a maintenance 401 carries: what it was asked about, and the credential to repair."""
    return (
        f"the catalog did not accept the service credential of {identity!r} when asked about {asked_for} (401) — this is "
        f"maintenance's own identity, not the table: check its projected rask-catalog token (audience rask-catalog, "
        f"signed by the catalog's RASK_SA_ISSUER) and that {identity!r} is this pod's service-account subject."
    )


def service_headers(settings: MaintenanceSettings) -> dict[str, str]:
    """The `Authorization` header carrying this pod's `rask-catalog` token, read now.

    Raises `MaintenanceUnauthenticated` when the projected file cannot be read or is empty.
    """
    try:
        return identity_bearer(settings.catalog_identity_token_file)
    except IdentityTokenUnavailableError as exc:
        raise MaintenanceUnauthenticated(f"maintenance cannot present its catalog credential: {exc}") from exc


def _problem(response: httpx.Response) -> tuple[int | None, str]:
    """The door's RFC 9457 lance-namespace ``code`` and ``detail``; the head of the body stands in for a missing detail."""
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:200]
    if not isinstance(body, dict):
        return None, response.text[:200]
    code, detail = body.get("code"), body.get("detail")
    return (code if isinstance(code, int) else None), (str(detail) if detail is not None else response.text[:200])


def table_not_governed(response: httpx.Response, *, asked_for: str) -> TableNotGoverned | None:
    """`TableNotGoverned` when ``response`` is a 404 naming the table or namespace absent; ``None`` for any other answer."""
    if response.status_code != 404:
        return None
    code, detail = _problem(response)
    if code is None or code not in _ABSENT:
        return None
    return TableNotGoverned(f"the catalog answered 404 {ErrorCode(code).name} (code {code}) for {asked_for}: {detail}")


def table_in_invalid_state(response: httpx.Response, *, asked_for: str) -> MaintenanceDenied | None:
    """`MaintenanceDenied` when ``response`` is the catalog's 409 ``InvalidTableState`` (code 19); ``None`` otherwise.

    [[LH-279]] The catalog answers 19 for a table whose manifest declares a base it never sanctioned, and a credential
    for it would carry READ on the base. So it stops the dataset exactly as a 403 does, never reaching the "unavailable"
    arm whose fallback signs with the ambient key.
    """
    if response.status_code != 409:
        return None
    code, detail = _problem(response)
    if code != ErrorCode.INVALID_TABLE_STATE:
        return None
    return MaintenanceDenied(f"the catalog refused {asked_for} as in an invalid state (409 code {code}): {detail}")


class Vended(BaseModel):
    """What the catalog handed back: the scoped options AND where it says that table lives.

    THE LOCATION WAS ALWAYS IN THE RESPONSE AND WAS ALWAYS DISCARDED. `CredentialResponse` carries
    `location` — the catalog's authoritative answer for the table id it was asked about — and this
    module read only `credentials.storage_options`. Keeping it is what makes [[LH-141]]'s crossing
    check cost nothing: the question "does this credential cover the dataset I am holding" is answered
    by a field already on the wire, not by a second round trip the sweep cannot afford (the tick walks
    552 datasets).

    ``None`` where the door named none. A catalog that does not say is not a catalog that disagrees.

    The options hold the key pair as ``S3KeyPair``, its secret and session token ``SecretStr`` ([[LH-072]], [[LH-543]]), so a
    repr, a log line or an error that echoes a vend carries neither; only the call that signs puts them in clear.
    """

    model_config = ConfigDict(frozen=True)

    #: The storage options the door answered; their key pair is ``None`` where the answer carried no key id and secret
    #: to sign with (``StorageOptions.from_flat``).
    storage_options: StorageOptions
    location: str | None


def write_options_for(uri: str, settings: MaintenanceSettings, *, table_id: str) -> StorageOptions:
    """The storage options this dataset's REWRITE is signed with: the catalog's vend for its table, and nothing else.

    ``table_id`` is the table the catalog's ``__manifest`` names the dataset as, or whose branch it is: maintenance names
    a dataset through nothing else ([[LH-520]]), and a dataset no row names is never handed here.

    One more condition is checked on the way OUT, because it needs the catalog's answer: the vended
    credential must COVER the dataset being maintained. See
    :func:`_refuse_a_credential_that_does_not_cover` — a credential scoped to another table's location
    is not a weaker credential, it is a credential for a different tenant's data.

    Raises:
        VendingNotDirect: The deployment has no vending door, or the door vends no direct credential.
        MaintenanceDenied: The door refused (with its ``MaintenanceUnauthenticated`` and ``GovernedElsewhere``).
        TableNotGoverned: The door names no such table or namespace.
        VendUndecided: The door could not be asked or decided nothing; the next tick retries the dataset.
    """
    vended = _vended_from(_answered(settings, _maintain_door(table_id, settings)), asked_for=table_id)
    # AFTER the vend and BEFORE handing it over: the catalog's answer is what says where this table
    # lives, so the crossing is only detectable once there is an answer to read.
    _refuse_a_credential_that_does_not_cover(uri=uri, table_id=table_id, location=vended.location)
    logger.info("write credential SCOPED for %s — this pass is signed by a table-scoped credential", table_id)
    return _at_own_store(vended.storage_options, settings.s3_endpoint)


def _at_own_store(options: StorageOptions, endpoint: str) -> StorageOptions:
    """The vended credential, used at maintenance's own store address ([[LH-489]]).

    The catalog vends the address a client off the cluster reaches the store at, which may be the store's
    public edge; maintenance runs in the cluster and reaches the store where its own settings say. The key, the
    scope and the encryption stay the vend's. A vend naming no endpoint is AWS proper and is used as vended.
    """
    if options.endpoint is None:
        return options
    return options.with_endpoint(endpoint)


def _refuse_a_credential_that_does_not_cover(*, uri: str, table_id: str, location: str | None) -> None:
    """Stop the dataset when the catalog's location for ``table_id`` is not the dataset being maintained.

    [[LH-141]]. The sweep names a dataset by the ``__manifest`` it read when the tick planned, and a rename or a
    re-registration landing before the vend can leave that name on another table's location. The vend door answers
    200 to such a request, correctly, because it is asked about a table and not about the dataset the caller holds.

    CONTAINMENT, NOT EQUALITY, and getting that backwards would refuse the healthy majority: a branch
    dataset is swept at ``<root>/tree/<branch>`` while the catalog answers with the root, and the live
    sweep holds several. The catalog answers the URL ``describe_table`` does, which percent-encodes a registered
    location's non-ASCII characters (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``), so it is read as the key it decodes to
    (``base_refs.decoded_path``), while ``uri`` is the key discovery lists and every lane holds
    (``…/räksmörgås``): a compare of the spellings would refuse every such table on every tick ([[LH-319]]).
    Re-implementing either reading here is what ``base_refs.normalise``'s docstring names as the failure
    indistinguishable from having no guard at all.

    AN UNVERIFIABLE VEND PROCEEDS, loudly. A missing location means the catalog did not say, not that
    it disagrees, and refusing on absence would stop maintaining the estate the day the response shape
    changed — the same asymmetry ``sweep._probe_before_vending`` argues for uncertainty.
    """
    if location is None:
        logger.warning(
            "vended credential for %s carries no location, so it cannot be checked against %s — this pass proceeds unverified",
            table_id,
            uri,
        )
        return
    governed, holding = decoded_path(location), normalise(uri)
    if holding == governed or holding.startswith(f"{governed}/"):
        return
    raise GovernedElsewhere(
        f"the dataset at {uri} was named {table_id!r} by the catalog's __manifest, which the catalog now governs at {location} — "
        "the two name different locations, so a pass here would be signed for one table and land in another's. The next tick "
        "reads the manifest again."
    )


class _VendedCredentials(BaseModel):
    """The ``credentials`` of the door's answer: the flat map Lance takes ([[LH-543]])."""

    #: An answer that does not validate is refused without echoing its values, which carry the secret.
    model_config = ConfigDict(hide_input_in_errors=True)

    storage_options: dict[str, str]


class _VendAnswer(BaseModel):
    """A vend door's 2xx body, as much of the catalog's ``CredentialResponse`` as maintenance reads."""

    model_config = ConfigDict(hide_input_in_errors=True)

    mode: str
    credentials: _VendedCredentials | None = None
    location: str | None = None


class Door(BaseModel):
    """One of the catalog's doors maintenance asks, and how a refusal there reads."""

    model_config = ConfigDict(frozen=True)

    method: Literal["GET", "POST", "DELETE"] = "POST"
    #: The door's path under the catalog's address.
    path: str
    params: dict[str, str] = Field(default_factory=dict)
    body: dict[str, object] | None = None
    #: What the door hands over, as a refusal names it: ``a write credential``, ``a reclaim session``, ...
    answer: str
    #: What it is asked about: a table id, or a trash record's ``<kind>:<uuid>``.
    asked_for: str
    #: The sentence a 403 there ends with.
    denial: str


class TrashReleased(BaseModel):
    """What the catalog's release of a trash record did: the grants it revoked, and whether it stamped the record."""

    tuples_revoked: int
    stamped: bool


class _TrashEnded(BaseModel):
    tuples_revoked: int


class _GrantCensusAnswer(BaseModel):
    enabled: bool
    counts_by_type: dict[str, dict[str, int]] = Field(default_factory=dict)
    truncated: str | None = None


class GhostsRevoked(BaseModel):
    """What the catalog revoked of the ghosts maintenance named, each object's tuple count, and what it refused, with why."""

    revoked: dict[str, int] = Field(default_factory=dict)
    refused: dict[str, str] = Field(default_factory=dict)


class TenancyRebuilt(BaseModel):
    """The tenancy edges the catalog wrote, and the warehouses whose record justifies none, with why."""

    written: list[str] = Field(default_factory=list)
    unjustified: dict[str, str] = Field(default_factory=dict)


class _TrashNoted(BaseModel):
    attempts: int | None = None


def reclaim_options_for(kind: TrashKind, uuid: str, settings: MaintenanceSettings) -> StorageOptions:
    """The session that deletes the bytes of the trashed table ``uuid`` names: the catalog's vend for that record, and
    nothing else ([[XC-084]]). It lists and deletes at the table's location, in both spellings, and in its own data
    directories, and reaches nothing on the control root.

    Raises:
        VendingNotDirect: No vending door is configured, or it vends no direct credential.
        MaintenanceDenied: The door refused: this identity holds no reclaim rung (403), or the catalog's reads find the
            record not due, naming no bytes, at a place another table or none holds (409 code 19, noted on the record).
        TableNotGoverned: No trash record holds the uuid: purged or undropped since it was read.
        VendUndecided: The door could not be asked or decided nothing; the next tick retries the record.
    """
    door = _trash_door(kind, uuid, settings, act="/credentials", answer="a reclaim session")
    return _at_own_store(_vended_from(_answered(settings, door), asked_for=door.asked_for).storage_options, settings.s3_endpoint)


def release_trash(kind: TrashKind, uuid: str, settings: MaintenanceSettings) -> TrashReleased:
    """Have the catalog release the trash record of the ``kind`` object ``uuid`` names: revoke the object's grants, tell
    each person who lost one, and stamp the record released.

    Raises:
        VendingNotDirect: No catalog door is configured.
        MaintenanceDenied: The door refused: this identity holds no reclaim rung (403), or the record's deadline has not
            settled or a live object holds it again (409 code 19, noted on the record).
        TableNotGoverned: No trash record holds the uuid.
        VendUndecided: The door could not be asked or decided nothing.
    """
    door = _trash_door(kind, uuid, settings, act="/release", answer="a release")
    return _parsed(_answered(settings, door), TrashReleased, asked_for=door.asked_for)


def end_trash(kind: TrashKind, uuid: str, settings: MaintenanceSettings) -> int:
    """Have the catalog end the trash record of the ``kind`` object ``uuid`` names, its bytes gone, and return how many
    grants it still held and revoked.

    Raises:
        VendingNotDirect: No catalog door is configured.
        MaintenanceDenied: The door refused: this identity holds no reclaim rung (403), or the record's place still holds
            bytes, or another table's or none (409 code 19, noted on the record).
        TableNotGoverned: No trash record holds the uuid.
        VendUndecided: The door could not be asked or decided nothing.
    """
    door = _trash_door(kind, uuid, settings, act="", answer="the end of the record", method="DELETE")
    return _parsed(_answered(settings, door), _TrashEnded, asked_for=door.asked_for).tuples_revoked


def note_trash_refusal(kind: TrashKind, uuid: str, settings: MaintenanceSettings, *, reason: str) -> int | None:
    """Have the catalog note maintenance's own refusal on the trash record, and return the record's new attempt count.

    Raises:
        VendingNotDirect: No catalog door is configured.
        MaintenanceDenied: The door refused (403).
        TableNotGoverned: No trash record holds the uuid.
        VendUndecided: The door could not be asked or decided nothing.
    """
    door = _trash_door(kind, uuid, settings, act="/refusals", answer="a note of its refusal", body={"reason": reason})
    return _parsed(_answered(settings, door), _TrashNoted, asked_for=door.asked_for).attempts


def grant_census(settings: MaintenanceSettings) -> fga.GrantCensus | None:
    """The store's grants counted by object, as the catalog reads them for the drift report; ``None`` when the catalog
    answers that authorization is off.

    Raises:
        VendingNotDirect: No catalog door is configured.
        MaintenanceDenied: The door refused: this identity holds no reconciler rung (403).
        VendUndecided: The door could not be asked or decided nothing.
    """
    door = _grants_door(settings, act="/census", answer="a grant census", method="GET")
    answer = _parsed(_answered(settings, door), _GrantCensusAnswer, asked_for=door.asked_for)
    return fga.GrantCensus(counts_by_type=answer.counts_by_type, truncated=answer.truncated) if answer.enabled else None


def revoke_ghosts(settings: MaintenanceSettings, objects: list[str]) -> GhostsRevoked:
    """Have the catalog revoke every grant on each of ``objects``, which the drift report found no registry naming.

    Raises:
        VendingNotDirect: No catalog door is configured.
        MaintenanceDenied: The door refused: this identity holds no reconciler rung (403).
        VendUndecided: The door could not be asked or decided nothing.
    """
    door = _grants_door(settings, act="/ghosts/revoke", answer="a ghost's revoke", body={"objects": objects})
    return _parsed(_answered(settings, door), GhostsRevoked, asked_for=door.asked_for)


def rebuild_tenancy(settings: MaintenanceSettings, warehouses: list[str]) -> TenancyRebuilt:
    """Have the catalog write the tenancy edge each of ``warehouses``' registry record justifies.

    Raises:
        VendingNotDirect: No catalog door is configured.
        MaintenanceDenied: The door refused: this identity holds no reconciler rung (403).
        VendUndecided: The door could not be asked or decided nothing.
    """
    door = _grants_door(settings, act="/tenancy/rebuild", answer="a tenancy rebuild", body={"warehouses": warehouses})
    return _parsed(_answered(settings, door), TenancyRebuilt, asked_for=door.asked_for)


def _maintain_door(table_id: str, settings: MaintenanceSettings) -> Door:
    """The ``maintain``-tier vend door for ``table_id``.

    ``params``, NOT a body: the door declares ``tier: Annotated[Tier, Query()] = "read"``, and FastAPI ignores an unknown
    body on a query parameter, so a body-borne tier came back READ with a 200 and the write then failed at the object
    store as ``403 AccessDenied`` on a PUT (measured in-cluster 2026-09-03 on the ingest client). ``maintain`` is the
    tier ``can_vend_maintain`` opens to this identity alone ([[LH-202]], [[LH-473]]): a disarm commits a version and a
    reclaim deletes versions, and the writer tier reaches ``data/`` only.
    """
    return Door(
        path=f"/management/v1/table/{table_id}/credentials",
        params={"tier": "maintain"},
        answer="a write credential",
        asked_for=table_id,
        denial=denial_remedy(asked_for=table_id, identity=settings.catalog_service_identity),
    )


def _trash_door(
    kind: TrashKind,
    uuid: str,
    settings: MaintenanceSettings,
    *,
    act: str,
    answer: str,
    method: Literal["POST", "DELETE"] = "POST",
    body: dict[str, str] | None = None,
) -> Door:
    """The trash door ``act`` names for the record of the ``kind`` object ``uuid`` names."""
    return Door(
        method=method,
        path=f"/management/v1/trash/{kind}/{uuid}{act}",
        body=body,
        answer=answer,
        asked_for=f"{kind}:{uuid}",
        denial=reclaim_denial_remedy(identity=settings.catalog_service_identity),
    )


def _grants_door(
    settings: MaintenanceSettings, *, act: str, answer: str, method: Literal["GET", "POST"] = "POST", body: dict[str, object] | None = None
) -> Door:
    """The grant door ``act`` names."""
    return Door(
        method=method,
        path=f"/management/v1/grants{act}",
        body=body,
        answer=answer,
        asked_for="the estate's grants",
        denial=reconcile_denial_remedy(identity=settings.catalog_service_identity),
    )


def _answered(settings: MaintenanceSettings, door: Door) -> httpx.Response:
    """The door's 2xx answer, presented as this service's own identity.

    Raises:
        VendingNotDirect: No catalog door is configured (``MAINTENANCE_CATALOG_URL`` is empty), or the path serves none.
        MaintenanceUnauthenticated: This pod's catalog token cannot be read, so nothing is sent; or the door answered 401.
        MaintenanceDenied: The door refused.
        TableNotGoverned: The door names what it was asked about absent.
        VendUndecided: The door could not be asked: narrow on purpose, since a blanket catch reports a ``NameError`` here
            as "vending unavailable"; or it decided nothing.
    """
    if not settings.catalog_url:
        raise VendingNotDirect(f"no catalog door is configured (MAINTENANCE_CATALOG_URL is empty), so {door.answer} for {door.asked_for} cannot be asked for")
    headers = service_headers(settings)
    try:
        response = httpx.request(
            door.method, f"{settings.catalog_url.rstrip('/')}{door.path}", params=door.params, json=door.body, headers=headers, timeout=_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise VendUndecided(f"the catalog's door could not be asked for {door.answer} for {door.asked_for}: {exc}") from exc
    if (refusal := _refusal_of(response, door, identity=settings.catalog_service_identity)) is not None:
        raise refusal
    return response


def _refusal_of(response: httpx.Response, door: Door, *, identity: str) -> MaintenanceDenied | TableNotGoverned | VendUndecided | None:
    """The refusal or failure a non-2xx answer is, and ``None`` for a 2xx, whose body says the rest.

    Every one stops what it was asked for: a 403 is the catalog saying this identity may not, and doing it anyway under
    maintenance's own session would be the bypass. A 404 names what was asked about absent (``TableNotGoverned``) or
    names nothing, a path no door serves; a 409 code 19 is the catalog's refusal of the object as it stands ([[LH-279]],
    [[XC-084]]); a 5xx is the door failing to decide (the catalog answers 503 when a record it must judge cannot be read)
    and a 429 one that shed the request, both retried next tick.
    """
    if response.is_success:
        return None
    if response.status_code == 401:
        return MaintenanceUnauthenticated(f"no {door.answer} given: {unauthenticated_remedy(asked_for=door.asked_for, identity=identity)}")
    if response.status_code == 403:
        return MaintenanceDenied(f"the catalog REFUSED {door.answer} for {door.asked_for} (403) — this pass is not authorized. {door.denial}")
    if (absent := table_not_governed(response, asked_for=door.asked_for)) is not None:
        return absent
    code, detail = _problem(response)
    if response.status_code == 404:
        return VendingNotDirect(f"the catalog serves no door for {door.answer} for {door.asked_for} (404, code {code}): {detail}")
    if (invalid := table_in_invalid_state(response, asked_for=door.asked_for)) is not None:
        return invalid
    if response.status_code >= 500 or response.status_code == 429:
        return VendUndecided(f"the catalog could not decide {door.answer} for {door.asked_for} ({response.status_code}): {detail}")
    return MaintenanceDenied(f"the catalog refused {door.answer} for {door.asked_for} ({response.status_code}, code {code}): {detail}")


def _parsed[AnswerT: BaseModel](response: httpx.Response, model: type[AnswerT], *, asked_for: str) -> AnswerT:
    """The door's 2xx body as ``model``.

    Raises:
        VendUndecided: The body is not the answer the door gives.
    """
    try:
        return model.model_validate_json(response.content)
    except ValidationError as exc:
        raise VendUndecided(f"the catalog answered for {asked_for} with a body that is no {model.__name__} answer") from exc


def _vended_from(response: httpx.Response, *, asked_for: str) -> Vended:
    """The credential a 2xx answer vends, or why it vends none.

    A body that is no answer, half a credential, two spellings naming two identities, or a map with no key pair at all is
    no vend: read as a credential it would leave the pass to sign with whatever chain the process holds.

    Raises:
        VendingNotDirect: The answer is not ``direct`` (``server_mediated`` under ``mode_b``).
        VendUndecided: The body names no whole credential to sign with.
    """
    answer = _parsed(response, _VendAnswer, asked_for=asked_for)
    if answer.mode != "direct":
        raise VendingNotDirect(f"the catalog vends no direct credential for {asked_for} (mode {answer.mode!r}), so maintenance has nothing to sign with")
    if answer.credentials is None:
        raise VendUndecided(f"the catalog's direct vend for {asked_for} carries no credentials")
    try:
        storage_options = StorageOptions.from_flat(answer.credentials.storage_options)
    except ValueError as exc:
        raise VendUndecided(f"the catalog's vend for {asked_for} names a credential that cannot be signed with: {exc}") from exc
    if storage_options.key_pair is None:
        raise VendUndecided(f"the catalog's vend for {asked_for} names no key pair to sign with")
    return Vended(storage_options=storage_options, location=answer.location or None)
