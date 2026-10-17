"""The questions a governed door asks about authorization, and the two places they are answered ([[LH-442]]).

OpenFGA authenticates its callers and authorizes none of them: any token it admits may call `Write`, `Read`,
`ListObjects` and `WriteAuthorizationModel` as freely as `Check`. So the estate gives the store as few clients as it
has writers of grants: the catalog, maintenance, the annotator and the chart's bootstrap jobs. Every other governed
service (lineage, notifications, the viewer and search, flows, compute) holds no credential to the store and asks
the catalog's decision door instead, as Lakekeeper is OpenFGA's only client and its OPA bridge asks Lakekeeper's
`/management/v1/action/batch-check` (authz/opa-bridge/policies/lakekeeper/check.rego).

This module is the wire contract (the schemas both sides read) and the two clients; what the door accepts is the
catalog's own rule (`catalog.api.v1.endpoints.decisions`). `AccessDecider` is the four questions those services ask,
and nothing that changes the store. `OpenFgaDecider` answers them from the store, for the services that hold a client;
`CatalogDecider` asks the catalog, which answers each route only to a caller holding that route's decision role on
the estate root.

Both fail closed the same way: an answer that cannot be had raises `ServiceUnavailableError`, which every gate
turns into a 503, never an allow and never a deny.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, Protocol

import httpx
from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from service_kit.governed import fga
from service_kit.governed.machine_identity import IdentityTokenUnavailableError


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


log = logging.getLogger(__name__)

#: Where the catalog serves the decision door; the routes below hang off it.
DECISIONS_PREFIX: Final = "/management/v1/decisions"

#: The most objects one question may name, Lakekeeper's own batch-check ceiling (`MAX_CHECKS`,
#: crates/lakekeeper/src/api/management/v1/check.rs): above it the door answers 400, so the client asks in chunks.
MAX_OBJECTS_PER_QUESTION: Final = 1000

#: How often a decision is re-sent after the catalog could not be reached at all. Only a connection that never opened
#: is retried: a request the catalog received may already be retrying OpenFGA (up to `fga`'s 15 s budget), and sending
#: it again would multiply that load rather than answer sooner.
_TRANSPORT_ATTEMPTS: Final = 3


class DecisionCheck(BaseModel):
    """May ``user`` (a principal key, as a caller's ``subject``) hold ``relation`` on each of ``objects``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    objects: list[str] = Field(min_length=1, max_length=MAX_OBJECTS_PER_QUESTION)


class DecisionCheckResult(BaseModel):
    """One verdict per object asked about."""

    model_config = ConfigDict(frozen=True)

    allowed: dict[str, bool]


class DecisionListUsers(BaseModel):
    """Which ``user`` subjects hold ``relation`` on ``object``, effective access included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(min_length=1)
    object: str = Field(min_length=1)


class DecisionListUsersResult(BaseModel):
    """Bare subject ids, sorted, as `fga.list_users` returns them."""

    model_config = ConfigDict(frozen=True)

    users: list[str]


class DecisionGoverned(BaseModel):
    """Whether any tuple names each of ``objects`` (ADR 0050: "is this governed at all" is OpenFGA's to answer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    objects: list[str] = Field(min_length=1, max_length=MAX_OBJECTS_PER_QUESTION)


class DecisionGovernedResult(BaseModel):
    """One answer per object asked about."""

    model_config = ConfigDict(frozen=True)

    governed: dict[str, bool]


class AccessDecider(Protocol):
    """The authorization questions a check-only door asks."""

    async def check(self, *, user: str, relation: str, obj: str) -> bool: ...

    async def batch_check(self, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]: ...

    async def list_users(self, *, relation: str, obj: str) -> list[str]: ...

    async def governed(self, objects: list[str]) -> dict[str, bool]: ...

    async def close(self) -> None: ...


class OpenFgaDecider:
    """The answers read from the store, for a service that holds an OpenFGA client."""

    def __init__(self, client: OpenFgaClient) -> None:
        self._client = client

    async def check(self, *, user: str, relation: str, obj: str) -> bool:
        return await fga.check(self._client, user=user, relation=relation, obj=obj)

    async def batch_check(self, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        return await fga.batch_check(self._client, user=user, relation=relation, objects=objects)

    async def list_users(self, *, relation: str, obj: str) -> list[str]:
        return await fga.list_users(self._client, relation=relation, obj=obj)

    async def governed(self, objects: list[str]) -> dict[str, bool]:
        # A name that is no object id holds no tuple, and reading it would fail the whole read.
        return {obj: fga.is_object_id(obj) and bool(await fga.read_object_tuples(self._client, obj)) for obj in objects}

    async def close(self) -> None:
        """The client belongs to the lifespan that built it (`fga.dispose`), not to this view of it."""


class CatalogDecider:
    """The answers the catalog's decision door gives this service, asked as the service account its token names.

    ``token`` answers the pod's projected ``rask-catalog`` token, read on every call: the kubelet rotates it at
    about 515 s of its 600 s life (`machine_identity`). ``transport`` is httpx's own seam, for a caller that
    serves the catalog in-process.
    """

    def __init__(
        self,
        catalog_url: str,
        *,
        token: Callable[[], str],
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not catalog_url:
            raise ValueError("a catalog decider needs the catalog's URL (RASK_FGA_DECIDER_URL)")
        self._token = token
        self._http = httpx.AsyncClient(base_url=f"{catalog_url.rstrip('/')}{DECISIONS_PREFIX}", timeout=timeout_seconds, transport=transport)

    async def check(self, *, user: str, relation: str, obj: str) -> bool:
        return (await self.batch_check(user=user, relation=relation, objects=[obj]))[obj]

    async def batch_check(self, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        allowed: dict[str, bool] = {}
        for chunk in _chunks(objects):
            answer = await self._ask("/check", DecisionCheck(user=user, relation=relation, objects=chunk), DecisionCheckResult)
            allowed |= _complete(answer.allowed, chunk)
        return allowed

    async def list_users(self, *, relation: str, obj: str) -> list[str]:
        return (await self._ask("/list-users", DecisionListUsers(relation=relation, object=obj), DecisionListUsersResult)).users

    async def governed(self, objects: list[str]) -> dict[str, bool]:
        governed: dict[str, bool] = {}
        for chunk in _chunks(objects):
            governed |= _complete((await self._ask("/governed", DecisionGoverned(objects=chunk), DecisionGovernedResult)).governed, chunk)
        return governed

    async def close(self) -> None:
        await self._http.aclose()

    async def _ask[R: BaseModel](self, route: str, question: BaseModel, result: type[R]) -> R:
        """POST one question and parse its answer; anything but a well-formed 200 fails closed."""
        try:
            response = await self._post(route, question)
        except (httpx.TransportError, IdentityTokenUnavailableError) as exc:
            log.error("decision_unreachable", extra={"route": route}, exc_info=True)
            raise ServiceUnavailableError("authorization service unavailable") from exc
        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            # THIS SERVICE may not ask (no token, or no decision role for this route): a deployment fault, so it is an
            # outage of every gate here rather than a refusal of the person behind the request.
            log.error("decision_refused", extra={"route": route, "status": response.status_code, "body": response.text[:512]})
            raise ServiceUnavailableError("authorization service unavailable")
        if response.status_code != httpx.codes.OK:
            log.error("decision_failed", extra={"route": route, "status": response.status_code, "body": response.text[:512]})
            raise ServiceUnavailableError("authorization service unavailable")
        try:
            return result.model_validate_json(response.content)
        except ValidationError as exc:
            log.error("decision_answer_malformed", extra={"route": route}, exc_info=True)
            raise ServiceUnavailableError("authorization service unavailable") from exc

    @retry(
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ConnectTimeout)),
        stop=stop_after_attempt(_TRANSPORT_ATTEMPTS),
        wait=wait_exponential_jitter(initial=fga.DEFAULT_RETRY_BACKOFF_SECONDS, max=fga.DEFAULT_RETRY_MAX_BACKOFF_SECONDS),
        reraise=True,
    )
    async def _post(self, route: str, question: BaseModel) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        return await self._http.post(route, content=question.model_dump_json(), headers=headers)


def _chunks(objects: list[str]) -> list[list[str]]:
    """``objects`` without repeats, in questions the door accepts."""
    distinct = list(dict.fromkeys(objects))
    return [distinct[i : i + MAX_OBJECTS_PER_QUESTION] for i in range(0, len(distinct), MAX_OBJECTS_PER_QUESTION)]


def _complete(answer: dict[str, bool], asked: list[str]) -> dict[str, bool]:
    """``answer`` when it answers exactly ``asked``: one that leaves an object out, or names one nobody asked about, fails closed."""
    if set(answer) != set(asked):
        log.error("decision_answer_incomplete", extra={"asked": len(asked), "answered": len(answer)})
        raise ServiceUnavailableError("authorization service unavailable")
    return answer


__all__ = [
    "DECISIONS_PREFIX",
    "MAX_OBJECTS_PER_QUESTION",
    "AccessDecider",
    "CatalogDecider",
    "DecisionCheck",
    "DecisionCheckResult",
    "DecisionGoverned",
    "DecisionGovernedResult",
    "DecisionListUsers",
    "DecisionListUsersResult",
    "OpenFgaDecider",
]
