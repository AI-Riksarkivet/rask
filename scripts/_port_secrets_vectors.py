"""The secret-store vectors of ``gen_port_vectors.py``: what the real session with the store does through scripted answers.

The real ``lakehouse.governed.secrets.SecretStore`` is driven with an injected clock and an ``httpx.MockTransport`` that
answers each request from a script, and records at every step what was asked (method, path, the token header, the login's
body) and what the session did: logged in, sat out, read, refused. The real ``lakehouse.lineage.keys.PublishedKeys`` is
driven the same way through a scripted fetch. The pure decisions (the settings, the lease, a login's and a read's
answer, the secret-name rule, the backoff) are recorded on their own. Every token is a fixture string, and the
jitter of the backoff is fixed, so the file is the same on every run.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch


#: The projected tokens and store tokens the scripts use: fixture strings, protecting nothing.
_JWT = "fixture-projected-1"
_JWT_ROTATED = "fixture-projected-2"
_SETTINGS = {"RASK_SECRET_STORE_ADDR": "http://store.test:8200/", "RASK_SECRET_STORE_ROLE": "lineage"}
#: The events the session's loop says once per outage, the ones a scripted step records.
_SAID_ONCE = {
    "secret_store_token_not_checked",
    "secret_store_token_cannot_ask_about_itself",
    "secret_store_token_dropped",
    "secret_store_login_not_renewed",
    "secret_store_login_lapsed",
}


def _login(token: str, lease: Any = 900) -> dict[str, Any]:  # noqa: ANN401 - a lease is any JSON value an answer may hold
    return {"status": 200, "body": {"auth": {"client_token": token, "lease_duration": lease, "policies": ["default"]}}}


def _kv(fields: Any) -> dict[str, Any]:  # noqa: ANN401 - the fields are any JSON value an answer may hold
    return {"status": 200, "body": {"data": {"data": fields, "metadata": {"version": 1}}}}


_OK = {"status": 200, "body": {"data": {"ttl": 600}}}
_FORBIDDEN = {"status": 403, "body": {"errors": ["permission denied"]}}
_DOWN = {"down": True}


def _scenarios() -> list[dict[str, Any]]:
    """Each scenario: the answers in the order the store gives them, and the steps the session is put through."""

    def step(op: str, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the step's own arguments
        return {"op": op, **args}

    read = step
    return [
        {
            "name": "a first login, then a read under it",
            "answers": [_login("s.one"), _OK, _kv({"seed": "x", "keys": "a,b"})],
            "steps": [step("log_in", retries=1), read("read", name="signing-public-catalog", retries=1)],
        },
        {
            "name": "a read logs in itself where no loop passes",
            "answers": [_login("s.one"), _kv({"k": "v"}), _kv({"k": "w"})],
            "steps": [read("read", name="a", retries=1), read("read", name="a", retries=1)],
        },
        {
            "name": "the loop renews at half the lease",
            "answers": [_login("s.one", 600), _OK, _OK, _login("s.two", 600), _kv({"k": "v"})],
            "steps": [
                step("log_in", retries=1),
                step("advance", seconds=299),
                step("pass"),
                step("advance", seconds=1),
                step("pass"),
                read("read", name="a", retries=1),
            ],
        },
        {
            "name": "a lease shorter than a minute is no login",
            "answers": [_login("s.one", 59), _login("s.two", 3601), _login("s.three", 0), _login("s.four", 60), _OK],
            "steps": [step("log_in", retries=4)],
        },
        {
            "name": "a token an HTTP header cannot carry is no login",
            "answers": [_login("s.has space"), _login("s.ünicode"), _login(""), _login("s.ok"), _OK],
            "steps": [step("log_in", retries=4)],
        },
        {
            "name": "a login refused the question about itself is dropped and made again",
            "answers": [_login("s.one"), _FORBIDDEN, _login("s.two"), _OK],
            "steps": [step("log_in", retries=2)],
        },
        {
            "name": "a login refused at every attempt fails the start",
            "answers": [_login("s.one"), _FORBIDDEN, {"status": 400, "body": {"errors": ["invalid role"]}}],
            "steps": [step("log_in", retries=2), step("answers")],
        },
        {
            "name": "the loop logs in again on a dropped token",
            "answers": [_login("s.one"), _OK, _FORBIDDEN, _login("s.two"), _OK, _OK],
            "steps": [step("log_in", retries=1), step("advance", seconds=15), step("pass"), step("advance", seconds=15), step("pass"), step("answers")],
        },
        {
            "name": "a store that refuses every new token the question: the backoff row",
            "answers": [_login("s.0"), _OK]
            + [_FORBIDDEN, _login("s.1")]
            + [_FORBIDDEN, _login("s.2")]
            + [_FORBIDDEN]
            + [_FORBIDDEN, _login("s.3")]
            + [_FORBIDDEN] * 3
            + [_FORBIDDEN, _login("s.4")]
            + [_FORBIDDEN] * 7
            + [_FORBIDDEN, _login("s.5")]
            + [_FORBIDDEN] * 15
            + [_FORBIDDEN, _login("s.6")]
            + [_FORBIDDEN] * 15
            + [_FORBIDDEN, _login("s.7")]
            + [_OK, _OK],
            "steps": [step("log_in", retries=1)] + [step("pass") for _ in range(1 + 1 + 1 + 1 + 3 + 1 + 7 + 1 + 15 + 1 + 15 + 1 + 2)],
        },
        {
            "name": "a login the lease asks for inside a row counts in it",
            "answers": [_login("s.0", 120), _OK, _FORBIDDEN, _login("s.1", 120), _FORBIDDEN, _login("s.2", 120), _login("s.3", 120), _OK],
            "steps": [step("log_in", retries=1), step("pass"), step("pass"), step("advance", seconds=60), step("pass"), step("pass")],
        },
        {
            "name": "an outage with the login still usable, then lapsed",
            "answers": [_login("s.one", 600), _OK, _DOWN, _DOWN, _DOWN, _DOWN, _DOWN, _DOWN, _DOWN, _login("s.two", 600), _OK],
            "steps": [
                step("log_in", retries=1),
                step("pass"),
                step("pass"),
                step("advance", seconds=300),
                step("pass"),
                step("pass"),
                read("read", name="a", retries=1),
                step("advance", seconds=240),
                step("pass"),
                read("read", name="a", retries=1),
                step("answers"),
                step("pass"),
                step("pass"),
                step("pass"),
            ],
        },
        {
            "name": "a read waits for the loop that passes",
            "answers": [_login("s.one", 60), _OK, _DOWN, _login("s.two", 60), _kv({"k": "v"})],
            "steps": [
                step("log_in", retries=1),
                step("advance", seconds=30),
                step("pass"),
                step("advance", seconds=25),
                read("read", name="a", retries=1),
                step("advance", seconds=60),
                read("read", name="a", retries=1),
                step("advance", seconds=1),
                read("read", name="a", retries=1),
            ],
        },
        {
            "name": "the projected token is read at every login",
            "answers": [_login("s.one", 60), _kv({"k": "v"}), _login("s.two", 60), _kv({"k": "w"})],
            "steps": [
                read("read", name="a", retries=1),
                step("rotate", token=_JWT_ROTATED),
                step("advance", seconds=55),
                read("read", name="a", retries=1),
            ],
        },
        {
            "name": "an unreadable projected token is no login",
            "answers": [],
            "steps": [step("remove_token"), step("log_in", retries=1), read("read", name="a", retries=1)],
        },
        {
            "name": "reads the store does not serve",
            "answers": [
                _login("s.one"),
                {"status": 404, "body": {"errors": []}},
                {"status": 403, "body": {"errors": ["permission denied"]}},
                _kv({"n": 1}),
                _kv({"n": None}),
                _kv({"n": {"x": "y"}}),
                _kv([]),
                {"status": 200, "body": {"data": None}},
                {"status": 200, "text": "not json"},
                {"status": 500, "body": {}},
                {"status": 301, "body": {}, "location": "http://elsewhere.test/"},
                _kv({}),
                _kv({"seed": "", "keys": "a"}),
            ],
            "steps": [read("read", name="a", retries=1) for _ in range(12)],
        },
        {
            "name": "a read is asked again within its budget",
            "answers": [_login("s.one"), {"status": 404, "body": {}}, _DOWN, _kv({"k": "v"})],
            "steps": [read("read", name="a", retries=3)],
        },
        {
            "name": "names that are no secret name ask nothing",
            "answers": [],
            "steps": [read("read", name=name, retries=1) for name in ["", "A", "-a", "a/b", "a..b", "a_b", "a b", "../a", "é", "a" * 254, "a\n", "a.b"]],
        },
        {
            "name": "a required secret",
            "answers": [_login("s.one"), _kv({"seed": "S", "other": "o"}), _kv({"other": "o"}), _kv({"seed": ""}), _DOWN],
            "steps": [
                read("read_required", name="signing-key-catalog", require="seed", retries=1),
                read("read_required", name="signing-key-catalog", require="seed", retries=1),
                read("read_required", name="signing-key-catalog", require="seed", retries=1),
                read("read_required", name="signing-key-catalog", require="seed", retries=1),
                read("read_required", name="Bad", require="seed", retries=1),
            ],
        },
        {
            "name": "the readiness report",
            "answers": [_login("s.one"), _OK, _OK, _FORBIDDEN, {"status": 500, "body": {}}, _DOWN],
            "steps": [step("log_in", retries=1), step("answers"), step("answers"), step("answers"), step("answers")],
        },
        {
            "name": "the readiness report with no login",
            "answers": [{"status": 400, "body": {}}, _DOWN, _login("s.one"), _OK],
            "steps": [step("answers"), step("answers"), step("answers")],
        },
        {
            "name": "mounts and an address with a path",
            "settings": {
                "RASK_SECRET_STORE_ADDR": "http://store.test:8200/prefix/",
                "RASK_SECRET_STORE_AUTH_MOUNT": "k8s-east",
                "RASK_SECRET_STORE_KV_MOUNT": "kv",
            },
            "answers": [_login("s.one"), _kv({"k": "v"})],
            "steps": [read("read", name="a", retries=1)],
        },
    ]


def _settings_cases() -> list[dict[str, str]]:
    full = {**_SETTINGS, "RASK_SECRET_STORE_TOKEN_FILE": "/var/run/secrets/store/token"}
    return [
        {},
        {"RASK_SECRET_STORE_ROLE": "r"},
        full,
        {**full, "RASK_SECRET_STORE_AUTH_MOUNT": "k8s", "RASK_SECRET_STORE_KV_MOUNT": "kv", "RASK_SECRET_STORE_CA_FILE": "/etc/store/ca.crt"},
        {"RASK_SECRET_STORE_ADDR": "http://s"},
        {"RASK_SECRET_STORE_ADDR": "http://s", "RASK_SECRET_STORE_ROLE": "r"},
        {"RASK_SECRET_STORE_ADDR": "http://s", "RASK_SECRET_STORE_TOKEN_FILE": "/t"},
        {"RASK_SECRET_STORE_ADDR": "http://s", "RASK_SECRET_STORE_ROLE": "", "RASK_SECRET_STORE_TOKEN_FILE": "/t"},
        {"RASK_SECRET_STORE_ADDR": "http://s", "RASK_SECRET_STORE_ROLE": "r", "RASK_SECRET_STORE_TOKEN_FILE": ""},
        {"RASK_SECRET_STORE_ADDR": "", "RASK_SECRET_STORE_ROLE": "r"},
        {"rask_secret_store_addr": "http://s", "rask_secret_store_role": "r", "Rask_Secret_Store_Token_File": "/t"},
        {"rask_secret_store_addr": "http://s"},
        {"RASK_SECRET_STORE_ADDR": "http://s", "RASK_SECRET_STORE_ROLE": "r", "RASK_SECRET_STORE_TOKEN_FILE": "/t", "RASK_SECRET_STORE_AUTH_MOUNT": ""},
    ]


#: A member an answer leaves out.
_ABSENT: Any = object()


def _auth(token: Any, lease: Any, **extra: Any) -> str:  # noqa: ANN401 - any JSON value an answer may hold
    """A login answer's text: the token and the lease, either left out where `_ABSENT`, beside ``extra``."""
    members = {name: value for name, value in (("client_token", token), ("lease_duration", lease)) if value is not _ABSENT}
    return json.dumps({"auth": members | extra})


