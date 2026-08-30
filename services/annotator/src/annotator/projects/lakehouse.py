"""The lakehouse side of the publish saga — the concrete :class:`~annotator.projects.saga.Publisher`.

Posts through the catalog's REST API over the same ``lance_namespace_urllib3_client`` stack the
media plane's merge transport uses (`service_kit.lancekit.writer.RestCatalogWriteTransport`) — **the
annotator never writes Lance directly** (§7.1): the catalog is what seeds FGA ownership on the new
table and emits the ``CREATE`` RunEvent.

Retry safety maps onto the catalog's own contract:

- ``create`` posts ``mode=exist_ok``. Fresh id ⇒ the catalog creates, seeds ownership, emits the
  event. Replayed id ⇒ the catalog KEEPS the existing table, returns its version, and deliberately
  skips re-seeding — exactly the "existing table with this id is success" the saga relies on.
- ``tag`` has no exist-ok on the catalog side (pylance ``tags.create`` errors on a duplicate), so
  convergence lives HERE: on failure, read the tag back — pointing at our version means a previous
  attempt already tagged it (success); pointing elsewhere means a different publish owns the name
  (loud failure, never silently adopted).

The transport itself is TWO stacks, and they used to disagree about failure: the tag calls go
through the generated client (`Configuration(retries=...)`), while the token mint and the S4
``create`` were bare ``httpx.post`` calls — a fresh client and connection each, with no retries at
all, on the half that WRITES. Both now share :func:`publish_client`, one pooled retrying client per
publisher, released by :meth:`CatalogPublisher.close`.

`spawn_publish` is the seam the project actor's watchdog reminder calls: it schedules
:func:`run_publish_for` on the event loop OUTSIDE the actor's turn, because the saga drives the
actor through its own proxy surface and an in-turn await would deadlock on the actor's mailbox.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel

from annotator.core.config import get_annotator_settings
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA, PublishPlan
from annotator.projects.saga import PublishOutcome, run_publish
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.warehouse_registry import namespace_for
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE


logger = logging.getLogger(__name__)

#: The catalog header that carries caller-supplied run facets (`{name: payload}`). The catalog's
#: ``merge_insert`` already parses it; the ``create`` side is the S4 extension.
RUN_FACETS_HEADER = "X-Lance-Run-Facets"

#: Connect-failure retries for the publish transport's DIRECT-HTTP half, kept equal to the retry
#: budget the generated client is built with below (`Configuration(retries=...)`).
#:
#: The two halves used to disagree: the tag calls went through the SDK and survived a dropped
#: connection, while the token mint and the `create` — the call that WRITES — were bare
#: `httpx.post(...)` at httpx's default of 0. A transient reset on the write half fires
#: `publish_failed` and leaves the project waiting for a watchdog tick. Retrying `create` is safe by
#: construction, not by hope: it posts `mode=exist_ok`, which is the same property the whole
#: retry-safety argument in this module's docstring rests on.
CATALOG_TRANSPORT_RETRIES = 3


def publish_client(timeout: float) -> Any:
    """One pooled, retrying `httpx.Client` for the publish path's direct-HTTP calls.

    A `Client` (and therefore a connection) per call was the other half of the same finding: the
    module-level `httpx.post` helper builds one, uses it once and throws it away. Callers own the
    returned client and must close it.
    """
    import httpx  # noqa: PLC0415 - publish path only, like every other httpx use in this module

    return httpx.Client(transport=httpx.HTTPTransport(retries=CATALOG_TRANSPORT_RETRIES), timeout=timeout)


def _bare_facets(run_facet: dict[str, Any]) -> dict[str, Any]:
    """Strip the spec stamps off each facet payload for the `X-Lance-Run-Facets` contract.

    `project_facet` returns a spec-legal facet — `custom_facet` stamps `_producer`/`_schemaURL` into
    it — but the header carries BARE payloads: the catalog's `shape_run_facets` stamps each facet
    itself and 400s any `_`-prefixed or `producer` key. Found by the live drive as a publish that
    could never succeed; the stripping lives HERE because the stamping is this transport's far end."""
    return {
        name: {k: v for k, v in payload.items() if not k.startswith("_") and k != "producer"} if isinstance(payload, dict) else payload
        for name, payload in run_facet.items()
    }


