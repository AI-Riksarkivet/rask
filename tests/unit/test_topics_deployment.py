from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


TOPICS_DIR = Path(__file__).resolve().parents[2] / "runners" / "topics"


class _Request:
    def __init__(self, body: Any, authorization: str | None = None) -> None:
        self._body = body
        self.headers = {} if authorization is None else {"authorization": authorization}
        self.json_calls = 0

    async def json(self) -> Any:
        self.json_calls += 1
        return self._body


class _MalformedRequest(_Request):
    async def json(self) -> Any:
        self.json_calls += 1
        raise ValueError("malformed JSON")


def _load_deployment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None = "serve-secret",
    db_path: str | None = r"C:\datasets\topics",
    llm_url: str | None = "http://models.internal/v1",
) -> tuple[Any, list[dict[str, str | None]]]:
    if token is None:
        monkeypatch.delenv("MEDIA_TOPICS_SERVE_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MEDIA_TOPICS_SERVE_TOKEN", token)
    if db_path is None:
        monkeypatch.delenv("MEDIA_TOPICS_DB_PATH", raising=False)
    else:
        monkeypatch.setenv("MEDIA_TOPICS_DB_PATH", db_path)
    if llm_url is None:
        monkeypatch.delenv("MEDIA_TOPICS_LLM_URL", raising=False)
    else:
        monkeypatch.setenv("MEDIA_TOPICS_LLM_URL", llm_url)

    calls: list[dict[str, str | None]] = []
    worker = ModuleType("worker")

    def run(*, db_path: str, llm_url: str | None) -> int:
        calls.append({"db_path": db_path, "llm_url": llm_url})
        return 17

    worker.__dict__["run"] = run

    class _Deployment:
        def __init__(self, target: type[Any]) -> None:
            self.func_or_class = target

        def bind(self) -> _Deployment:
            return self

    class _Serve:
        @staticmethod
        def deployment(**kwargs: object) -> Callable[[type[Any]], _Deployment]:
            def decorate(target: type[Any]) -> _Deployment:
                return _Deployment(target)

            return decorate

    class _JSONResponse:
        def __init__(
            self,
            content: Any,
            status_code: int,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.content = content
            self.status_code = status_code
            self.headers = {key.casefold(): value for key, value in (headers or {}).items()}

    ray = ModuleType("ray")
    ray.__dict__["serve"] = _Serve
    starlette = ModuleType("starlette")
    responses = ModuleType("starlette.responses")
    responses.__dict__["JSONResponse"] = _JSONResponse
    monkeypatch.setitem(sys.modules, "worker", worker)
    monkeypatch.setitem(sys.modules, "ray", ray)
    monkeypatch.setitem(sys.modules, "starlette", starlette)
    monkeypatch.setitem(sys.modules, "starlette.responses", responses)

    spec = importlib.util.spec_from_file_location("_topics_deployment_under_test", TOPICS_DIR / "deployment.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module.__dict__["TopicsDeployment"].func_or_class(), calls


def _load_worker(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("_topics_worker_under_test", TOPICS_DIR / "worker.py")
    assert spec is not None and spec.loader is not None
    worker = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, worker)
    spec.loader.exec_module(worker)
    return worker


@pytest.mark.parametrize("authorization", [None, "Basic serve-secret", "Bearer wrong"])
def test_http_ingress_rejects_unauthorized_callers_before_parsing_or_build(
    monkeypatch: pytest.MonkeyPatch,
    authorization: str | None,
) -> None:
    deployment, calls = _load_deployment(monkeypatch)
    request = _Request({"db": "s3://other-tenant/private", "llm_url": "http://attacker/"}, authorization)

    response = asyncio.run(deployment(request))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert request.json_calls == 0
    assert calls == []


@pytest.mark.parametrize("token", [None, "", "   "])
def test_http_ingress_fails_closed_when_server_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    deployment, calls = _load_deployment(monkeypatch, token=token)

    response = asyncio.run(deployment(_Request({}, "Bearer anything")))

    assert response.status_code == 503
    assert calls == []


@pytest.mark.parametrize("db_path", [None, "", "   "])
def test_http_ingress_fails_closed_when_server_database_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    db_path: str | None,
) -> None:
    deployment, calls = _load_deployment(monkeypatch, db_path=db_path)

    response = asyncio.run(deployment(_Request({}, "Bearer serve-secret")))

    assert response.status_code == 503
    assert calls == []


def test_http_ingress_rejects_request_selected_database_and_llm_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment, calls = _load_deployment(monkeypatch)

    response = asyncio.run(deployment(_Request({"db": "s3://other-tenant/private", "llm_url": "http://attacker/"}, "Bearer serve-secret")))

    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("body", [None, [], "not an object"])
def test_http_ingress_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    deployment, calls = _load_deployment(monkeypatch)

    response = asyncio.run(deployment(_Request(body, "Bearer serve-secret")))

    assert response.status_code == 400
    assert calls == []


def test_http_ingress_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment, calls = _load_deployment(monkeypatch)
    request = _MalformedRequest(None, "Bearer serve-secret")

    response = asyncio.run(deployment(request))

    assert response.status_code == 400
    assert request.json_calls == 1
    assert calls == []


def test_http_ingress_builds_only_the_configured_dataset_and_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment, calls = _load_deployment(monkeypatch)

    response = asyncio.run(deployment(_Request({}, "Bearer serve-secret")))

    assert response == {"rows": 17}
    assert calls == [{"db_path": r"C:\datasets\topics", "llm_url": "http://models.internal/v1"}]


def test_http_ingress_uses_the_worker_default_when_llm_configuration_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, calls = _load_deployment(monkeypatch, llm_url=None)

    response = asyncio.run(deployment(_Request({}, "Bearer serve-secret")))

    assert response == {"rows": 17}
    assert calls == [{"db_path": r"C:\datasets\topics", "llm_url": None}]


def test_http_ingress_serializes_concurrent_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    deployment, calls = _load_deployment(monkeypatch)
    worker = sys.modules["worker"]
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def run(*, db_path: str, llm_url: str | None) -> int:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            is_first = active == 1
            maximum_active = max(maximum_active, active)
            if is_first:
                first_started.set()
            else:
                second_started.set()
        if is_first:
            release_first.wait(timeout=2)
        else:
            time.sleep(0.02)
        with state_lock:
            active -= 1
        calls.append({"db_path": db_path, "llm_url": llm_url})
        return 17

    worker.__dict__["run"] = run

    async def invoke_concurrently() -> tuple[Any, Any, bool]:
        request = _Request({}, "Bearer serve-secret")
        first = asyncio.create_task(deployment(request))
        await asyncio.to_thread(first_started.wait, 1)
        second = asyncio.create_task(deployment(request))
        await asyncio.sleep(0.05)
        saw_overlap = second_started.is_set()
        release_first.set()
        responses = await asyncio.gather(first, second)
        return responses[0], responses[1], saw_overlap

    try:
        first_response, second_response, saw_overlap = asyncio.run(invoke_concurrently())
    finally:
        release_first.set()

    assert first_response == {"rows": 17}
    assert second_response == {"rows": 17}
    assert not saw_overlap
    assert maximum_active == 1
    assert len(calls) == 2


def test_cancelled_request_does_not_release_build_lock_while_worker_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment, calls = _load_deployment(monkeypatch)
    worker = sys.modules["worker"]
    active = 0
    maximum_active = 0
    state_lock = threading.Lock()
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()

    def run(*, db_path: str, llm_url: str | None) -> int:
        nonlocal active, maximum_active
        with state_lock:
            active += 1
            is_first = active == 1
            maximum_active = max(maximum_active, active)
            if is_first:
                first_started.set()
            else:
                second_started.set()
        if is_first:
            release_first.wait(timeout=2)
        with state_lock:
            active -= 1
        calls.append({"db_path": db_path, "llm_url": llm_url})
        return 17

    worker.__dict__["run"] = run

    async def cancel_then_build() -> tuple[Any, bool]:
        first = asyncio.create_task(deployment(_Request({}, "Bearer serve-secret")))
        await asyncio.to_thread(first_started.wait, 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(deployment(_Request({}, "Bearer serve-secret")))
        await asyncio.sleep(0.05)
        saw_overlap = second_started.is_set()
        release_first.set()
        return await second, saw_overlap

    try:
        response, saw_overlap = asyncio.run(cancel_then_build())
    finally:
        release_first.set()

    assert response == {"rows": 17}
    assert not saw_overlap
    assert maximum_active == 1
    assert len(calls) == 2


@pytest.mark.parametrize("llm_url", [None, "", "   "])
def test_worker_uses_the_local_llm_default_for_unset_or_blank_configuration(
    monkeypatch: pytest.MonkeyPatch,
    llm_url: str | None,
) -> None:
    if llm_url is None:
        monkeypatch.delenv("MEDIA_TOPICS_LLM_URL", raising=False)
    else:
        monkeypatch.setenv("MEDIA_TOPICS_LLM_URL", llm_url)
    worker = _load_worker(monkeypatch)

    args = worker.__dict__["_parse_args"](["--db", "configured-db"])

    assert args.llm_url == "http://127.0.0.1:8003/v1"


def test_worker_build_does_not_mutate_openai_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _load_worker(monkeypatch)

    class _ReachedDatasetBoundary(Exception):
        pass

    lance = ModuleType("lance")

    def dataset(path: str) -> Any:
        raise _ReachedDatasetBoundary

    lance.__dict__["dataset"] = dataset
    pyarrow = ModuleType("pyarrow")
    toponymy = ModuleType("toponymy")
    toponymy.__dict__["KeyphraseBuilder"] = object
    toponymy.__dict__["Toponymy"] = object
    toponymy.__dict__["ToponymyClusterer"] = object
    embedding_wrappers = ModuleType("toponymy.embedding_wrappers")
    embedding_wrappers.__dict__["OpenAIEmbedder"] = object
    llm_wrappers = ModuleType("toponymy.llm_wrappers")
    llm_wrappers.__dict__["OpenAINamer"] = object
    for name, module in {
        "lance": lance,
        "pyarrow": pyarrow,
        "toponymy": toponymy,
        "toponymy.embedding_wrappers": embedding_wrappers,
        "toponymy.llm_wrappers": llm_wrappers,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv("OPENAI_API_KEY", "existing-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://existing.internal/v1")
    with pytest.raises(_ReachedDatasetBoundary):
        worker.__dict__["_build"](SimpleNamespace(db="configured-db", llm_url="http://request-controlled/"))

    assert os.environ["OPENAI_API_KEY"] == "existing-key"
    assert os.environ["OPENAI_BASE_URL"] == "http://existing.internal/v1"
