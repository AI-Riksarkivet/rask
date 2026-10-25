"""The concrete CatalogPublisher — the transport half of the publish saga.

`Publisher` is a Protocol precisely so the saga's orchestration tests need no catalog; these are the
tests for the OTHER side of that seam. Driven against a structural fake of the create transport
(the estate's Ray-client pattern), so what is pinned here is the CONTRACT this class holds up to the
saga:

- create posts `mode=exist_ok` with the plan's rows as an Arrow-IPC stream typed by the published
  schema, the properties JSON-encoded, and the run facet on `X-Lance-Run-Facets`.
- every call presents the annotator's own projected ServiceAccount token, read again for that call
  because the kubelet replaces it before it expires, and names the person the publish is for on
  `x-lance-originator`: the service authors every event the publish produces, so the header is the
  only place the human survives (ADR 0171).
"""

from __future__ import annotations

import json
from pathlib import Path
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


def _publisher(create: _Create | None = None, **kw: Any) -> CatalogPublisher:
    return CatalogPublisher("http://catalog:2333", data_api=create or _Create(), **kw)


def _token(path: Path, value: str) -> Path:
    path.write_text(value)
    return path


@pytest.mark.asyncio
async def test_create_posts_exist_ok_with_schema_typed_rows_and_its_run(tmp_path: Path) -> None:
    create = _Create(version=9)
    publisher = _publisher(create, token_file=_token(tmp_path / "token", "svc-token"), originator="alice")

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


@pytest.mark.asyncio
async def test_every_publish_call_presents_the_annotators_own_token_read_again_and_names_the_person(tmp_path: Path) -> None:
    """The token as it is when each call is made, and the person the publish is for.

    The kubelet replaces the projected token before each one expires, so a token read once at construction would present
    an expired credential to every call after the first.
    """
    create = _Create(version=4)
    token = _token(tmp_path / "token", "token-1")
    publisher = _publisher(create, token_file=token, originator="dex~gina")

    await publisher.create_table("acme-silver$labels_p1", _plan(), properties={}, run_id="pub-1")
    _token(token, "token-2")
    await publisher.create_table("acme-silver$labels_p1", _plan(), properties={}, run_id="pub-1")

    presented = [(call["_headers"]["Authorization"], call["_headers"]["x-lance-originator"]) for call in create.calls]
    assert presented == [("Bearer token-1", "dex~gina"), ("Bearer token-2", "dex~gina")]