def _arrow_stream_bytes(plan: PublishPlan) -> bytes:
    """The plan's rows as an Arrow-IPC stream body for the catalog's create. Explicit schema, so an
    all-sentinel publish still produces correctly-typed columns."""
    import pyarrow as pa  # noqa: PLC0415 - heavy import, publish path only

    from service_kit.lancekit.arrow_ipc import encode_arrow_stream  # noqa: PLC0415 - keeps pyarrow off module import

    return encode_arrow_stream(pa.Table.from_pylist(plan.rows, schema=PUBLISHED_LABELS_SCHEMA))


def publish_token(settings: Any) -> str | None:
    """The saga's catalog identity, resolved at publish time.

    Precedence, and the reasoning behind it:

    1. ``MEDIA_PUBLISH_TOKEN_URL`` + ``MEDIA_PUBLISH_USERNAME`` set → mint a FRESH token from the
       IdP via the password grant with the dedicated service account. The password comes from the
       Dapr secret store (``lance-secrets``/``lance``), fail-closed — the estate's secrets rule:
       the store is the SOLE source and no plaintext credential rides pod env. Minted per publish,
       so nothing stored can expire — the exact failure a hand-pinned token produced live.
    2. Otherwise → ``None`` (an auth-off stack publishes anonymously, as before).

    ``MEDIA_CATALOG_TOKEN`` USED TO BE STEP 1 AND IS GONE. The field was deleted with the
    confused-deputy finding ("open_reader/open_writer fall back to the estate's catalog service
    credential") whose closing note wrongly claimed the credential had no other consumer — this was
    it, and the branch raised AttributeError on every publish until an adversarial re-audit found it.
    Restoring the field is not the fix: a pinned estate-wide credential as a publish IDENTITY means
    every published row carries the platform's name instead of the publisher's, which is exactly what
    that finding refused. Minting per publish is also what stops a hand-pinned token expiring, the
    failure this path already had live.

    Sync and blocking (the secret fetch + token POST); the caller runs it in a thread.
    """
    if not (settings.publish_token_url and settings.publish_username):
        return None

    from service_kit.governed.secrets import fetch_required_secrets  # noqa: PLC0415 - publish path only

    bundle = fetch_required_secrets(settings.publish_secret_store, settings.publish_secret_key, require="publisher-oidc-password")
    data = {
        "grant_type": "password",
        "username": settings.publish_username,
        "password": bundle["publisher-oidc-password"],
        "scope": "openid profile email",
    }
    # The client secret comes from the SAME bundle when the store serves it — this credential was the
    # one outside the estate's fail-closed secrets guard, riding plaintext pod env while the comment
    # above its chart row claimed otherwise. The env field stays only as the
    # no-OpenBao fallback, the same shape as every other guarded credential.
    client_secret = bundle.get("publisher-oidc-client-secret") or settings.publish_client_secret or ""
    auth = (settings.publish_client_id, client_secret) if settings.publish_client_id else None
    with publish_client(15.0) as client:
        response = client.post(settings.publish_token_url, data=data, auth=auth)
    if response.status_code >= 400:
        raise RuntimeError(f"the IdP refused the publish identity: HTTP {response.status_code} {response.text[:200]}")
    payload = response.json()
    token = payload.get("id_token") or payload.get("access_token")
    if not token:
        raise RuntimeError("the IdP's token response carries neither id_token nor access_token")
    return str(token)


class CreateTableResult(BaseModel):
    """What the catalog's create answers with, as far as the saga cares: the table version.

    ``version`` mirrors the SDK response's attribute of the same name, so `CatalogPublisher` reads
    an injected SDK-shaped fake and this model through one `getattr`."""

    version: int | None = None


