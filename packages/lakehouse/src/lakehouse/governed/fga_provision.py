"""How a process comes to hold its OpenFGA client: the store it finds or creates, this image's model in that store, the
client configured on both, and the client's disposal.

:func:`provision` writes this image's model when the store has never held its body, and only the registered bootstrap
calls it (its `banned-api` entry in ``pyproject.toml``); :func:`resolve` is its read-only half, for a service that does
not own the estate's permissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from contextlib import suppress
from typing import Any, Final, Self, override

from lance_namespace import ServiceUnavailableError
from openfga_sdk import ClientConfiguration, OpenFgaClient
from openfga_sdk.configuration import RetryParams
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from openfga_sdk.exceptions import ForbiddenException, UnauthorizedException
from openfga_sdk.models.create_store_request import CreateStoreRequest
from openfga_sdk.models.write_authorization_model_request import WriteAuthorizationModelRequest
from pydantic import BaseModel, ConfigDict

from lakehouse.governed import fga_model
from lakehouse.governed.fga_resilience import DEFAULT_RETRY, FAIL_CLOSED, ONE_ATTEMPT, RetryPolicy, guarded
from lakehouse.governed.machine_identity import read_identity_token


log = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS: Final = 5.0


# --------------------------------------------------------------------------- #
# Provisioning / client construction
# --------------------------------------------------------------------------- #


def _plain(value: Any) -> Any:
    """An SDK model (or any nesting of them) as plain JSON-able data.

    The comparison has to span two shapes — `load_model()` yields dicts off `model.json`, the store
    yields SDK objects — and every SDK model here exposes `to_dict()`, which recurses for us.
    """
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        # `serialize=True`, and it is the whole correctness of the comparison. The SDK's generated
        # models keep PYTHON attribute names and render those unless asked otherwise —
        # `attr = self.attribute_map.get(attr, attr) if serialize else attr` — so the default
        # `to_dict()` yields `computed_userset` while `model.json`, and the wire it came from, say
        # `computedUserset`. Comparing the two spellings can only ever answer "different", which would
        # make the unchanged-model skip below a branch that never runs.
        #
        # The fallback is for a `to_dict` that takes no argument at all; the SDK's does, and the real
        # shapes are pinned against actual SDK objects rather than a double, because a double that
        # returns the authored dict agrees with it by construction and proves nothing about spelling.
        try:
            return _plain(to_dict(serialize=True))
        except TypeError:
            return _plain(to_dict())
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


#: The model grammar's EMPTY MESSAGES, whose presence is the value: ``this: {}`` is a direct assignment
#: and ``wildcard: {}`` is what makes ``[user:*]`` differ from ``[user]``. They are the only fields the
#: SDK's model schema types as ``object``, walked rather than listed in
#: `packages/lakehouse/tests/test_fga_provision.py::test_every_EMPTY_MESSAGE_in_the_model_schema_survives_the_canonical_form`.
_EMPTY_MESSAGES: Final = frozenset({"this", "wildcard"})

#: The schema's MAP fields, whose keys are names an author chose (a relation, a condition, a parameter)
#: rather than grammar fields.
_NAMED_MAPS: Final = frozenset({"conditions", "parameters", "relations"})


def _without_defaults(value: Any, *, named: bool = False) -> Any:
    """Drop the empty values OpenFGA fills in — ``None``, ``""``, ``{}`` and ``[]`` — except an empty message.

    THIS IS WHAT MAKES THE COMPARISON POSSIBLE AT ALL. The store does not keep the model it was given.
    Measured on OpenFGA v1.18.3 (2026-09-25), writing this repo's `model.json` and reading it back over
    REST fills `module: ""` (169), `source_info: null` (169), `object: ""` (234), `condition: ""` (132),
    `directly_related_user_types: []` (89), `generic_types: []` (3), `metadata: null` (2) and
    `relations: {}` (1); the SDK read adds `None` for every unset field. Comparing that directly would
    answer "different" forever, so the skip built on it would be a branch that never runs.

    An empty value under an :data:`_EMPTY_MESSAGES` key is kept: stripping it would make a relation
    widened to ``[user:*]``, or narrowed back, compare equal and never be written. ``named`` marks the
    value of a :data:`_NAMED_MAPS` field, whose keys are an author's names, so a relation called
    ``wildcard`` is not mistaken for the field. A falsey SCALAR is not a default either — `0` and
    `False` are values a condition may carry — so they survive.
    """
    if isinstance(value, dict):
        kept = {}
        for key, item in value.items():
            stripped = _without_defaults(item, named=not named and key in _NAMED_MAPS)
            if not named and key in _EMPTY_MESSAGES and stripped == {}:
                kept[key] = stripped
                continue
            if stripped is None or stripped == "" or stripped == {} or stripped == []:
                continue
            kept[key] = stripped
        return kept
    if isinstance(value, list):
        return [_without_defaults(item) for item in value]
    return value


#: The three fields a model WRITE carries. The store-assigned `id` is deliberately not among them: it
#: differs by construction on every version and would make an unchanged model look changed.
_MODEL_FIELDS: Final = ("schema_version", "type_definitions", "conditions")


def _field(source: Any, name: str) -> Any:
    """One field off either shape — a mapping from ``model.json`` or an object off the SDK."""
    return source.get(name) if isinstance(source, dict) else getattr(source, name, None)


def canonical_model(model: Any) -> str:
    """The comparable form of an authorization model, from either shape it arrives in.

    Fields are read off the top level BEFORE flattening, because the two shapes disagree about what the
    top level is: `load_model()` hands back a mapping and the store hands back an SDK object.

    ``default=repr`` keeps the dump TOTAL rather than letting an unexpected value raise mid-boot. It is
    unreachable for the two shapes that really arrive — plain JSON, and SDK models that all expose
    `to_dict()` — and where it does fire, two unlike objects render unlike, so the answer is "changed"
    and the write proceeds as it always did. Falling to the writing side is the safe direction: the
    worst case is the version churn this exists to reduce, never a skipped model edit.
    """
    body = {name: _plain(_field(model, name)) for name in _MODEL_FIELDS}
    return json.dumps(_without_defaults(body), sort_keys=True, separators=(",", ":"), default=repr)


def _relation_bodies(type_definitions: Iterable[Any] | None) -> dict[str, str]:
    """``{type#relation: canonical definition}`` from either shape a model arrives in.

    A definition is the userset
    body AND its ``metadata.relations`` entry, because the entry holds the type restrictions: ``[user]``
    against ``[user:*]``, or a dropped ``with non_expired_grant``, changes who a tuple may name under an
    identical body. Both go through the canonicaliser the unchanged-model check uses, so the two agree
    about what "the same definition" means.
    """
    bodies: dict[str, str] = {}
    for definition in type_definitions or ():
        type_name = _field(definition, "type")
        restrictions = _field(_field(definition, "metadata"), "relations") or {}
        for relation, body in (_field(definition, "relations") or {}).items():
            entry = _without_defaults(_plain({"body": body, "metadata": restrictions.get(relation)}))
            bodies[f"{type_name}#{relation}"] = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=repr)
    return bodies


def _body_changes(incoming: dict[str, str], current: dict[str, str]) -> list[str]:
    """Relations present in BOTH models whose definition differs — what a write is about to change.

    NOT a narrowing detector, and must not be read as one: a widened relation and a narrowed one both
    appear here. It exists so a model write names what it changed, because `provision` logged nothing
    at all and a boot that replaced the estate's authorization model was indistinguishable from one
    that did not.
    """
    return sorted(key for key, body in current.items() if key in incoming and incoming[key] != body)


#: 50 is OpenFGA's default page of models; v1.18.3 serves up to 100 (measured 2026-09-28), so the live
#: store's 1,353 models are 14 pages.
MODEL_PAGE_SIZE: Final = 100
#: A history longer than this is refused rather than read as "absent", because absent means WRITE.
MODEL_MAX_PAGES: Final = 400


class ModelHistoryTooLongError(Exception):
    """The store's model history runs past ``MODEL_MAX_PAGES``: unreadable as a whole, so never "absent".

    Its own type, not an outage: `resolve` waits out an unavailable OpenFGA, and waiting cannot shorten
    a history.
    """


#: How long a booting service waits for its store and its image's model before failing closed. It runs
#: inside the fleet's startup budget (`lance.appProbes`: 30 x 10 s) beside the lifespan's other steps,
#: the secret store login alone up to about two minutes while the store does not admit the pod, so the budget has to
#: hold this deadline plus those steps.
RESOLVE_DEADLINE_SECONDS: Final = 120.0


class OpenFgaCredentialRefusedError(ServiceUnavailableError):
    """OpenFGA answered 401 or 403 to this pod's credential ([[XC-077]]).

    Not an outage: no retry or wait mends a missing, wrong or unlisted token, so a booting service raises it
    whatever its `fatal` posture (`auth_lifespan.build_fga_client`). A pod that kept serving with no client
    would stay Ready while every governed door failed closed, and a rollout would report green.
    """


class OpenFgaModelAbsentError(ServiceUnavailableError):
    """The store, or the model this image carries, was still absent when :func:`resolve` stopped waiting.

    Its message names what the last look found: no store by the name, a store holding no model whose canonical body is
    this image's ``model.json``, or OpenFGA not answering (chained to the transport's error). A booting service fails
    closed on it (`auth_lifespan.build_fga_client`): a non-fatal one serves with every governed door unavailable, and a
    fatal one does not start.
    """


def credential_refused(exc: BaseException) -> bool:
    """Whether ``exc``, or anything in its cause chain, is OpenFGA refusing the credential (401 or 403)."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, (UnauthorizedException, ForbiddenException, OpenFgaCredentialRefusedError)):
            return True
        seen = seen.__cause__
    return False