def _login_answers() -> list[str]:
    return [
        _auth("s.ok", 900),
        _auth("s.ok", 60),
        _auth("s.ok", 59),
        _auth("s.ok", 3600),
        _auth("s.ok", 3601),
        _auth("s.ok", 0),
        _auth("s.ok", -900),
        _auth("s.ok", 900.0),
        _auth("s.ok", 900.5),
        _auth("s.ok", True),
        _auth("s.ok", None),
        _auth("s.ok", _ABSENT),
        _auth(_ABSENT, 900),
        _auth(None, 900),
        _auth(7, 900),
        _auth("", 900),
        _auth("s.has space", 900),
        _auth("s.tab\t", 900),
        _auth("s.ünicode", 900),
        _auth("s.~!#", 900),
        _auth("s.ok", 900, renewable=True, extra={"a": 1}),
        json.dumps({"auth": None}),
        json.dumps({"data": {}}),
        json.dumps([]),
        "not json",
        "",
        '{"auth": {"client_token": "s.first", "client_token": "s.last", "lease_duration": 900}}',
        '{"auth": {"client_token": "s.ok", "lease_duration": 1e3}}',
        '{"auth": {"client_token": "s.ok", "lease_duration": 9000000000000000000000}}',
    ]


def _read_answers() -> list[str]:
    return [
        json.dumps({"data": {"data": {"a": "1", "b": ""}, "metadata": {}}}),
        json.dumps({"data": {"data": {}}}),
        json.dumps({"data": {"data": {"a": 1}}}),
        json.dumps({"data": {"data": {"a": None}}}),
        json.dumps({"data": {"data": {"a": True}}}),
        json.dumps({"data": {"data": {"a": ["x"]}}}),
        json.dumps({"data": {"data": {"a": {"x": "y"}}}}),
        json.dumps({"data": {"data": None}}),
        json.dumps({"data": {"data": []}}),
        json.dumps({"data": {}}),
        json.dumps({"data": None}),
        json.dumps({}),
        json.dumps([]),
        "not json",
        '{"data": {"data": {"a": "first", "a": "last"}}}',
        json.dumps({"data": {"data": {"ünicode": "välue\u0000"}}}),
    ]