class _HttpCreateApi:
    """The create call over direct HTTP — same signature as the SDK's ``DataApi.create_table`` so
    the injectable test seam is unchanged, plus the S4 params the generated client cannot send.

    Holds ONE pooled, retrying client for the life of the publisher (see
    :data:`CATALOG_TRANSPORT_RETRIES`); `close` releases it, and `CatalogPublisher.close` is what
    calls that.
    """

    def __init__(self, base_url: str) -> None:
        self._base = base_url.rstrip("/")
        self._client = publish_client(60.0)

    def close(self) -> None:
        """Release the pooled connection. Called by `CatalogPublisher.close`."""
        self._client.close()

    def create_table(
        self,
        table_id: str,
        body: bytes,
        *,
        delimiter: str = CATALOG_DELIMITER,
        mode: str | None = None,
        properties: str | None = None,
        source: str | None = None,
        source_version: int | None = None,
        _headers: dict[str, str] | None = None,
        _request_timeout: float = 60.0,
    ) -> CreateTableResult:
        from urllib.parse import quote  # noqa: PLC0415

        params: dict[str, str] = {"delimiter": delimiter}
        if mode:
            params["mode"] = mode
        if properties:
            params["properties"] = properties
        if source:
            params["source"] = source
            if source_version is not None:
                params["source_version"] = str(source_version)
        response = self._client.post(
            f"{self._base}/v1/table/{quote(table_id, safe='')}/create",
            params=params,
            content=body,
            headers={"content-type": ARROW_STREAM_MEDIA_TYPE, **(_headers or {})},
            timeout=_request_timeout,
        )
        if response.status_code >= 400:
            # Same failure surface the SDK raises — translate_catalog_errors maps it onward.
            from lance_namespace_urllib3_client.exceptions import ApiException  # noqa: PLC0415

            raise ApiException(status=response.status_code, reason=response.text[:500])

        return CreateTableResult(version=response.json().get("version"))


