"""A plan door that answers 4xx is telling THIS executor its request is malformed — it is not an outage.

`plan_via_catalog` has four kinds of answer and only one licenses the in-pod fallback:

    connection error / 5xx / unparseable    the plane could not answer   -> fall back, counted by mode
    401 / 403                               the answer is NO             -> leave the dataset alone
    404 TableNotFound / NamespaceNotFound   no table by this id          -> leave the dataset alone
    any other 4xx                           our request is wrong         -> skip the rewrite, loudly

A 4xx read as an outage would turn an executor bug into an in-pod rewrite logged at INFO, visible only
as a `compaction_mode` count. So a refused request logs at ERROR with the table, the status and the
problem detail, counts on `compaction.plan.refused`, and that table is not rewritten on that tick. Only
the rewrite: index optimize and version cleanup still run, because the request is what is wrong, not
the table. 408 and 429 stay outages: both ask the caller to retry and say nothing about the request.

A 404 naming the table or namespace absent answers about the TABLE (spec.yaml `ErrorResponse.code`: 13
is the malformed request, 1 and 4 are absences). It is the condition the vend door answers 403 when the
catalog's gate runs first, so it gets that refusal's outcome: nothing is maintained under an id the
catalog does not govern.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import respx
from lance_namespace.errors import ErrorCode

from maintenance.core.config import MaintenanceSettings
from maintenance.services import catalog_compaction, optimize, sweep
from maintenance.services import compaction_executor as ce
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
PLAN_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_plan"
# The door's own bodies, as the catalog app answers each case.
PROBLEM = {
    "type": "https://lance.org/problems/invalidinputerror",
    "title": "InvalidInputError",
    "status": 400,
    "code": 13,
    "detail": "unsupported compaction option(s) ['io_buffer_size']",
}
VALIDATION = {"type": "https://lance.org/problems/validation", "title": "Validation Error", "status": 422, "code": 13, "detail": "Validation Error"}
ROUTING_MISS = {"type": "https://lance.org/problems/not-found", "title": "Not Found", "status": 404, "code": 0, "detail": "Not Found"}
TABLE_NOT_FOUND = {
    "type": "https://lance.org/problems/tablenotfounderror",
    "title": "TableNotFoundError",
    "status": 404,
    "code": 4,
    "detail": f"Table not found: table id '{TABLE_ID}'",
}
NAMESPACE_NOT_FOUND = {
    "type": "https://lance.org/problems/namespacenotfounderror",
    "title": "NamespaceNotFoundError",
    "status": 404,
    "code": 1,
    "detail": "Namespace not found: Child namespace reads require an existing __manifest dataset",
}
PERMISSION_DENIED = {
    "type": "https://lance.org/problems/permissiondeniederror",
    "title": "PermissionDeniedError",
    "status": 403,
    "code": 15,
    "detail": f"permission denied: can_maintain on table:{TABLE_ID}",
}
#: JSON that is not a problem object, and a problem object with no `detail`: the body's head stands in.
NOT_AN_OBJECT = [PROBLEM]
NO_DETAIL = {key: value for key, value in PROBLEM.items() if key != "detail"}
#: A 4xx from something in front of the door, which sends no problem body.
HTML_400 = "<html><head><title>400 Bad Request</title></head><body><center><h1>400 Bad Request</h1></center><hr><center>nginx</center></body></html>" * 2


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings.model_validate({"s3_bucket": "lake", "distributed_compaction": True, "catalog_url": CATALOG})


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_compaction, "service_headers", lambda _settings: {})


def _fragmented(tmp_path: Path) -> str:
    """Six fragments, stamped with `TABLE_ID` so the sweep takes the distributed path for it."""
    uri = str(tmp_path / "t.lance")
    for i in range(6):
        rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())}).replace_schema_metadata({b"lineage.dataset_id": TABLE_ID.encode()})
        lance.write_dataset(rows, uri, mode="create" if i == 0 else "append")
    return uri


def _execute(uri: str, monkeypatch: pytest.MonkeyPatch, plan: DatasetPlan | None = None) -> Any:
    monkeypatch.setattr(sweep.credentials, "write_options_for", lambda *_a, **_k: {})
    return sweep.execute_unit(DatasetWorkItem(uri=uri, plan=plan or DatasetPlan()), settings=_settings(), options={}, now=datetime.now(UTC))


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        pytest.param(httpx.Response(400, json=PROBLEM), PROBLEM["detail"], id="400-invalid-input"),
        pytest.param(httpx.Response(422, json=VALIDATION), VALIDATION["detail"], id="422-validation"),
        pytest.param(httpx.Response(409, json={**PROBLEM, "status": 409, "code": 14}), PROBLEM["detail"], id="409-any-other-4xx"),
        pytest.param(httpx.Response(404, json=ROUTING_MISS), ROUTING_MISS["detail"], id="404-a-path-no-door-serves"),
        # An absence code names a missing table only on the status the spec maps it to.
        pytest.param(httpx.Response(409, json={**TABLE_NOT_FOUND, "status": 409}), TABLE_NOT_FOUND["detail"], id="409-carrying-an-absence-code"),
        pytest.param(httpx.Response(400, json={**NAMESPACE_NOT_FOUND, "status": 400}), NAMESPACE_NOT_FOUND["detail"], id="400-carrying-an-absence-code"),
        pytest.param(httpx.Response(400, text=HTML_400, headers={"content-type": "text/html"}), HTML_400[:200], id="400-not-json"),
        pytest.param(httpx.Response(400, json=NOT_AN_OBJECT), httpx.Response(400, json=NOT_AN_OBJECT).text[:200], id="400-json-that-is-not-an-object"),
        pytest.param(httpx.Response(400, json=NO_DETAIL), httpx.Response(400, json=NO_DETAIL).text[:200], id="400-a-problem-with-no-detail"),
    ],
)
def test_a_4xx_plan_answer_is_the_executors_bug_not_an_outage(answer: httpx.Response, detail: str, caplog: pytest.LogCaptureFixture) -> None:
    with respx.mock() as router, caplog.at_level(logging.ERROR, logger=catalog_compaction.__name__):
        router.post(PLAN_URL).mock(return_value=answer)
        with pytest.raises(Exception) as caught:
            catalog_compaction.plan_via_catalog(TABLE_ID, {}, settings=_settings())

    assert not isinstance(caught.value, ce.CompactionPlaneUnavailable), (
        f"a {answer.status_code} was read as an outage, which the sweep answers with an in-pod rewrite"
    )
    assert isinstance(caught.value, ce.CompactionPlanRefused), type(caught.value)
    logged = [record for record in caplog.records if record.getMessage() == "compaction_plan_refused"]
    assert len(logged) == 1, [record.getMessage() for record in caplog.records]
    fields = {name: getattr(logged[0], name, None) for name in ("table_id", "status", "detail")}
    assert fields == {"table_id": TABLE_ID, "status": answer.status_code, "detail": detail}, fields


@pytest.mark.parametrize("body", [pytest.param(TABLE_NOT_FOUND, id="TableNotFound"), pytest.param(NAMESPACE_NOT_FOUND, id="NamespaceNotFound")])
def test_a_table_the_catalog_does_not_govern_is_not_the_executors_bug(body: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    with respx.mock() as router, caplog.at_level(logging.ERROR, logger=catalog_compaction.__name__):
        router.post(PLAN_URL).mock(return_value=httpx.Response(404, json=body))
        with pytest.raises(Exception) as caught:
            catalog_compaction.plan_via_catalog(TABLE_ID, {}, settings=_settings())

    assert type(caught.value).__name__ == "TableNotGoverned", f"a 404 {body['title']} answers about the table, not the request: {caught.value!r}"
    # The door's own answer, not a diagnosis: code 4 also answers a governed table whose dataset was never written.
    code = ErrorCode(int(body["code"]))
    assert str(caught.value) == f"the catalog answered 404 {code.name} (code {code.value}) for {TABLE_ID}: {body['detail']}", str(caught.value)
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_an_ungoverned_table_is_neither_an_outage_nor_a_refused_request() -> None:
    """Either parent would put it on a path that handles it: the in-pod fallback, or the rewrite-only skip."""
    assert not issubclass(ce.TableNotGoverned, ce.CompactionPlaneUnavailable | ce.CompactionPlanRefused | ce.MaintenanceDenied)


def test_a_refusal_is_not_an_unavailability_by_inheritance() -> None:
    """Every fallback `except` catches `CompactionPlaneUnavailable`; a subclass would fall back again."""
    assert not issubclass(ce.CompactionPlanRefused, ce.CompactionPlaneUnavailable)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(httpx.Response(500, json={"detail": "boom"}), id="500"),
        pytest.param(httpx.Response(503, json={"detail": "down"}), id="503"),
        pytest.param(httpx.Response(408, json={"detail": "slow"}), id="408"),
        pytest.param(httpx.Response(429, json={"detail": "busy"}), id="429"),
        pytest.param(httpx.Response(200, content=b"<html>not json</html>"), id="an-unparseable-body"),
        pytest.param(httpx.ConnectError("refused"), id="a-connection-error"),
    ],
)
def test_an_unavailable_plane_stays_an_outage(answer: httpx.Response | Exception) -> None:
    with respx.mock() as router:
        route = router.post(PLAN_URL)
        if isinstance(answer, Exception):
            route.mock(side_effect=answer)
        else:
            route.mock(return_value=answer)
        with pytest.raises(ce.CompactionPlaneUnavailable):
            catalog_compaction.plan_via_catalog(TABLE_ID, {}, settings=_settings())


def test_a_refused_plan_leaves_the_table_unrewritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = _fragmented(tmp_path)

    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=httpx.Response(400, json=PROBLEM))
        result = _execute(uri, monkeypatch)

    assert len(lance.dataset(uri).get_fragments()) == 6, "the refused plan was answered with an in-pod rewrite"
    assert result.error is not None and result.error_type == "CompactionPlanRefused", (result.error, result.error_type)


def test_a_denied_plan_is_refused_and_never_rewritten_in_pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 403 is the answer NO. An in-pod rewrite, or a cleanup, would run under the credential that
    opened the dataset, which on a denied table is the ambient key. So nothing runs."""
    uri = _fragmented(tmp_path)

    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=httpx.Response(403, json=PERMISSION_DENIED))
        result = _execute(uri, monkeypatch, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1))

    assert len(lance.dataset(uri).get_fragments()) == 6, "the denied rewrite was performed in-pod"
    assert result.refused is not None and TABLE_ID in result.refused, f"the denial was not recorded as a refusal: {result.refused!r}"
    assert (result.refused_by, result.compaction_mode, result.error) == ("plan_denied", "in_pod", None), (
        result.refused_by,
        result.compaction_mode,
        result.error,
    )
    assert len(lance.dataset(uri).versions()) == 6, "a denied table had its versions cleaned up"