def _identities() -> list[str]:
    return [
        "kubernetes~system:serviceaccount:rask:catalog",
        "kubernetes~system:serviceaccount:rask:svc-ingest",
        "kubernetes~catalog",
        "kubernetes~",
        "dex~ada",
        "catalog",
        "Kubernetes~system:serviceaccount:rask:catalog",
        "kubernetes~a\x07b",
        "kubernetes~system:serviceaccount:rask:",
    ]


def _keys_scenarios() -> list[dict[str, Any]]:
    """Each scenario: what the fetch answers in turn, and the steps one or more verifications take."""
    one = {"keys": "UKEY1"}
    two = {"keys": "UKEY2, UKEY1"}
    return [
        {
            "name": "a cache hit inside the TTL, a read past it",
            "fetches": [one, two],
            "steps": [
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "advance", "seconds": 59},
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "advance", "seconds": 1},
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
            ],
        },
        {
            "name": "an unknown key id inside and outside the pacing interval",
            "fetches": [one, one, two],
            "steps": [
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "advance", "seconds": 10},
                {"op": "event", "kid": "k2"},
                {"op": "published", "identity": "svc"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 20},
                {"op": "event", "kid": "k3"},
                {"op": "refresh", "identity": "svc"},
                {"op": "event", "kid": "k3"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 29},
                {"op": "event", "kid": "k4"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 1},
                {"op": "refresh", "identity": "svc"},
            ],
        },
        {
            "name": "a redelivery is judged on a read begun since its key id was first met",
            "fetches": [one, two],
            "steps": [
                {"op": "event", "kid": "k9"},
                {"op": "published", "identity": "svc"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 5},
                {"op": "event", "kid": "k9"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 30},
                {"op": "event", "kid": "k8"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 1},
                {"op": "event", "kid": "k9"},
                {"op": "refresh", "identity": "svc"},
            ],
        },
        {
            "name": "a failed read answers the verification that waited on it, and the next reads afresh",
            "fetches": [{}, {"keys": " , "}, one, {}, two],
            "steps": [
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "published", "identity": "svc"},
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "advance", "seconds": 1},
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "advance", "seconds": 1},
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "svc"},
                {"op": "advance", "seconds": 31},
                {"op": "event", "kid": "k2"},
                {"op": "refresh", "identity": "svc"},
                {"op": "refresh", "identity": "svc"},
                {"op": "event", "kid": "k2"},
                {"op": "refresh", "identity": "svc"},
                {"op": "advance", "seconds": 1},
                {"op": "event", "kid": "k2"},
                {"op": "refresh", "identity": "svc"},
                {"op": "published", "identity": "svc"},
            ],
        },
        {
            "name": "each identity is cached and paced on its own",
            "fetches": [one, two, one],
            "steps": [
                {"op": "event", "kid": "k1"},
                {"op": "published", "identity": "a"},
                {"op": "published", "identity": "b"},
                {"op": "refresh", "identity": "a"},
                {"op": "event", "kid": None},
                {"op": "refresh", "identity": "b"},
                {"op": "published", "identity": "a"},
            ],
        },
    ]


