"""The concrete CatalogPublisher — the transport half of the publish saga.

`Publisher` is a Protocol precisely so the saga's orchestration tests need no catalog; these are the
tests for the OTHER side of that seam. Driven against structural fakes of the lance-ns SDK's
`DataApi`/`TagApi` (the estate's Ray-client pattern), so what is pinned here is the CONTRACT this
class holds up to the saga:

- create posts `mode=exist_ok` with the plan's rows as an Arrow-IPC stream typed by the published
  schema, the properties JSON-encoded, and the run facet on `X-Lance-Run-Facets`.
- a duplicate tag CONVERGES when it already points at our version, and fails LOUDLY when a
  different publish owns the name.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pytest

from annotator.projects.lakehouse import CatalogPublisher
from annotator.projects.publish import PUBLISHED_LABELS_SCHEMA, PublishPlan


def _plan(rows: int = 1) -> PublishPlan:
    return PublishPlan(
        project_id="p1",
        publish_id="0123456789abcdef",
        rows=[
            dict.fromkeys(PUBLISHED_LABELS_SCHEMA.names)
            | {"project_id": "p1", "task_id": f"t{i}", "shape_type": "bbox", "polygon": [], "attributes": "{}", "difficult": False}
            for i in range(rows)
        ],
    )


class _StubClient:
    """A pooled-client stand-in for the publish transport's `publish_client` factory.

    The transport moved from module-level `httpx.post` (a throwaway client, and therefore a
    connection, per call) to ONE pooled retrying client — ANN-14. These tests intercept at the new
    seam; every assertion about the request itself is unchanged.
    """

    def __init__(self, post: Any) -> None:
        self._post = post
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._post(url, **kwargs)

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *_exc: Any) -> bool:
        self.close()
        return False


class _Create:
    def __init__(self, version: int = 7, fail: Exception | None = None) -> None:
        self.version = version
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def create_table(self, table_id: str, body: bytes, **kwargs: Any) -> Any:
        self.calls.append({"table_id": table_id, "body": body, **kwargs})
        if self.fail:
            raise self.fail

        class _Resp:
            version = self.version

        return _Resp()


class _Tags:
    """Tag store fake with the real catalog's sharp edge: a duplicate tag ERRORS."""

    def __init__(self) -> None:
        self.tags: dict[tuple[str, str], int] = {}
        self.creates = 0

    def create_table_tag(self, table_id: str, request: Any, **kwargs: Any) -> None:
        self.creates += 1
        key = (table_id, request.tag)
        if key in self.tags:
            raise RuntimeError("tag already exists")
        self.tags[key] = request.version

    def get_table_tag_version(self, table_id: str, request: Any, **kwargs: Any) -> Any:
        key = (table_id, request.tag)
        if key not in self.tags:
            raise RuntimeError("tag not found")

        class _Resp:
            version = self.tags[key]

        return _Resp()


def _publisher(create: _Create | None = None, tags: _Tags | None = None, **kw: Any) -> CatalogPublisher:
    return CatalogPublisher("http://catalog:2333", data_api=create or _Create(), tag_api=tags or _Tags(), **kw)


@pytest.mark.asyncio
async def test_create_posts_exist_ok_with_schema_typed_rows_and_its_run() -> None:
    create = _Create(version=9)
    publisher = _publisher(create, token="svc-token", originator="alice")

    version = await publisher.create_table(
        "silver$vasa_0123456789ab",
        _plan(rows=2),
        properties={"annotation.project_id": "p1"},
        run_id="pub-1",
    )

    assert version == 9
    call = create.calls[0]
    assert call["table_id"] == "silver$vasa_0123456789ab"
    assert call["mode"] == "exist_ok", "the saga's retry safety RELIES on exist_ok — any other mode re-creates or errors on replay"
    assert json.loads(call["properties"]) == {"annotation.project_id": "p1"}
    headers = call["_headers"]
    assert (headers["x-rask-run-id"], "x-rask-inputs" in headers) == ("pub-1", False), "an absent pin names no input (§7.2 — never fabricate)"
    assert headers["Authorization"] == "Bearer svc-token"
    # The service's bearer authors the events, so this header is the only place the human survives.
    assert headers["x-lance-originator"] == "alice"
    # The body must be a readable Arrow-IPC STREAM carrying the published schema — an IPC file, or
    # inferred types, would fail the catalog's reader or type an all-sentinel publish as nulls.
    table = pa.ipc.open_stream(call["body"]).read_all()
    assert table.schema.names == list(PUBLISHED_LABELS_SCHEMA.names)
    assert table.num_rows == 2


