"""Per-table credentials for the maintenance plane, from the catalog's vending door ([[LH-473]]).

Version expiry deletes files: ``cleanup_old_versions`` removes superseded manifests and the data files only they
reference, and disarming commit-path cleanup commits a config change. Both are writes, so what signs them is a
``maintain``-tier credential scoped to ONE table prefix, expiring in 900s, issued against an audited decision
(`vend_credentials` records who asked, for what table, at what tier). That tier opens to this service's identity alone
(``can_vend_maintain``, ADR 0156 C4).

**NO CACHE, deliberately.** Maintenance vends once per dataset per tick, and only for a dataset that has something to
expire (``sweep._probe_before_vending``); a cache would hold credentials for datasets this tick has finished with.

**WHAT STAYS ON THE ROOT KEY, stated rather than glossed:** discovery, the protection pre-pass and the due evaluation.
`sweep._protected_roots` must open every manifest in every bucket, because a shallow clone in bucket B is the only
thing that knows bucket A's dataset is referenced — no per-table credential can express a whole-estate read. Those are
READS. The clause this serves is that no service holds a root key on a write path, and the write path is here.

A door that could not be ASKED — unreachable, or answering a body that does not parse — degrades to the ambient
credential and says so. An answer refuses instead: a 401 about this service's own credential
(`MaintenanceUnauthenticated`), and three about the id — a 403 (`MaintenanceDenied`), a 404 naming no table or namespace
(`TableNotGoverned`), or a location that does not cover the dataset (`GovernedElsewhere`). A 5xx is the door failing to
decide (`VendUndecided`): the dataset is retried next tick, because the catalog's own fail-closed refusals are 5xx too
([[LH-279]]).

THE CREDENTIAL MAINTENANCE PRESENTS IS THE POD'S OWN SERVICE-ACCOUNT TOKEN, projected by the kubelet with audience
`rask-catalog` and read from the file on EVERY call (:func:`service_headers`): the kubelet rewrites it at ~515 s of a
600 s lifetime (measured, LH-220 probe d). An unreadable token stops the dataset as unauthenticated and nothing is sent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

import httpx
from lance_namespace.errors import ErrorCode
from pydantic import BaseModel, ConfigDict

from maintenance.core.metrics import record_credential_tier
from service_kit.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer
from service_kit.lakehouse.base_refs import decoded_path, normalise
from service_kit.lakehouse.endpoint_scheme import allow_http_for
from service_kit.lakehouse.objectfs import StorageOptions


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings


logger = logging.getLogger(__name__)

#: Short. The door does a describe, a manifest read and an STS exchange; a maintenance tick that
#: blocks longer than this on ONE credential should proceed on the ambient one rather than stall the
#: dataset behind it.
_TIMEOUT_SECONDS = 10.0

#: The 404 codes that answer about the TABLE rather than the request: spec.yaml `ErrorResponse.code` 1 and 4 name an
#: absence, where 13 is the malformed request and 0 a path no door serves.
_ABSENT: Final = frozenset({ErrorCode.NAMESPACE_NOT_FOUND, ErrorCode.TABLE_NOT_FOUND})


class MaintenanceDenied(RuntimeError):
    """The catalog answered NO — this identity may not maintain this table.

    THE CONDITIONS ARE DIFFERENT QUESTIONS AND ONLY ONE PERMITS A FALLBACK:

        timeout / unparseable         we could not ASK    -> degrade to the ambient credential.
        401 / 403                     the answer is NO    -> refuse. Doing the work anyway with the deployment's
                                                             ambient key is an authorization bypass.
        a 5xx                         it could not DECIDE -> stop and retry (:class:`VendUndecided`).

    MEASURED ON THE LIVE ESTATE 2026-09-10: a client classifying on `status_code >= 400` took the outage branch on a
    403, and eight refused tables were then maintained under the root credential, visible only as an INFO line.
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
    """The vend door answered 5xx: it was asked and could not decide whether to issue this credential.

    Neither a credential nor a refusal: the dataset stops as a failure and the next tick retries it. Never the ambient
    key: the catalog answers 503 when a table's base record cannot be read ([[LH-279]]), and signing anyway is the
    bypass of the refusal the door would have given.
    """