def test_a_refused_plan_skips_only_the_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The request is what is wrong, not the table, so its versions are still reclaimed."""
    uri = _fragmented(tmp_path)
    assert len(lance.dataset(uri).versions()) == 6

    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=httpx.Response(400, json=PROBLEM))
        result = _execute(uri, monkeypatch, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1))

    assert len(lance.dataset(uri).get_fragments()) == 6, "the refused plan was answered with an in-pod rewrite"
    assert result.error_type == "CompactionPlanRefused", (result.error, result.error_type)
    assert result.old_versions_removed == 5, f"the refused request cost the table its version cleanup: {result.error}"
    assert len(lance.dataset(uri).versions()) == 1


class _LineageEmitter:
    """Records which lane each table reached."""

    def __init__(self) -> None:
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def emit_maintenance(self, *, table_id: str, namespace: str, operation: str = "compaction") -> None:
        self.completed.append(table_id)

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, operation: str = "compaction") -> None:
        self.failed.append(table_id)


def test_the_cleanup_a_refused_plan_still_runs_reaches_the_lineage_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reclaimed versions are a maintenance run on the table, and a rewrite skipped for the request is not its failure."""
    uri = _fragmented(tmp_path)

    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=httpx.Response(400, json=PROBLEM))
        result = _execute(uri, monkeypatch, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1))
    assert (result.error_type, result.old_versions_removed) == ("CompactionPlanRefused", 5), "the fixture did no cleanup, so nothing below is tested"
    emitter = _LineageEmitter()
    asyncio.run(sweep.emit_sweep_lineage(emitter, [result], delimiter="$"))

    assert (emitter.completed, emitter.failed) == ([TABLE_ID], []), (emitter.completed, emitter.failed)