@pytest.mark.asyncio
async def test_a_duplicate_tag_at_our_version_converges() -> None:
    """The catalog has no exist-ok tag; convergence is THIS class's job. A crashed attempt that
    already tagged must read as success on retry, or the saga can never finish."""
    tags = _Tags()
    tags.tags[("silver$t_1", "publish-abc")] = 7
    publisher = _publisher(tags=tags)

    await publisher.tag_version("silver$t_1", 7, "publish-abc")  # must not raise


@pytest.mark.asyncio
async def test_a_duplicate_tag_at_a_different_version_fails_loudly() -> None:
    """A tag pointing elsewhere means a DIFFERENT publish owns the name. Adopting it would record
    our provenance against someone else's dataset version."""
    tags = _Tags()
    tags.tags[("silver$t_1", "publish-abc")] = 3
    publisher = _publisher(tags=tags)

    with pytest.raises(RuntimeError, match="currently points at 3"):
        await publisher.tag_version("silver$t_1", 7, "publish-abc")


@pytest.mark.asyncio
async def test_a_fresh_tag_is_created() -> None:
    tags = _Tags()
    publisher = _publisher(tags=tags)

    await publisher.tag_version("silver$t_1", 7, "publish-abc")

    assert tags.tags == {("silver$t_1", "publish-abc"): 7}


@pytest.mark.asyncio
async def test_the_pin_reaches_the_create_as_the_runs_input() -> None:
    """§7.2 over the wire: the one pinned source is the run's input in the catalog Context."""
    create = _Create()
    publisher = _publisher(create)

    await publisher.create_table("silver$t_1", _plan(), properties={}, run_id="pub-1", source="demo$pages", source_version=24)

    assert json.loads(create.calls[0]["_headers"]["x-rask-inputs"]) == [{"table": ["demo", "pages"], "version": 24}]


# --------------------------------------------------------------------------------------------------
# _HttpCreateApi — the direct-HTTP create transport itself (the audit found it previously untested behind the
# injected fake)
# --------------------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._body


def test_the_http_create_api_sends_the_context_the_arrow_type_and_quotes_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from annotator.projects import lakehouse
    from annotator.projects.lakehouse import _HttpCreateApi

    seen: dict[str, Any] = {}

    def fake_post(url: str, *, params: dict[str, str], content: bytes, headers: dict[str, str], timeout: float) -> _Resp:
        seen.update(url=url, params=params, content=content, headers=headers)
        return _Resp(200, {"version": 7})

    monkeypatch.setattr(lakehouse, "publish_client", lambda _timeout: _StubClient(fake_post))
    api = _HttpCreateApi("http://catalog:2333/")

    resp = api.create_table("silver$vasa_0123456789ab", b"ARROW", mode="exist_ok", properties="{}", _headers={"x-rask-run-id": "pub-1"})

    assert resp.version == 7
    # `$` is the catalog delimiter INSIDE the id — it must travel percent-encoded, not as a path split.
    assert seen["url"] == "http://catalog:2333/v1/table/silver%24vasa_0123456789ab/create"
    assert seen["params"]["mode"] == "exist_ok"
    assert seen["headers"]["content-type"] == "application/vnd.apache.arrow.stream"
    assert seen["headers"]["x-rask-run-id"] == "pub-1"
    assert seen["content"] == b"ARROW"


def test_the_http_create_api_raises_the_sdk_exception_on_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """>=400 becomes the SDK's ApiException, so `translate_catalog_errors` maps it exactly like an
    SDK failure — the saga's retry semantics see ONE error surface regardless of transport."""
    from lance_namespace_urllib3_client.exceptions import ApiException

    from annotator.projects import lakehouse
    from annotator.projects.lakehouse import _HttpCreateApi

    monkeypatch.setattr(lakehouse, "publish_client", lambda _timeout: _StubClient(lambda url, **kw: _Resp(502, text="upstream unreachable")))

    with pytest.raises(ApiException) as err:
        _HttpCreateApi("http://catalog:2333").create_table("silver$t", b"A")
    assert err.value.status == 502