class TableNotGoverned(RuntimeError):
    """The vend door answered 404 TableNotFound or NamespaceNotFound for the id this dataset is addressed by.

    An answer about the id, not the request (spec.yaml:2413 and :2416). With authorization on, the catalog's gate runs
    before existence resolution, so an id that holds no tuples is answered 403 and parks as ``vend_denied`` instead
    (measured 2026-09-26, OIDC and FGA on). This 404 then means a table trashed after the tick planned it, a grant that
    outlived its table, or a declared table never written. It stops the dataset for the tick, as a 403 does.
    """


def denial_remedy(*, table_id: str, identity: str) -> str:
    """The sentence a maintenance 403 ends with, naming BOTH causes, because the sweep cannot tell them apart.

    The catalog's authorization gate runs BEFORE existence resolution, so "this identity holds no `can_maintain`" and
    "no such table exists" are the SAME 403 on the wire. Measured 2026-09-15: a refusal advised *"Grant can_maintain on
    table:lakehouse$silver"*, and `lakehouse$silver` is a NAMESPACE.
    """
    return (
        f"Either {identity!r} holds no can_maintain on table:{table_id}, or no such table is registered "
        f"and this dataset is ungoverned — the catalog answers both the same way, because its authorization "
        f"gate runs before existence resolution. Check that the id names a TABLE before granting on it."
    )


