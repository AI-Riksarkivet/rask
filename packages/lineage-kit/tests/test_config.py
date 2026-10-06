"""Env-driven transport config (RASK_* first, official OpenLineage names as aliases), and the credential
the HTTP transport presents at the lineage door."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, override

import pytest

from lineage_kit import ClientEmitter, Job, LineageSettings, NoopEmitter, Run, RunEvent, RunState, build_emitter


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def test_rask_env_vars_configure_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    monkeypatch.setenv("RASK_LINEAGE_API_KEY", "sekrit")
    monkeypatch.setenv("RASK_LINEAGE_NAMESPACE", "htr")
    monkeypatch.setenv("RASK_LINEAGE_TIMEOUT", "2.5")
    s = LineageSettings()
    assert s.endpoint == "http://marquez:5000"
    assert s.api_key == "sekrit"
    assert s.namespace == "htr"
    assert s.timeout == 2.5


def test_official_openlineage_names_are_accepted_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
    monkeypatch.setenv("OPENLINEAGE_NAMESPACE", "htr")
    s = LineageSettings()
    assert s.endpoint == "http://marquez:5000"
    assert s.namespace == "htr"


def test_rask_name_wins_over_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://rask-wins:5000")
    monkeypatch.setenv("OPENLINEAGE_URL", "http://alias-loses:5000")
    assert LineageSettings().endpoint == "http://rask-wins:5000"


@pytest.fixture
def lineage_door() -> Iterator[tuple[str, list[dict[str, str]]]]:
    """A local HTTP endpoint standing in for the lineage door: it records each POST's headers and answers 201."""
    received: list[dict[str, str]] = []

    class _Door(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            received.append({key.lower(): value for key, value in self.headers.items()})
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Door)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _event() -> RunEvent:
    return RunEvent(
        eventType=RunState.START,
        eventTime="2026-10-02T00:00:00Z",
        run=Run(runId="6f2b1a5e-1f3d-5a0e-9c4b-2f9f0a7d1c33"),
        job=Job(namespace="ray-jobs", name="train.demo"),
    )


def test_each_emit_presents_the_identity_token_the_file_holds_at_that_moment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lineage_door: tuple[str, list[dict[str, str]]]
) -> None:
    """The kubelet rewrites the projected token at about 515 s of its 600 s life, so an emitter built once
    (the process-wide default) must present whatever the file holds when each event is sent.

    The endpoint is named by ``RASK_LINEAGE_ENDPOINT``, as the Ray head renders it for the training job, the
    long-lived emitter this matters for.
    """
    url, received = lineage_door
    token_file = tmp_path / "token"
    token_file.write_text("first.projected.jwt\n")
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", url)
    monkeypatch.setenv("RASK_LINEAGE_IDENTITY_TOKEN_FILE", str(token_file))
    emitter = build_emitter()

    delivered = [emitter.emit(_event())]
    token_file.write_text("rotated.projected.jwt\n")
    delivered.append(emitter.emit(_event()))

    assert delivered == [True, True]
    assert [headers.get("authorization") for headers in received] == ["Bearer first.projected.jwt", "Bearer rotated.projected.jwt"]


@pytest.mark.parametrize("content", [pytest.param(None, id="absent"), pytest.param("\n", id="empty")])
def test_an_unreadable_identity_token_leaves_the_event_unsent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lineage_door: tuple[str, list[dict[str, str]]], content: str | None
) -> None:
    """A credential that cannot be read is not sent as an anonymous request: the event stays unsent and
    ``emit`` answers False, so the caller knows it needs recovery."""
    url, received = lineage_door
    token_file = tmp_path / "token"
    if content is not None:
        token_file.write_text(content)
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", url)
    monkeypatch.setenv("RASK_LINEAGE_IDENTITY_TOKEN_FILE", str(token_file))

    delivered = build_emitter().emit(_event())

    assert (delivered, received) == (False, [])


@pytest.mark.parametrize(
    ("env", "authorization"),
    [
        pytest.param({"RASK_LINEAGE_IDENTITY_TOKEN_FILE": ""}, None, id="blank-file-sends-no-credential"),
        pytest.param({"OPENLINEAGE_API_KEY": "static.bearer"}, "Bearer static.bearer", id="static-bearer-wins-over-the-file"),
    ],
)
def test_the_door_receives_only_the_configured_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    lineage_door: tuple[str, list[dict[str, str]]],
    env: dict[str, str],
    authorization: str | None,
) -> None:
    url, received = lineage_door
    token_file = tmp_path / "token"
    token_file.write_text("projected.jwt\n")
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", url)
    monkeypatch.setenv("RASK_LINEAGE_IDENTITY_TOKEN_FILE", str(token_file))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    delivered = build_emitter().emit(_event())

    assert delivered is True
    assert [headers.get("authorization") for headers in received] == [authorization]


def test_forced_noop_overrides_a_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "noop")
    assert isinstance(build_emitter(), NoopEmitter)


def test_console_transport_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "console")
    assert isinstance(build_emitter(), ClientEmitter)


def test_http_forced_without_endpoint_degrades_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "http")
    assert isinstance(build_emitter(), NoopEmitter)