class _ProjectedToken(CredentialConfiguration):
    """The pod's projected ServiceAccount token, read again on every request the SDK sends ([[XC-077]]).

    The SDK reads ``api_token`` each time it builds a request's ``Authorization`` header
    (``ApiClient.update_params_for_auth``), so a property that reads the file presents the token the kubelet
    holds now: a copy taken at construction stops authenticating at the first rotation (~515 s of a 600 s
    token). An unreadable file raises `IdentityTokenUnavailableError` and the request is not sent.
    """

    def __init__(self, token_file: str) -> None:
        super().__init__()
        self._token_file = token_file

    @property
    @override
    def api_token(self) -> str:
        return read_identity_token(self._token_file)

    @api_token.setter
    def api_token(self, value: str | None) -> None:
        """Refused: a token set here would be a copy, which stops authenticating at the first rotation."""
        raise AttributeError(f"the OpenFGA token is read from {self._token_file} on every request and cannot be set")


def client_configuration(
    api_url: str,
    *,
    token_file: str | None,
    store_id: str | None = None,
    authorization_model_id: str | None = None,
    timeout_millisec: int | None = None,
    retry_params: RetryParams | None = None,
) -> ClientConfiguration:
    """The configuration every OpenFGA client in the estate is built from, presenting ``token_file`` when given.

    ``token_file`` has no default, so no call site leaves the credential out by omission. ``None`` sends no
    ``Authorization`` header, which an OpenFGA with authn answers 401. The SDK reads the token once while
    building (``validate_credentials_config``), so an unreadable file fails the build, not the first check.
    """
    credentials = Credentials(method="api_token", configuration=_ProjectedToken(token_file)) if token_file else None
    return ClientConfiguration(
        api_url=api_url,
        credentials=credentials,
        store_id=store_id,
        authorization_model_id=authorization_model_id,
        timeout_millisec=timeout_millisec,
        retry_params=retry_params,
    )