class CatalogPublisher:
    """`saga.Publisher` over the catalog's REST API.

    The SDK client is synchronous (urllib3), so every call runs in a worker thread. ``data_api`` /
    ``tag_api`` are injectable for tests — the structural-fake pattern the estate uses for Ray."""

    def __init__(
        self,
        base_url: str,
        *,
        delimiter: str = CATALOG_DELIMITER,
        token: str | None = None,
        originator: str | None = None,
        timeout: float = 60.0,
        data_api: Any | None = None,
        tag_api: Any | None = None,
    ) -> None:
        self._delimiter = delimiter
        self._timeout = timeout
        self._headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}
        # WHO this publish is for. The token above is the service's credential and the catalog's
        # `enforce_author` will author every resulting event as that service, so this header is the
        # only place the human survives. It authorizes nothing — the notifications plane re-derives
        # visibility per recipient at delivery — which is why it may ride beside a service bearer.
        if originator:
            self._headers["x-lance-originator"] = originator
        if data_api is not None and tag_api is not None:
            self._data, self._tags = data_api, tag_api
            return
        from lance_namespace_urllib3_client import ApiClient, Configuration  # noqa: PLC0415 - optional dep
        from lance_namespace_urllib3_client.api.tag_api import TagApi  # noqa: PLC0415

        client = ApiClient(Configuration(host=base_url, retries=CATALOG_TRANSPORT_RETRIES))
        if token:
            client.set_default_header("Authorization", f"Bearer {token}")
        # The tag call goes through the generated client, which does not read `self._headers` — wiring
        # only the create would leave the publish half-attributed.
        if originator:
            client.set_default_header("x-lance-originator", originator)
        # Create goes over DIRECT HTTP, not the SDK: the spec-generated `create_table` cannot send
        # our S4 `source`/`source_version` query params (OPEN-WORK §B3), and the pin must travel.
        self._data = _HttpCreateApi(base_url)
        self._tags = TagApi(client)

    def close(self) -> None:
        """Release both transports' connections.

        Only what THIS publisher opened: an injected `data_api`/`tag_api` belongs to its injector,
        and closing it here would be ownership this object does not have (the same reasoning the
        annotator's lifespan applies to `AppState`'s slots).
        """
        for api in (self._data, self._tags):
            closer = getattr(api, "close", None)
            if closer is not None:
                try:
                    closer()
                except Exception:  # noqa: BLE001 - a shutdown that raises hides whatever came after it
                    logger.warning("error closing the publish transport %s", type(api).__name__)

    async def create_table(
        self,
        table_id: str,
        plan: PublishPlan,
        *,
        properties: dict[str, str],
        run_facet: dict[str, Any],
        source: str | None = None,
        source_version: int | None = None,
    ) -> int:
        """Create (or converge on) the published table; return its version."""
        from service_kit.lancekit.reader import translate_catalog_errors  # noqa: PLC0415 - keeps import-time light

        body = _arrow_stream_bytes(plan)
        headers = {**self._headers, RUN_FACETS_HEADER: json.dumps(_bare_facets(run_facet))}

        def _call() -> Any:
            with translate_catalog_errors():
                return self._data.create_table(
                    table_id,
                    body,
                    delimiter=self._delimiter,
                    mode="exist_ok",
                    properties=json.dumps(properties),
                    source=source,
                    source_version=source_version,
                    _headers=headers,
                    _request_timeout=self._timeout,
                )

        response = await asyncio.to_thread(_call)
        return int(getattr(response, "version", None) or 1)

    async def tag_version(self, table_id: str, version: int, tag: str) -> None:
        """Tag the version, treating "already tagged at this exact version" as success."""
        from lance_namespace_urllib3_client import CreateTableTagRequest  # noqa: PLC0415 - optional dep

        def _create() -> None:
            self._tags.create_table_tag(
                table_id,
                CreateTableTagRequest(tag=tag, version=version),
                delimiter=self._delimiter,
                _headers=self._headers or None,
                _request_timeout=self._timeout,
            )

        try:
            await asyncio.to_thread(_create)
        except Exception as exc:
            existing = await self._tag_points_at(table_id, tag)
            if existing == version:
                logger.info("tag %s already at version %d — converged", tag, version)
                return
            raise RuntimeError(f"could not tag {table_id}@{version} as {tag!r}: {exc} (tag currently points at {existing})") from exc

    async def _tag_points_at(self, table_id: str, tag: str) -> int | None:
        """Where the tag currently points, or None if it does not exist / cannot be read."""
        from lance_namespace_urllib3_client import GetTableTagVersionRequest  # noqa: PLC0415 - optional dep

        def _get() -> Any:
            return self._tags.get_table_tag_version(
                table_id,
                GetTableTagVersionRequest(tag=tag),
                delimiter=self._delimiter,
                _headers=self._headers or None,
                _request_timeout=self._timeout,
            )

        try:
            response = await asyncio.to_thread(_get)
        except Exception:
            return None
        version = getattr(response, "version", None)
        return int(version) if version is not None else None


# --------------------------------------------------------------------------------------------------
# The runner — what the watchdog reminder actually drives
# --------------------------------------------------------------------------------------------------

#: Projects with a saga run live IN THIS PROCESS. Actors are single-placement, so the reminder for a
#: given project only ever fires on the pod hosting its actor — a process-local guard is the whole
#: story, and a 60 s tick racing a live run stands down here instead of double-driving the catalog.
_RUNNING: set[str] = set()


#: Strong references to in-flight saga tasks. asyncio holds only a weak reference to a running task,
#: so without this a publish could be collected mid-flight — losing the work and leaking `_RUNNING`.
_TASKS: set[asyncio.Task[None]] = set()