# --------------------------------------------------------------------------------------------------
# publish_token — the saga's catalog identity (minted fresh per publish; secrets from the store)
# --------------------------------------------------------------------------------------------------


def _token_settings() -> Any:
    """The annotator's real settings, so the publish path reads the fields production builds: a field the settings drop
    or a secret name they gain reaches this test, where a hand-made double would keep its own."""
    from annotator.core.config import AnnotatorSettings

    return AnnotatorSettings()


# `test_a_pinned_catalog_token_wins_and_touches_nothing_else` lived here and is DELETED, not adapted.
# It asserted that a pinned `MEDIA_CATALOG_TOKEN` beats the mint — the precedence removed when the
# confused-deputy finding deleted that field ("open_reader/open_writer fall back to the estate's
# catalog service credential"). Two independent reasons it should not come back: a pinned estate-wide
# credential as a publish IDENTITY makes every published row carry the platform's name instead of the
# publisher's; and `publish_token`'s own docstring records that a hand-pinned token EXPIRED in
# production, which is why the mint is per-publish. The behaviour it pinned is unreachable — the field
# does not exist on the real settings class.


def test_minting_reads_the_store_and_posts_the_password_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The password comes from the Dapr secret store (fail-closed, sole source) and the grant carries
    the service account — a FRESH token per publish, so nothing stored can ever go stale."""
    from annotator.projects import lakehouse
    from service_kit.governed import secrets as sk_secrets

    settings = _token_settings()
    settings.publish_token_url = "http://dex:5556/dex/token"
    settings.publish_client_id = "lance-catalog"
    settings.publish_username = "publisher@rask.internal"

    fetched: list[tuple[str, str, str]] = []

    def fake_fetch(store: str, key: str, *, require: str) -> dict[str, str]:
        fetched.append((store, key, require))
        return {"password": "s3cret"} if require == "password" else {"client-secret": "the-idp-client-secret"}

    def optional_read(store: str, key: str) -> dict[str, str]:
        raise AssertionError(f"{store}/{key} was read as optional, so a store that holds nothing falls through to a fallback")

    posted: dict[str, Any] = {}

    def fake_post(url: str, *, data: dict[str, str], auth: Any) -> Any:
        posted.update(url=url, data=data, auth=auth)
        return _Resp(200, {"id_token": "minted-token"})

    monkeypatch.setattr(sk_secrets, "fetch_required_secrets", fake_fetch)
    monkeypatch.setattr(sk_secrets, "fetch_dapr_secret", optional_read)
    monkeypatch.setattr(lakehouse, "publish_client", lambda _timeout: _StubClient(fake_post))

    assert lakehouse.publish_token(settings) == "minted-token"
    assert fetched == [("lance-secrets", "publisher-oidc", "password"), ("lance-secrets", "oidc-client", "client-secret")]
    assert posted["url"] == "http://dex:5556/dex/token"
    assert posted["data"]["grant_type"] == "password"
    assert posted["data"]["username"] == "publisher@rask.internal"
    assert posted["data"]["password"] == "s3cret"
    assert posted["auth"] == ("lance-catalog", "the-idp-client-secret")


def test_an_idp_refusal_raises_with_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong password or sealed store is an OPERATOR's problem — it must surface as publish_failed
    with the IdP's words, never a stranded `publishing`."""
    from annotator.projects import lakehouse
    from service_kit.governed import secrets as sk_secrets

    settings = _token_settings()
    settings.publish_token_url = "http://dex:5556/dex/token"
    settings.publish_username = "publisher@rask.internal"

    monkeypatch.setattr(sk_secrets, "fetch_required_secrets", lambda *a, **k: {"password": "wrong"})
    monkeypatch.setattr(lakehouse, "publish_client", lambda _timeout: _StubClient(lambda url, **kw: _Resp(401, text="invalid credentials")))

    with pytest.raises(RuntimeError, match="401"):
        lakehouse.publish_token(settings)