def newest_store(stores: Iterable[Any], name: str) -> Any | None:
    """The newest store named ``name``: the rule every reader and writer of the estate's model shares.

    Its callers ask OpenFGA for the stores of that name (`ListStores`'s ``name``, an exact match): a listing of every
    store answers 50 a page, oldest first (measured on the pinned server), so on an OpenFGA that held 50 stores before
    the estate's, the first page does not hold it.
    """
    named = [store for store in stores if _field(store, "name") == name]
    return max(named, key=lambda store: _field(store, "created_at")) if named else None


class ModelHistory(BaseModel):
    """A store's models read against one body: its newest model, and the newest one carrying that body.

    Nothing treats the store's newest model as authoritative ([[LH-201]]): every service and the hook
    use the id of the model their own image carries. ``carrying`` below ``newest``
    is therefore no failure — an older image, a revert, or a writer that wrote since — and writing the
    body again would only grow the history. ``carrying`` absent is a model the store has never held.
    """

    model_config = ConfigDict(frozen=True)

    newest: Any = None
    carrying: Any = None

    @property
    def held_below_newest(self) -> bool:
        return self.carrying is not None and str(_field(self.carrying, "id")) != str(_field(self.newest, "id"))

    def read(self, models: Iterable[Any], wanted: str) -> Self:
        """This history extended by the next page, newest first; ``wanted`` is the body's canonical form."""
        newest, carrying = self.newest, self.carrying
        for model in models:
            newest = model if newest is None else newest
            if carrying is None and canonical_model(model) == wanted:
                carrying = model
        return self.model_copy(update={"newest": newest, "carrying": carrying})