def spawn_publish(project_id: str) -> asyncio.Task[None] | None:
    """Schedule the saga for one project, unless it is already running here."""
    if project_id in _RUNNING:
        logger.debug("publish for %s already running — the tick stands down", project_id)
        return None

    async def _drive() -> None:
        try:
            outcome = await run_publish_for(project_id)
            if outcome is not None:
                logger.info(
                    "published project %s: table %s v%d (%d rows, %d shapes)%s",
                    outcome.project_id,
                    outcome.table_id,
                    outcome.version,
                    outcome.rows,
                    outcome.shapes,
                    " [converged]" if outcome.already_published else "",
                )
        except Exception:
            # `run_publish` already fired `publish_failed` with the reason; the next watchdog tick
            # (or an operator retry) re-drives. This log is the operator-facing trace.
            logger.exception("publish saga failed for project %s", project_id)
        finally:
            _RUNNING.discard(project_id)

    task = asyncio.get_running_loop().create_task(_drive())
    # Claimed only once the task EXISTS. Adding to `_RUNNING` first meant a `create_task` that raised
    # pinned the id forever, and `spawn_publish` would then stand down on every later tick — a project
    # that can never publish again, with nothing naming why.
    _RUNNING.add(project_id)
    # A STRONG REFERENCE, which asyncio requires and this did not hold: the event loop keeps only a
    # weak one, so an unreferenced task can be garbage-collected mid-flight. That loses the publish
    # AND leaks `_RUNNING`, because `_drive`'s `finally` never runs. The caller's returned handle is
    # not enough — `run_watchdog` discards it.
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


async def run_publish_for(project_id: str) -> PublishOutcome | None:
    """Run the saga for one project against the real actors and the real catalog.

    Reads the pinned target namespace and trigger off the project document — the endpoint
    authorized THAT namespace, so this never guesses one.
    """
    from annotator.projects.actor import AnnotationTaskActorInterface  # noqa: PLC0415 - avoids an import cycle
    from annotator.projects.project_actor import AnnotationProjectActorInterface  # noqa: PLC0415
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - opens a sidecar channel

    project_handle: Any = typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface)

    def task_handle(task_id: str) -> Any:
        return typed_proxy("AnnotationTaskActor", task_id, AnnotationTaskActorInterface)

    raw = await project_handle.get()
    if raw is None:
        logger.warning("publish watchdog fired for project %s but its actor holds no state", project_id)
        return None
    # Qualified by the project's own tenant, not a bare literal: this is the crash-resume path, so
    # the namespace it picks is the one a publish lands in with no request left to correct it.
    namespace = str(raw.get("pending_target_namespace") or namespace_for(str(raw.get("tenant") or ""), "silver"))
    subject = str(raw.get("pending_publish_by") or "system")

    settings = get_annotator_settings()
    if not settings.catalog_uri:
        # No transport is a CONFIGURATION failure, and it must surface as one — `publish_failed`
        # with a reason the UI can show — rather than stranding the project in `publishing` while
        # the watchdog ticks forever against a catalog that was never named.
        try:
            await project_handle.fire(
                {
                    "event": "publish_failed",
                    "actor": subject,
                    "error": "MEDIA_CATALOG_URI is not configured — the publish transport has no catalog to post to",
                }
            )
        except Exception:
            logger.exception("could not record the missing-catalog failure for project %s", project_id)
        return None

    # The saga's identity: a pinned token when configured, else minted FRESH from the IdP for this
    # run. Minting failures surface as publish_failed with the IdP's reason — not a stranded
    # `publishing` — because a wrong password or a sealed secret store is an operator's problem to
    # SEE, and the retry edge re-mints.
    try:
        token = await asyncio.to_thread(publish_token, settings)
    except Exception as exc:
        logger.exception("could not obtain a publish identity for project %s", project_id)
        try:
            await project_handle.fire({"event": "publish_failed", "actor": subject, "error": f"publish identity unavailable: {exc}"})
        except Exception:
            logger.exception("could not record the identity failure for project %s", project_id)
        return None

    publisher = CatalogPublisher(
        settings.catalog_uri,
        delimiter=settings.catalog_delimiter,
        token=token,
        originator=subject,
    )
    try:
        return await run_publish(
            project_handle=project_handle,
            task_handle=task_handle,
            publisher=publisher,
            namespace=namespace,
            subject=subject,
        )
    finally:
        # The publisher holds pooled connections to the catalog (and the SDK's own client). One
        # publisher is built per saga run, so leaving them to the collector leaks a connection per
        # publish — the same shape as the FGA session the lifespan closes.
        publisher.close()