@pytest.mark.parametrize("body", [pytest.param(TABLE_NOT_FOUND, id="TableNotFound"), pytest.param(NAMESPACE_NOT_FOUND, id="NamespaceNotFound")])
def test_an_ungoverned_table_is_left_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
    refused_added: list[tuple[int, dict[str, Any] | None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Refused, not failed, and nothing runs: what would sign the cleanup is the ambient credential.

    The refused counter and the WARNING are the only signals this outcome has, since it does not page.
    """
    uri = _fragmented(tmp_path)

    with respx.mock() as router, caplog.at_level(logging.DEBUG, logger=optimize.__name__):
        router.post(PLAN_URL).mock(return_value=httpx.Response(404, json=body))
        result = _execute(uri, monkeypatch, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1))

    assert (result.error, result.refused_by) == (None, "table_not_governed"), (result.error, result.refused_by, result.refused)
    assert result.refused is not None and TABLE_ID in result.refused, f"the refusal carries no reason: {result.refused!r}"
    assert refused_added == [(1, {"refused_by": "table_not_governed"})], f"the refusal did not reach the refused counter: {refused_added}"
    warned = [record.levelno for record in caplog.records if record.getMessage() == "maintenance_table_not_governed"]
    assert warned == [logging.WARNING], f"the refusal was not logged once at WARNING: {warned}"
    assert len(lance.dataset(uri).get_fragments()) == 6, "an ungoverned table was rewritten"
    assert len(lance.dataset(uri).versions()) == 6, "an ungoverned table's versions were cleaned up"


def test_an_unavailable_plane_still_falls_back_in_pod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    uri = _fragmented(tmp_path)

    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=httpx.Response(503, json={"detail": "down"}))
        result = _execute(uri, monkeypatch)

    assert result.error is None, result.error
    assert result.compaction_mode == "in_pod"
    assert len(lance.dataset(uri).get_fragments()) == 1, "the outage did not fall back to the in-pod rewrite"


@pytest.mark.parametrize(
    ("answer", "counted"),
    [
        pytest.param(httpx.Response(400, json=PROBLEM), 1, id="400"),
        pytest.param(httpx.Response(503, json={**PROBLEM, "status": 503, "code": 17}), 0, id="503"),
        pytest.param(httpx.Response(404, json=TABLE_NOT_FOUND), 0, id="404-no-such-table"),
    ],
)
def test_each_unit_counts_whether_its_plan_was_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan_refused_added: list[int], answer: httpx.Response, counted: int
) -> None:
    uri = _fragmented(tmp_path)

    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=answer)
        _execute(uri, monkeypatch)

    assert plan_refused_added == [counted], plan_refused_added


def test_a_refused_plan_is_counted_when_a_later_step_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan_refused_added: list[int]) -> None:
    """Cleanup runs after a refused request, and its failure takes `error_type`; the count must not follow it."""
    uri = _fragmented(tmp_path)

    def _cleanup_breaks(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("cleanup broke")

    monkeypatch.setattr(optimize, "_reclaim_versions", _cleanup_breaks)
    with respx.mock() as router:
        router.post(PLAN_URL).mock(return_value=httpx.Response(400, json=PROBLEM))
        _execute(uri, monkeypatch)

    assert plan_refused_added == [1], plan_refused_added


def test_each_tick_emits_the_zero_baseline(monkeypatch: pytest.MonkeyPatch, plan_refused_added: list[int]) -> None:
    """An estate whose planner enqueues nothing runs no unit, so the per-tick zero is what creates the series."""
    monkeypatch.setattr(sweep, "_load_policies", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep, "_trash_exclusions", lambda *_a, **_k: {})
    monkeypatch.setattr(sweep, "_s3fs", lambda *_a, **_k: object())
    monkeypatch.setattr(sweep, "_buckets_to_sweep", lambda *_a, **_k: ["b"])
    monkeypatch.setattr(sweep, "_discover_all", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep, "_protected_roots", lambda *_a, **_k: base_refs.BaseRefs())

    assert sweep.plan_sweep(_settings()) == ([], []), "the fixture planned work, so the zero below could come from a unit"
    assert plan_refused_added == [0], plan_refused_added