def unauthenticated_remedy(*, table_id: str, identity: str) -> str:
    """The sentence a maintenance 401 carries: the table it was asked for, and the credential to repair."""
    return (
        f"the catalog did not accept the service credential of {identity!r} when asked for {table_id} (401) — this is "
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


def table_not_governed(response: httpx.Response, *, table_id: str) -> TableNotGoverned | None:
    """`TableNotGoverned` when ``response`` is a 404 naming the table or namespace absent; ``None`` for any other answer."""
    if response.status_code != 404:
        return None
    code, detail = _problem(response)
    if code is None or code not in _ABSENT:
        return None
    return TableNotGoverned(f"the catalog answered 404 {ErrorCode(code).name} (code {code}) for {table_id}: {detail}")


def table_in_invalid_state(response: httpx.Response, *, table_id: str) -> MaintenanceDenied | None:
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
    return MaintenanceDenied(f"the catalog refused {table_id} as in an invalid state (409 code {code}): {detail}")


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


def write_options_for(uri: str, settings: MaintenanceSettings, *, table_id: str, fallback: StorageOptions) -> StorageOptions:
    """The storage options this dataset's REWRITE should be signed with.

    ``table_id`` is the table the catalog's ``__manifest`` names the dataset as, or whose branch it is: maintenance names
    a dataset through nothing else ([[LH-520]]), and a dataset no row names is never handed here. Returns the vended,
    table-scoped options when the deployment vends and the door answers ``direct``, and ``fallback`` (the deployment's
    ambient credential) when a catalog URL is unset, which means this deployment has no vending door, or when the door
    answers ``server_mediated``, a supported posture (`mode_b`) rather than a fault.

    One more condition is checked on the way OUT, because it needs the catalog's answer: the vended
    credential must COVER the dataset being maintained. See
    :func:`_refuse_a_credential_that_does_not_cover` — a credential scoped to another table's location
    is not a weaker credential, it is a credential for a different tenant's data.

    Raises ``MaintenanceDenied`` (or its ``MaintenanceUnauthenticated`` or ``GovernedElsewhere``) or
    ``TableNotGoverned`` where the door answered, and ``VendUndecided`` where it answered 5xx; each stops
    the caller's dataset, which must not fall back to ``fallback``.
    """
    if not settings.catalog_url:
        _announce_vending_is_off(table_id, key_id=settings.s3_access_key_id)
        return fallback
    vended = _vend(table_id, settings)
    if vended is None:
        # NAME THE KEY, do not rank it. "the root key" is an assertion this function cannot make:
        # `MAINTENANCE_S3_ACCESS_KEY_ID` is `rask-maintenance` on the deployed estate, a scoped
        # identity, so the line reported a posture the estate had already left — and an operator
        # reading it went looking for a hardening that was in place. Emptying the chart value is still
        # a documented way back to the tenant root, which is exactly why the identity has to be read
        # rather than guessed.
        logger.info(
            "write credential AMBIENT for %s — nothing vended; this pass is signed by the process credential %s",
            table_id,
            settings.s3_access_key_id or "<ambient environment>",
        )
        # ON THE SERIES as well as the log. Per-dataset `info` was the only signal, and an estate where
        # EVERY pass took this branch read as routine traffic for long enough to be cited as evidence
        # that something else was working.
        record_credential_tier(tier="ambient")
        return fallback
    # AFTER the vend and BEFORE handing it over: the catalog's answer is what says where this table
    # lives, so the crossing is only detectable once there is an answer to read.
    _refuse_a_credential_that_does_not_cover(uri=uri, table_id=table_id, location=vended.location)
    logger.info("write credential SCOPED for %s — this pass is signed by a table-scoped credential", table_id)
    record_credential_tier(tier="scoped")
    return _at_own_store(vended.storage_options, settings.s3_endpoint)


def _at_own_store(options: StorageOptions, endpoint: str) -> StorageOptions:
    """The vended credential, used at maintenance's own store address ([[LH-489]]).

    The catalog vends the address a client off the cluster reaches the store at, which may be the store's
    public edge; maintenance runs in the cluster and reaches the store where its own settings say. The key, the
    scope and the encryption stay the vend's. A vend naming no endpoint is AWS proper and is used as vended.
    """
    if "endpoint" not in options.options:
        return options
    return options.with_options({"endpoint": endpoint, "allow_http": allow_http_for(endpoint)})


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


#: Whether this process has already said that vending is switched off. The CONDITION is a whole-service
#: one — an empty ``catalog_url`` does not vary between datasets — so it is reported at the volume of the
#: configuration rather than of the sweep. A per-dataset line here would be thousands an hour that never
#: change, which is how § Q17-26's detector drowned the audit trail it shared.
_vending_off_announced = False


def _reset_vending_notice() -> None:
    """Test seam: the notice is once-per-PROCESS, and a test asserting that needs to start from unsaid."""
    global _vending_off_announced
    _vending_off_announced = False


def _announce_vending_is_off(subject: str, *, key_id: str) -> None:
    """Say, once, that every pass from here is signed by the root key.

    THIS BRANCH USED TO BE THE SILENT ONE, and it is the branch that covers a whole-service
    misconfiguration — so the module kept its "degrades and says so" promise for a single unreachable
    table and broke it for an entire estate running unhardened. Measured 2026-09-09: the live
    maintenance pod held ``catalog_url = ''`` (``maintenance.vendWriteCredentials`` was the chart
    default), so every pass signed with the root object-store key and the only evidence was the
    ABSENCE of a stream nobody was watching.
    """
    global _vending_off_announced
    if _vending_off_announced:
        return
    _vending_off_announced = True
    logger.warning(
        "write credential vending is NOT CONFIGURED (no catalog URL) — every pass from this process, "
        "starting with %s, is signed by the process credential %s. Set maintenance.vendWriteCredentials to scope it.",
        subject,
        key_id or "<ambient environment>",
    )


def _vend(table_id: str, settings: MaintenanceSettings) -> Vended | None:
    """One vend, or ``None``. Narrow ``except`` on purpose: a blanket catch reports a `NameError` in the
    vending method itself as "vending unavailable"."""
    url = f"{settings.catalog_url.rstrip('/')}/management/v1/table/{table_id}/credentials"
    # Read before the request: an unreadable token raises `MaintenanceUnauthenticated` and nothing is sent.
    headers = service_headers(settings)
    try:
        # `params`, NOT a body. The door declares `tier: Annotated[Tier, Query()] = "read"`, and
        # FastAPI ignores an unknown body on a query parameter — a body-borne tier came back READ with
        # a 200, and the write then failed at the object store as `403 AccessDenied` on a PUT.
        # Measured in-cluster 2026-09-03 on the ingest client, which had exactly this bug.
        # `maintain`, the tier `can_vend_maintain` opens to this identity alone ([[LH-202]], [[LH-473]]): a disarm commits a version and a reclaim
        # deletes versions, and the writer tier reaches `data/` only.
        response = httpx.post(url, params={"tier": "maintain"}, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.info("credential vending unreachable for %s (%s)", table_id, exc)
        return None
    if response.status_code == 401:
        raise MaintenanceUnauthenticated(f"no write credential vended: {unauthenticated_remedy(table_id=table_id, identity=settings.catalog_service_identity)}")
    if response.status_code == 403:
        # A DENIAL, NOT AN OUTAGE — and the difference decides whether a pass may proceed. Falling
        # back here hands the caller the deployment's ambient key, which reaches every bucket in the
        # estate, in answer to the catalog saying this identity may not write this one table. Measured
        # live 2026-09-10: eight tables were refused and rewritten under the root credential anyway,
        # visible only as an INFO line nobody reads.
        raise MaintenanceDenied(
            f"the catalog REFUSED a write credential for {table_id} ({response.status_code}) — this pass is not "
            f"authorized, and signing it with the ambient key would be a bypass. "
            f"{denial_remedy(table_id=table_id, identity=settings.catalog_service_identity)}"
        )
    if (absent := table_not_governed(response, table_id=table_id)) is not None:
        # AN ANSWER ABOUT THE ID, not an outage, so no fallback either: a trashed table, or with authorization
        # off any unregistered id.
        raise absent
    if (invalid := table_in_invalid_state(response, table_id=table_id)) is not None:
        # AN ANSWER ABOUT THE TABLE ([[LH-279]]): its manifest declares a base the catalog never sanctioned.
        # Falling through to the generic arm below would sign this pass with the ambient key — the
        # bypass of a refusal this client exists to prevent.
        raise invalid
    if response.status_code >= 500:
        # ASKED AND UNDECIDED ([[LH-279]]): the catalog answers 503 when the base record it must judge cannot
        # be read, and 500 when the store fails under it — the ambient key standing in for either signs a
        # pass the door may be about to refuse. The dataset stops and is retried.
        raise VendUndecided(f"the catalog could not decide a write credential for {table_id} ({response.status_code}): {response.text[:200]}")
    if response.status_code >= 400:
        logger.info("credential vending unavailable for %s (%s)", table_id, response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.info("credential vending returned an unparseable body for %s", table_id)
        return None
    if payload.get("mode") != "direct":
        return None
    options = (payload.get("credentials") or {}).get("storage_options")
    if not isinstance(options, dict):
        return None
    answered = {str(key): str(value) for key, value in options.items()}
    try:
        storage_options = StorageOptions.from_flat(answered)
    except ValueError as exc:
        # Half a credential, or two spellings naming two identities: read as no credential, the pass would sign with the
        # pod's own chain while reported as scoped. Like an unparseable body, it is no vend, and the deployment's own
        # credential signs and is named for it.
        logger.info("credential vending answered a credential that cannot be signed with for %s: %s", table_id, exc)
        return None
    location = payload.get("location")
    return Vended(storage_options=storage_options, location=str(location) if location else None)