class _Answers:
    """The scripted store: each request takes the next answer, and one past the script is a store that is down."""

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self._answers = list(answers)
        self.asked: list[dict[str, Any]] = []

    def __call__(self, request: Any) -> Any:  # noqa: ANN401 - httpx's request and response
        import httpx

        body = json.loads(request.content) if request.content else None
        self.asked.append({"method": request.method, "path": request.url.raw_path.decode(), "token": request.headers.get("x-vault-token"), "body": body})
        answer: dict[str, Any] = self._answers.pop(0) if self._answers else _DOWN
        if answer.get("down"):
            raise httpx.ConnectError("the store is down", request=request)
        headers = {"location": answer["location"]} if "location" in answer else {}
        content = answer["text"].encode() if "text" in answer else json.dumps(answer["body"]).encode()
        return httpx.Response(answer["status"], content=content, headers=headers)


@contextmanager
def _no_jitter() -> Generator[None]:
    """The backoff's jitter fixed at nothing, so a scripted retry waits the same on every run."""
    with patch("tenacity.wait.random.uniform", lambda low, high: low):
        yield


class _Said(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.msg in _SAID_ONCE:
            self.events.append(str(record.msg))


def _store_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from lakehouse.governed import secrets

    answers = _Answers(scenario["answers"])
    said = _Said()
    log = logging.getLogger(secrets.__name__)
    log.addHandler(said)
    log.setLevel(logging.DEBUG)
    now = [1000.0]
    with TemporaryDirectory() as directory:
        token = Path(directory) / "token"
        token.write_text(f"{_JWT}\n")
        environment = {**_SETTINGS, "RASK_SECRET_STORE_TOKEN_FILE": str(token), **scenario.get("settings", {})}
        with patch.dict(os.environ, environment, clear=True):
            store = secrets.SecretStore(secrets.SecretStoreSettings(), clock=lambda: now[0])
        store._client = httpx.Client(base_url=store._client.base_url, transport=httpx.MockTransport(answers), trust_env=False)
        steps = []
        try:
            for step in scenario["steps"]:
                asked_before, said_before = len(answers.asked), len(said.events)
                outcome = _run_step(store, step, now, token)
                steps.append(step | {"outcome": outcome, "asked": answers.asked[asked_before:], "said": said.events[said_before:]})
        finally:
            log.removeHandler(said)
            store.close()
    return {"name": scenario["name"], "settings": scenario.get("settings", {}), "answers": scenario["answers"], "steps": steps}


def _run_step(store: Any, step: dict[str, Any], now: list[float], token: Path) -> Any:  # noqa: ANN401 - the session and the step's outcome
    from lakehouse.governed import secrets

    op = step["op"]
    if op == "advance":
        now[0] += step["seconds"]
        return None
    if op == "rotate":
        token.write_text(step["token"])
        return None
    if op == "remove_token":
        token.unlink()
        return None
    if op == "log_in":
        try:
            store.log_in(timeout=5.0, retries=step["retries"], backoff=0.0)
        except RuntimeError:
            return "refused"
        return "logged_in"
    if op == "pass":
        return "logged_in" if store.keep_logged_in() else "held"
    if op == "answers":
        try:
            store.answers()
        except ConnectionError as refused:
            return {"refused": _refusal_kind(refused)}
        return "answers"
    try:
        if op == "read":
            return {"fields": store.read(step["name"], timeout=5.0, retries=step["retries"], backoff=0.0)}
        secrets._PROCESS._store = store
        try:
            return {"fields": secrets.read_required_secret(step["name"], require=step["require"], timeout=5.0, retries=step["retries"], backoff=0.0)}
        except RuntimeError:
            return {"refused": "not_served"}
        finally:
            secrets._PROCESS._store = None
    except ValueError:
        return {"refused": "no_secret_name"}


def _refusal_kind(refused: ConnectionError) -> str:
    """Which of the readiness report's answers ``refused`` is: the store's status, no answer, no usable login, or a dropped token."""
    import httpx

    cause = refused.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return f"status {cause.response.status_code}"
    if isinstance(cause, httpx.HTTPError):
        return "no answer"
    return "no usable login" if cause is not None else "token refused"


def _keys_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    from lakehouse.lineage.keys import PublishedKeys
    from lakehouse.lineage.signing import KeySourceUnavailableError

    fetches = list(scenario["fetches"])
    fetched: list[str] = []
    now = [1000.0]

    def fetch(identity: str) -> dict[str, str]:
        fetched.append(identity)
        return fetches.pop(0) if fetches else {}

    keys = PublishedKeys(fetch, clock=lambda: now[0])
    source = None
    steps = []
    for step in scenario["steps"]:
        before = len(fetched)
        outcome: Any = None
        if step["op"] == "advance":
            now[0] += step["seconds"]
        elif step["op"] == "event":
            source = keys.for_event(step["kid"])
        elif source is not None:
            read: Callable[[str], Any] = source.published if step["op"] == "published" else source.refresh
            try:
                outcome = {"keys": list(read(step["identity"]))}
            except KeySourceUnavailableError:
                outcome = "unavailable"
        steps.append(step | {"outcome": outcome, "fetched": fetched[before:]})
    return {"name": scenario["name"], "fetches": scenario["fetches"], "steps": steps}


def secrets_vectors() -> dict[str, list[Any]]:
    """The settings, the pure decisions, and every scripted scenario of the session and of the published keys."""
    import tenacity
    from pydantic import ValidationError

    from lakehouse.governed import secrets
    from lakehouse.governed.signing_key import signing_public_secret

    settings = []
    for environment in _settings_cases():
        with patch.dict(os.environ, environment, clear=True):
            try:
                taken = secrets.SecretStoreSettings()
            except ValidationError:
                settings.append({"env": environment, "taken": False})
                continue
        paths = {
            name: (None if path is None else {"normal": str(path)} if str(path) == environment_value else {"normal": None})
            for name, path, environment_value in (
                ("token_file", taken.token_file, _value(environment, "RASK_SECRET_STORE_TOKEN_FILE")),
                ("ca_file", taken.ca_file, _value(environment, "RASK_SECRET_STORE_CA_FILE")),
            )
        }
        fields = {"address": taken.address, "role": taken.role, "auth_mount": taken.auth_mount, "kv_mount": taken.kv_mount}
        settings.append({"env": environment, "taken": True, "fields": fields, "paths": paths, "configured": bool(taken.address)})

    logins = []
    for text in _login_answers():
        try:
            issued = secrets._LoginAnswer.model_validate_json(text).auth
        except ValidationError:
            logins.append({"answer": text, "taken": False})
        else:
            logins.append({"answer": text, "taken": True, "token": issued.client_token.get_secret_value(), "lease": issued.lease_duration})

    reads = []
    for text in _read_answers():
        try:
            reads.append({"answer": text, "fields": secrets._ReadAnswer.model_validate_json(text).data.data})
        except ValidationError:
            reads.append({"answer": text, "fields": None})

    leases = []
    for lease in [60, 61, 90, 333, 599, 600, 900, 901, 1799, 3599, 3600]:
        login = secrets._Login.model_validate(
            {"token": "fixture", "renew_after": 1000.0 + lease * secrets._RENEWED_AT, "expires": 1000.0 + lease * secrets._LEASE_USED}
        )
        leases.append({"asked": 1000.0, "lease": lease, "renew_after": login.renew_after, "expires": login.expires})

    names = ["a", "a-b", "0", "signing-public-catalog", "", "-", "-a", "A", "a_b", "a.b", "a/b", "a b", "a\n", "é", "a" * 253, "a" * 254, "9-"]
    texts = ["s.ok", "", " ", "a b", "~", "!", "\x7f", "\x20x", "é", "s.tab\t", "x" * 4096]
    backoff = []
    for initial in [3.0, 0.5, 0.0]:
        for draw in [0.0, 0.25, 0.999]:
            for attempt in range(1, 9):
                wait = tenacity.wait_exponential_jitter(initial=initial, max=secrets._LONGEST_BACKOFF_SECONDS)
                state = tenacity.RetryCallState(retry_object=tenacity.Retrying(), fn=None, args=(), kwargs={})
                state.attempt_number = attempt
                with patch("tenacity.wait.random.uniform", lambda low, high, draw=draw: low + (high - low) * draw):
                    backoff.append({"initial": initial, "draw": draw, "attempt": attempt, "wait": wait(state)})

    with _no_jitter():
        sessions = [_store_scenario(scenario) for scenario in _scenarios()]
    return {
        "settings": settings,
        "logins": logins,
        "reads": reads,
        "leases": leases,
        "secret_names": [{"name": name, "taken": secrets._SECRET_NAME.fullmatch(name) is not None} for name in names],
        "fits_a_header": [{"text": text, "fits": secrets.fits_a_header(text)} for text in texts],
        "backoff": backoff,
        "public_secrets": [{"identity": identity, "secret": signing_public_secret(identity)} for identity in _identities()],
        "sessions": sessions,
        "published_keys": [_keys_scenario(scenario) for scenario in _keys_scenarios()],
    }


def _value(environment: dict[str, str], name: str) -> str | None:
    """The value ``name`` has in ``environment``, read without regard to case as the settings read it."""
    return next((value for key, value in environment.items() if key.upper() == name), None)
