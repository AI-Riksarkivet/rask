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
from annotator.projects.lakehouse import RUN_FACETS_HEADER, CatalogPublisher
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
async def test_create_posts_exist_ok_with_schema_typed_rows_and_the_facet_header() -> None:
    create = _Create(version=9)
    publisher = _publisher(create, token="svc-token")

    version = await publisher.create_table(
        "silver$vasa_0123456789ab",
        _plan(rows=2),
        properties={"annotation.project_id": "p1"},
        run_facet={"annotationProject": {"projectId": "p1"}},
    )

    assert version == 9
    call = create.calls[0]
    assert call["table_id"] == "silver$vasa_0123456789ab"
    assert call["mode"] == "exist_ok", "the saga's retry safety RELIES on exist_ok — any other mode re-creates or errors on replay"
    assert json.loads(call["properties"]) == {"annotation.project_id": "p1"}
    headers = call["_headers"]
    assert json.loads(headers[RUN_FACETS_HEADER]) == {"annotationProject": {"projectId": "p1"}}
    assert headers["Authorization"] == "Bearer svc-token"
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
async def test_facet_payloads_ride_bare_of_spec_stamps() -> None:
    """`project_facet` returns a spec-stamped facet (`_producer`, `_schemaURL` — custom_facet adds
    them), but the `X-Lance-Run-Facets` contract wants BARE payloads: the catalog stamps each facet
    itself and 400s any `_`-prefixed or `producer` key. The live drive found this as a publish that
    could never succeed; the stripping is THIS transport's job because the stamping is its far end's."""
    create = _Create()
    publisher = _publisher(create)

    await publisher.create_table(
        "silver$t_1",
        _plan(),
        properties={},
        run_facet={"annotationProject": {"_producer": "x", "_schemaURL": "y", "producer": "z", "projectId": "p1"}},
    )

    sent = json.loads(create.calls[0]["_headers"][RUN_FACETS_HEADER])
    assert sent == {"annotationProject": {"projectId": "p1"}}, sent


@pytest.mark.asyncio
async def test_the_pin_params_reach_the_create_call() -> None:
    """§7.2 over the wire: the S4 `source`/`source_version` query params ride the direct-HTTP
    create (the spec-generated SDK cannot send them — OPEN-WORK §B3)."""
    create = _Create()
    publisher = _publisher(create)

    await publisher.create_table(
        "silver$t_1",
        _plan(),
        properties={},
        run_facet={},
        source="demo",
        source_version=24,
    )

    call = create.calls[0]
    assert call["source"] == "demo"
    assert call["source_version"] == 24


@pytest.mark.asyncio
async def test_no_pin_sends_no_pin_params() -> None:
    create = _Create()
    publisher = _publisher(create)

    await publisher.create_table("silver$t_1", _plan(), properties={}, run_facet={})

    call = create.calls[0]
    assert call["source"] is None and call["source_version"] is None


# --------------------------------------------------------------------------------------------------
# _HttpCreateApi — the direct-HTTP create transport itself (the SDK client cannot send the S4 pin
# params, so this class IS the wire; the audit found it previously untested behind the injected fake)
# --------------------------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._body


def test_the_http_create_api_sends_the_pin_params_the_arrow_type_and_quotes_the_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from annotator.projects.lakehouse import _HttpCreateApi

    seen: dict[str, Any] = {}

    def fake_post(url: str, *, params: dict[str, str], content: bytes, headers: dict[str, str], timeout: float) -> _Resp:
        seen.update(url=url, params=params, content=content, headers=headers)
        return _Resp(200, {"version": 7})

    monkeypatch.setattr(httpx, "post", fake_post)
    api = _HttpCreateApi("http://catalog:2333/")

    resp = api.create_table(
        "silver$vasa_0123456789ab", b"ARROW", mode="exist_ok", properties="{}", source="demo", source_version=24, _headers={RUN_FACETS_HEADER: "{}"}
    )

    assert resp.version == 7
    # `$` is the catalog delimiter INSIDE the id — it must travel percent-encoded, not as a path split.
    assert seen["url"] == "http://catalog:2333/v1/table/silver%24vasa_0123456789ab/create"
    assert seen["params"]["source"] == "demo"
    assert seen["params"]["source_version"] == "24"
    assert seen["params"]["mode"] == "exist_ok"
    assert seen["headers"]["content-type"] == "application/vnd.apache.arrow.stream"
    assert seen["headers"][RUN_FACETS_HEADER] == "{}"
    assert seen["content"] == b"ARROW"


def test_the_http_create_api_omits_the_pin_params_when_there_is_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from annotator.projects.lakehouse import _HttpCreateApi

    seen: dict[str, Any] = {}

    def fake_post(url: str, *, params: dict[str, str], content: bytes, headers: dict[str, str], timeout: float) -> _Resp:
        seen.update(params=params)
        return _Resp(200, {"version": 3})

    monkeypatch.setattr(httpx, "post", fake_post)
    _HttpCreateApi("http://catalog:2333").create_table("silver$t", b"A", mode="exist_ok")

    assert "source" not in seen["params"] and "source_version" not in seen["params"], "an absent pin must stay absent on the wire (§7.2 — never fabricate)"


def test_the_http_create_api_raises_the_sdk_exception_on_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """>=400 becomes the SDK's ApiException, so `translate_catalog_errors` maps it exactly like an
    SDK failure — the saga's retry semantics see ONE error surface regardless of transport."""
    import httpx
    from annotator.projects.lakehouse import _HttpCreateApi
    from lance_namespace_urllib3_client.exceptions import ApiException

    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(502, text="upstream unreachable"))

    with pytest.raises(ApiException) as err:
        _HttpCreateApi("http://catalog:2333").create_table("silver$t", b"A")
    assert err.value.status == 502
