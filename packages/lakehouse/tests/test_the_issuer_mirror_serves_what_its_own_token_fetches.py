"""The issuer mirror serves the cluster issuer's discovery and keys to a verifier that sends no credential ([[XC-077]]).

OpenFGA fetches `<issuer>/.well-known/openid-configuration` and then its `jwks_uri` with no bearer, and the k3s
issuer answers both 401 without one. RUN: the real app (`with TestClient`, so its lifespan builds the upstream
client) in front of a stub issuer that, like the API server, answers 401 unless the request carries the token
the mirror's file holds at that moment, and advertises a `jwks_uri` on another host, as k3s advertises its node
IP. A verifier must be pointed back at the mirror for the keys, and the file is rotated between the two GETs.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from lakehouse.governed.issuer_mirror import IssuerMirrorSettings, create_app
from lakehouse.process.lifecycle import mark_draining


#: The loggers the mirror's log setup moves, restored after the test that runs it.
_LOGGERS = ("", "catalog", "lineage", "maintenance", "lakehouse")
_KEYS: dict[str, Any] = {"keys": [{"kty": "RSA", "kid": "k3s-1", "alg": "RS256", "use": "sig", "n": "AQAB", "e": "AQAB"}]}


class _Issuer(BaseModel):
    """A stub issuer on a free port: its URL, the token file it admits, every (path, bearer) it was sent, the answer it
    gives a path in place of its document, and the seconds it waits before answering."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    url: str
    token: Path
    seen: list[tuple[str, str]] = Field(default_factory=list)
    answers: dict[str, tuple[int, bytes]] = Field(default_factory=dict)
    stall: list[float] = Field(default_factory=lambda: [0.0])