async def _model_history(
    client: OpenFgaClient,
    desired: Any,
    *,
    known_newest: str | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> ModelHistory:
    """The store's models read against ``desired``, paged newest first and stopped at the first match.

    ``known_newest`` is the newest id an earlier read already scanned past: models are listed newest first
    and a new one can only be prepended, so paging stops there and a re-read costs one page. Under the
    module's posture, retried then failed closed: an unread history is not an absent model, and absent is
    what makes a writer write.
    """
    wanted = canonical_model(desired)

    async def _do_read() -> ModelHistory:
        history, token = ModelHistory(), ""
        for _ in range(MODEL_MAX_PAGES):
            options: dict[str, int | str | dict[str, int | str]] = {"page_size": MODEL_PAGE_SIZE}
            if token:
                options["continuation_token"] = token
            response = await client.read_authorization_models(options=options)
            page = list(response.authorization_models or [])
            ids = [str(_field(model, "id")) for model in page]
            reached = known_newest in ids
            history = history.read(page[: ids.index(known_newest)] if reached else page, wanted)
            token = response.continuation_token or ""
            if history.carrying is not None or reached or not token:
                return history
        raise ModelHistoryTooLongError(f"the store's model history exceeded {MODEL_MAX_PAGES} pages; refusing to read it as absent")

    return await guarded(
        _do_read,
        event="openfga_read_model_unavailable",
        retry=retry,
    )


async def provision(
    api_url: str,
    *,
    token_file: str | None,
    store_name: str = "lance-catalog",
    store_id: str | None = None,
    retry: RetryPolicy = DEFAULT_RETRY,
) -> tuple[str, str]:
    """Idempotently ensure the catalog store + model exist; return ``(store_id, model_id)``.

    REUSES an existing store of the same name instead of minting a fresh one on every
    unpinned boot — otherwise a restart strands every tuple written against the previous
    store (creators silently lose access). This image's model is written when the store has never held
    its body, removals included: every component checks against the model its own image carries, so a
    narrower body governs only the pods built with it ([[LH-201]]). Tuples live on the store and survive
    new model versions. A pinned ``store_id`` (``RASK_FGA_STORE_ID``) is used as is, the store the hook uses
    too.
    """
    model = fga_model.load_model()
    store_is_new = False
    if store_id is None:
        async with OpenFgaClient(client_configuration(api_url, token_file=token_file)) as client:
            stores = await client.list_stores(options={"name": store_name})
            existing = newest_store(stores.stores or [], store_name)
            if existing is not None:
                store_id = str(existing.id)
            else:
                created = await client.create_store(CreateStoreRequest(name=store_name))
                store_id = str(created.id)
                store_is_new = True
    async with OpenFgaClient(client_configuration(api_url, token_file=token_file, store_id=store_id)) as client:
        # A store minted moments ago holds no model, so it never pays the read, which is also what keeps
        # a first boot from depending on a model that cannot exist yet.
        history = ModelHistory() if store_is_new else await _model_history(client, model, retry=retry)
        if history.carrying is not None:
            # THE STORE ALREADY HOLDS THIS IMAGE'S MODEL, so nothing is written ([[LH-201]]). Every
            # `write_authorization_model` mints an immutable version, so rewriting a held body grows the
            # history on every boot (1,353 versions in the live store, 2026-09-27). Held below the newest,
            # it is still this image's model: every consumer checks against its own, so the newest is no
            # one's rules and re-promoting a body would only grow the history.
            carrying_id = str(history.carrying.id)
            if history.held_below_newest:
                log.info("openfga_model_held_below_newest", extra={"store_id": store_id, "model_id": carrying_id, "newest_model_id": str(history.newest.id)})
            else:
                log.info("openfga_model_unchanged", extra={"store_id": store_id, "model_id": carrying_id})
            return store_id, carrying_id
        current = history.newest
        changed = _body_changes(_relation_bodies(model["type_definitions"]), _relation_bodies(current.type_definitions)) if current is not None else []
        written = await client.write_authorization_model(
            WriteAuthorizationModelRequest(
                schema_version=model["schema_version"],
                type_definitions=model["type_definitions"],
                # The WHOLE model, conditions included. Dropping this key while relations reference
                # a condition makes OpenFGA 400 the write ("condition … is undefined"), the client
                # never builds, and every FGA-enabled service fail-closes 503 — the entire plane
                # down from one silently omitted field (found live, 2026-08-03).
                conditions=model.get("conditions"),
            )
        )
    # A MODEL WRITE WAS SILENT, which is half of why 1,316 versions accumulated with nobody noticing.
    # Now that an unchanged model is skipped, reaching this line means the model REALLY changed, so
    # naming what changed is signal rather than noise. `changed` lists relations redefined in place —
    # widened and narrowed alike, since telling those apart is a semantic question about usersets, not
    # a structural one — which is what makes a body-level rollback visible even though it is not refused.
    log.info(
        "openfga_model_written",
        extra={"store_id": store_id, "model_id": written.authorization_model_id, "changed": changed[:20], "changed_count": len(changed)},
    )
    return store_id, written.authorization_model_id


async def resolve(
    api_url: str,
    *,
    token_file: str | None,
    store_name: str = "lance-catalog",
    store_id: str | None = None,
    deadline_seconds: float = RESOLVE_DEADLINE_SECONDS,
    poll_seconds: float = 5.0,
) -> tuple[str, str]:
    """Find the EXISTING store (by name, or ``store_id`` when pinned) and the model THIS IMAGE carries. Read-only.

    The half of :func:`provision` that is not a write. A service that does not own the estate's permissions must not
    write an authorization model, since whoever writes it decides everyone else's access; looking up the store every
    other service already uses is a read, and needs no such standing. The chart leaves the store unpinned by default
    (``auth.fgaStoreId: ""``: a store id is a per-cluster ULID and cannot be a committed default), so this lookup by
    name is how such a service finds its store at all. It never creates a store, writes a model or changes what anyone
    may do: a store or model still absent at the deadline is raised, and the caller fails closed.

    The model is the newest one whose canonical body equals the bundled ``model.json``, never merely the
    store's newest ([[LH-201]]): the newest is whichever writer wrote last, so during a rollout or after an
    older image boots, a relation this image reasons about can be missing or defined differently there.
    On a first install, or while the chart's pre-upgrade hook has not written this image's model yet, the
    store, the model or OpenFGA itself can be missing for a while; each is waited out, ``poll_seconds``
    apart, until ``deadline_seconds`` have passed. The first read scans the whole history; a later one
    stops at the newest model already scanned, so a wait costs one page per poll.

    Raises:
        OpenFgaCredentialRefusedError: OpenFGA refused this pod's credential, which no wait mends.
        OpenFgaModelAbsentError: at the deadline the store or this image's model was still absent, or OpenFGA was
            not answering; the message names which.
    """
    model = fga_model.load_model()
    loop = asyncio.get_running_loop()
    started = loop.time()
    known_newest: str | None = None
    while True:
        transport_error: BaseException | None = None
        try:
            looked = await _looked_up(api_url, store_name, model, store_id=store_id, token_file=token_file, known_newest=known_newest)
            if not isinstance(looked, _NotYet):
                return looked
            absent, known_newest = OpenFgaModelAbsentError(looked.reason), looked.known_newest
        except (*FAIL_CLOSED, ServiceUnavailableError) as exc:
            # The store lookup raises the transport's own errors; the history read raises them as
            # ServiceUnavailableError through `guarded`. Both are "not answering yet", except a refused
            # credential, which no wait mends.
            if credential_refused(exc):
                raise OpenFgaCredentialRefusedError(f"OpenFGA at {api_url} refused this pod's credential (token file {token_file!r})") from exc
            absent, transport_error = OpenFgaModelAbsentError(f"OpenFGA at {api_url} is not answering: {exc}"), exc
        if loop.time() - started + poll_seconds > deadline_seconds:
            raise absent from transport_error
        await asyncio.sleep(poll_seconds)


class _NotYet(BaseModel):
    """One look that found the store or the model absent: why, and the newest model id it scanned past."""

    model_config = ConfigDict(frozen=True)

    reason: str
    known_newest: str | None


async def _looked_up(
    api_url: str, store_name: str, model: Any, *, store_id: str | None, token_file: str | None, known_newest: str | None
) -> tuple[str, str] | _NotYet:
    """The ids of the store and of its model whose body is ``model``, from one look, or why not yet."""
    found = store_id or await _named_store_id(api_url, store_name, token_file=token_file)
    if found is None:
        return _NotYet(reason=f"OpenFGA at {api_url} holds no store named {store_name!r}", known_newest=known_newest)
    async with OpenFgaClient(client_configuration(api_url, token_file=token_file, store_id=found)) as client:
        history = await _model_history(client, model, known_newest=known_newest, retry=ONE_ATTEMPT)
    if history.carrying is not None:
        return found, str(history.carrying.id)
    if history.newest is not None:
        known_newest = str(_field(history.newest, "id"))
    return _NotYet(reason=f"store {found} at {api_url} holds no model carrying this image's model.json", known_newest=known_newest)


async def _named_store_id(api_url: str, store_name: str, *, token_file: str | None) -> str | None:
    """The id of the newest store named ``store_name``, or ``None`` when there is none."""
    async with OpenFgaClient(client_configuration(api_url, token_file=token_file)) as client:
        stores = await client.list_stores(options={"name": store_name})
    found = newest_store(stores.stores or [], store_name)
    return None if found is None else str(found.id)


def make_client(
    api_url: str,
    store_id: str,
    model_id: str,
    *,
    token_file: str | None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> OpenFgaClient:
    """Build an OpenFGA client pinned to a store + authorization model, presenting ``token_file`` (`client_configuration`).

    ``timeout_seconds`` becomes the client-wide request timeout
    (``ClientConfiguration.timeout_millisec``) so an unresponsive OpenFGA fails
    fast — and is then retried by ``check`` / ``grant_on_create`` — instead of
    hanging the request indefinitely.

    The SDK's built-in retry layer is disabled (``RetryParams(max_retry=0)``) so it
    does not stack on tenacity (which would fan one logical check into ~12 HTTP calls
    with two uncoordinated backoffs). tenacity is the single authoritative retry layer.
    """
    return OpenFgaClient(
        client_configuration(
            api_url,
            token_file=token_file,
            store_id=store_id,
            authorization_model_id=model_id,
            timeout_millisec=int(timeout_seconds * 1000),
            retry_params=RetryParams(max_retry=0),
        )
    )


async def dispose(app: object) -> None:
    """Close the OpenFGA client and the decider this app opened, if it opened them.

    THE FACTORY'S PACKAGE OWNS THE DISPOSAL, which is the whole point rather than a convenience. Five
    lifespans call `make_client` and exactly one of them closed the result — the other four each would
    have needed their own copy of the same six lines, which is how four of them came to be missing it.
    One helper beside the factory makes the pairing hard to forget instead of easy.

    The SDK is aiohttp-backed, so an unclosed client is collected with its session open: one half-open
    connection per replica on OpenFGA until its own idle timeout, and the only trace is an "Unclosed
    client session" line on the way out.

    SUPPRESSED, NEVER RAISED. A shutdown path that raises hides whatever came after it, and a failed
    close cannot be retried against anything on a pod that is already leaving. Absent or `None` is
    silent too: FGA-off stacks never build one, and a lifespan that failed before it got that far must
    still be able to unwind.
    """
    state = getattr(app, "state", None)
    # The decider too: a catalog decider holds an HTTP pool of its own, and a store decider's close is a no-op.
    for held in (getattr(state, "decider", None), getattr(state, "fga", None)):
        if held is not None:
            with suppress(Exception):
                await held.close()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MODEL_MAX_PAGES",
    "MODEL_PAGE_SIZE",
    "RESOLVE_DEADLINE_SECONDS",
    "ModelHistory",
    "ModelHistoryTooLongError",
    "OpenFgaCredentialRefusedError",
    "OpenFgaModelAbsentError",
    "canonical_model",
    "client_configuration",
    "credential_refused",
    "dispose",
    "make_client",
    "newest_store",
    "provision",
    "resolve",
]