@pytest.fixture
def issuer(tmp_path: Path) -> Iterator[_Issuer]:
    token = tmp_path / "token"
    token.write_text("mirror-token-1\n")
    stub: list[_Issuer] = []

    class Issuer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            this = stub[0]
            bearer = self.headers.get("authorization", "")
            this.seen.append((self.path, bearer))
            time.sleep(this.stall[0])
            if bearer != f"Bearer {token.read_text().strip()}":
                status, data = 401, json.dumps({"kind": "Status", "code": 401}).encode()
            elif self.path in this.answers:
                status, data = this.answers[self.path]
            elif self.path == "/.well-known/openid-configuration":
                discovery = {"issuer": "https://kubernetes.default.svc.cluster.local", "jwks_uri": f"{this.url}/node-ip/openid/v1/jwks"}
                status, data = 200, json.dumps(discovery).encode()
            elif self.path == "/node-ip/openid/v1/jwks":
                status, data = 200, json.dumps(_KEYS).encode()
            else:
                status, data = 404, b"{}"
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 — the stdlib's own parameter name
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    stub.append(_Issuer(url=f"http://127.0.0.1:{server.server_port}", token=token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield stub[0]
    finally:
        server.shutdown()
        server.server_close()


def test_a_verifier_with_no_credential_reads_the_discovery_and_keys_through_the_mirror(issuer: _Issuer) -> None:
    settings = IssuerMirrorSettings(issuer=issuer.url, public_url="http://rask-issuer-mirror:8080", token_file=str(issuer.token))

    with TestClient(create_app(settings)) as verifier:
        discovery = verifier.get("/.well-known/openid-configuration")
        issuer.token.write_text("mirror-token-2\n")
        keys = verifier.get("/openid/v1/jwks")

    assert (discovery.status_code, keys.status_code) == (200, 200), f"{discovery.text} {keys.text}"
    assert discovery.json() == {"issuer": "https://kubernetes.default.svc.cluster.local", "jwks_uri": "http://rask-issuer-mirror:8080/openid/v1/jwks"}
    assert keys.json() == _KEYS
    keys_fetched_with = [bearer for path, bearer in issuer.seen if path == "/node-ip/openid/v1/jwks"]
    assert (issuer.seen[0][1], keys_fetched_with) == ("Bearer mirror-token-1", ["Bearer mirror-token-2"])


def test_the_mirror_is_ready_while_the_issuer_answers_it_and_never_once_it_drains(issuer: _Issuer, tmp_path: Path) -> None:
    """`/readyz` is what the chart probes: ready while the issuer serves the mirror its discovery document, out of
    rotation while it does not, naming nothing of the issuer's address or answer, and shutting down once it drains."""
    app = create_app(IssuerMirrorSettings(issuer=issuer.url, public_url="http://rask-issuer-mirror:8080", token_file=str(issuer.token)))
    refused = tmp_path / "refused"
    refused.write_text("a token the issuer does not admit\n")
    unanswered = create_app(IssuerMirrorSettings(issuer=issuer.url, public_url="http://rask-issuer-mirror:8080", token_file=str(refused)))

    not_started = TestClient(app).get("/readyz")
    with TestClient(app) as probe, TestClient(unanswered) as cut_off:
        ready = probe.get("/readyz")
        out_of_rotation = cut_off.get("/readyz")
        mark_draining(app)
        draining = probe.get("/readyz")

    assert (not_started.status_code, not_started.json()["status"]) == (503, "starting")
    assert (ready.status_code, ready.json()) == (200, {"status": "ready", "components": {"issuer": "healthy"}})
    assert (out_of_rotation.status_code, out_of_rotation.json()) == (503, {"status": "unhealthy", "components": {"issuer": "unavailable"}})
    assert (draining.status_code, draining.json()["status"]) == (503, "shutting_down")


def test_a_probe_answers_while_the_issuer_is_slow_to(issuer: _Issuer) -> None:
    """The kubelet allows the probe a second, and the issuer may take the mirror's whole timeout: the probe answers the
    verdict the watch last kept, and fetches nothing itself."""
    app = create_app(IssuerMirrorSettings(issuer=issuer.url, public_url="http://rask-issuer-mirror:8080", token_file=str(issuer.token)))

    with TestClient(app) as probe:
        issuer.stall[0] = 2.0
        started = time.monotonic()
        ready = probe.get("/readyz")
        took = time.monotonic() - started
        issuer.stall[0] = 0.0

    assert (ready.status_code, took < 1.0) == (200, True), took


@pytest.mark.parametrize(
    ("path", "answer", "door"),
    [
        pytest.param("/.well-known/openid-configuration", b"<html>a proxy's page</html>", "/.well-known/openid-configuration", id="discovery-not-json"),
        pytest.param("/.well-known/openid-configuration", b'{"issuer": "https://kubernetes.default.svc"}', "/openid/v1/jwks", id="discovery-without-jwks-uri"),
        pytest.param("/node-ip/openid/v1/jwks", b'{"kid": "k3s-1"}', "/openid/v1/jwks", id="keys-without-a-key-set"),
    ],
)
def test_an_issuer_answer_that_is_not_its_document_is_the_specs_outage(issuer: _Issuer, path: str, answer: bytes, door: str) -> None:
    """A 200 whose body is not the document (a proxy's page, a document that names no key set) is the issuer not answering:
    the spec's 503, code 17, at the door and at the probe."""
    settings = IssuerMirrorSettings(issuer=issuer.url, public_url="http://rask-issuer-mirror:8080", token_file=str(issuer.token))
    issuer.answers[path] = (200, answer)

    with TestClient(create_app(settings)) as verifier:
        served = verifier.get(door)
        probed = verifier.get("/readyz")

    assert (served.status_code, served.json()["code"], served.json()["type"]) == (503, 17, "https://lance.org/problems/issuerunanswerederror"), served.text
    assert probed.status_code == (503 if path == "/.well-known/openid-configuration" else 200)


def test_an_issuer_that_refuses_the_mirror_is_the_specs_outage_and_its_address_stays_in_the_log(
    issuer: _Issuer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The mirror answers callers with no credential, so the issuer's address and answer go to the operator's log and
    the caller reads the spec's 503, code 17."""
    url = issuer.url
    refused = tmp_path / "refused"
    refused.write_text("a token the issuer does not admit\n")
    settings = IssuerMirrorSettings(issuer=url, public_url="http://rask-issuer-mirror:8080", token_file=str(refused))
    caplog.set_level(logging.WARNING)

    with TestClient(create_app(settings)) as verifier:
        answer = verifier.get("/.well-known/openid-configuration")

    assert (answer.status_code, answer.headers["content-type"]) == (503, "application/problem+json"), answer.text
    assert (answer.json()["code"], answer.json()["type"]) == (17, "https://lance.org/problems/issuerunanswerederror")
    assert url not in answer.text
    told = [
        (record.levelname, vars(record)["url"], vars(record)["status"])
        for record in caplog.records
        if record.getMessage() == "issuer_mirror_upstream_unanswered"
    ]
    assert set(told) == {("WARNING", f"{url}/.well-known/openid-configuration", 401)}


@pytest.fixture
def unconfigured_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No handler on the root logger until the app's own setup adds one, at the default level; restored after."""
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level) for name in _LOGGERS}
    monkeypatch.delenv("RASK_LOG_LEVEL", raising=False)
    logging.getLogger().handlers = []
    yield
    for name, (handlers, level) in saved.items():
        logging.getLogger(name).handlers, logging.getLogger(name).level = handlers, level


def test_a_mirror_whose_own_token_cannot_be_read_says_so_and_names_no_file(
    issuer: _Issuer, tmp_path: Path, capsys: pytest.CaptureFixture[str], unconfigured_logging: None
) -> None:
    """The door answers a caller with no credential, so what it says of the pod's own files stays in the log, which the
    mirror writes to stdout in the format every lakehouse process writes."""
    settings = IssuerMirrorSettings(issuer=issuer.url, public_url="http://rask-issuer-mirror:8080", token_file=str(tmp_path / "absent"))

    with TestClient(create_app(settings)) as verifier:
        answer = verifier.get("/.well-known/openid-configuration")

    own = [" ".join(line.split()[2:]) for line in capsys.readouterr().out.splitlines() if re.match(r"\S+ \S+ \S+ +lakehouse\.governed\.issuer_mirror ", line)]
    assert (answer.status_code, answer.json()["code"], issuer.seen) == (503, 17, [])
    assert str(tmp_path) not in answer.text
    assert {line for line in own if "issuer_mirror_token_unreadable" in line} == {"ERROR lakehouse.governed.issuer_mirror [-] — issuer_mirror_token_unreadable"}
