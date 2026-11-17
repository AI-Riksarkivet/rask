"""The outbox vectors of ``gen_port_vectors.py``: what the real Python outbox and store session do, for the Rust to do alike.

- ``object_keys``: the bucket and key ``lakehouse.outbox.stage_event`` puts and ``drop_event`` deletes for an outbox URI
  and an envelope id, through a client that records its calls.
- ``urls``: the request URL the real boto3 S3 client makes for a bucket and key.
- ``listings``: what ``list_events`` yields, oldest first and bounded, and what ``backlog`` measures, over a listing a
  stand-in S3 client gives, at the store's own precision.
- ``publishes``: ``publish_with_outbox`` with its stage, publish and drop each succeeding or failing: the effects in
  order, what it raised and whether the event stayed staged, and what it counted.
- ``errors``: how ``lakehouse.storage.s3_errors`` and ``lakehouse.records.create_json`` classify a store's refusal.
- ``signatures``: the SigV4 ``Authorization`` botocore's ``S3SigV4Auth`` gives a request, at a fixed time.
- ``settings``: the store identity ``LineageSettings`` reads from an environment, or that it refuses it.
- ``exchanges``: the STS ``AssumeRoleWithWebIdentity`` request ``lakehouse.store_session.exchange`` sends through the
  real boto3 STS client, and what it makes of the store's answer.
- ``sessions``: the real ``StoreSession`` on an injected clock, driven step by step against scripted STS answers: what
  it asked, what it holds, and what it said.

Every credential is a fixture string and the clock is fixed, so the file is the same on every run.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import tempfile
from collections.abc import Callable, Generator, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch


#: The moment every signature is made at.
_SIGNED_AT = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)
_STS = "http://sts.test:9000"
#: What a stand-in client is built from: nothing, since the stand-in reaches no store and no bus.
_NO_OPTIONS: Any = None
_NO_BUS: Any = None


def _object_keys() -> list[dict[str, Any]]:
    from lakehouse import outbox

    cases: list[dict[str, Any]] = []
    for uri, key in itertools.product(
        ("s3://bucket/_lineage_outbox", "s3://bucket/_lineage_outbox/", "s3://bucket", "s3://bucket/", "s3://bucket/a/b//", "s3://bucket//x"),
        ("r1@COMPLETE", "0190a1b2-0000-7000-8000-00000000000a"),
    ):
        client = _Recording()
        with patch.object(outbox, "object_client", lambda _options, client=client: client):
            outbox.stage_event(uri, _NO_OPTIONS, key, "{}")
            outbox.drop_event(uri, _NO_OPTIONS, key)
        cases.append({"uri": uri, "key": key, "calls": client.calls})
    return cases


class _Recording:
    """An S3 client that records the puts and deletes it is asked for."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def put_object(self, **kwargs: Any) -> None:  # noqa: ANN401 - boto3's own keyword arguments
        self.calls.append(["put", kwargs["Bucket"], kwargs["Key"], kwargs["Body"].decode()])

    def delete_object(self, **kwargs: Any) -> None:  # noqa: ANN401 - boto3's own keyword arguments
        self.calls.append(["delete", kwargs["Bucket"], kwargs["Key"]])


@contextmanager
def _signed_at_a_fixed_time() -> Generator[None]:
    with patch("botocore.auth.get_current_datetime", lambda: _SIGNED_AT):
        yield


def _urls() -> list[dict[str, Any]]:
    from botocore.awsrequest import AWSResponse
    from botocore.compat import HTTPHeaders

    from lakehouse.storage import s3_client

    seen: list[str] = []

    class Empty:
        def stream(self, **_: object) -> Iterator[bytes]:
            return iter([b""])

    def answer(request: Any, **_: object) -> AWSResponse:  # noqa: ANN401 - botocore's request
        seen.append(request.url)
        return AWSResponse(request.url, 204, HTTPHeaders(), Empty())

    client = s3_client("http://store.test:9000", access_key="AK", secret_key="SK", region="us-east-1")  # noqa: S106 - a fixture pair the stand-in never checks
    client.meta.events.register("before-send", answer)
    keys = ["_lineage_outbox/r1@COMPLETE.json", "p/a b+c=d&e?f#g%h.json", "p/ünïcode/ø.json", "p/~tilde/.json", "k", "p/(x)!*'.json"]
    for key in keys:
        client.delete_object(Bucket="bucket", Key=key)
    return [{"bucket": "bucket", "key": key, "url": url} for key, url in zip(keys, seen, strict=True)]


def _listings() -> list[dict[str, Any]]:
    from lakehouse import outbox

    base = "bucket/_lineage_outbox"
    listings: list[tuple[str, list[tuple[str, int, bool]], int | None]] = [
        ("an empty outbox", [], None),
        ("oldest first", [("a.json", 3_000_000_000, False), ("b.json", 1_000_000_000, False), ("c.json", 2_000_000_000, False)], None),
        ("ties keep the listing's order", [("a.json", 5_000_000_000, False), ("b.json", 5_000_000_000, False), ("c.json", 4_000_000_000, False)], None),
        ("bounded by the limit", [(f"e{n}.json", (10 - n) * 1_000_000_000, False) for n in range(6)], 4),
        ("a limit of zero takes none", [("a.json", 1_000_000, False)], 0),
        (
            "only staged objects are listed",
            [(name, n * 1_000_000, n == 3) for n, name in enumerate(("a.txt", "b.JSON", "c.json", "d.json.tmp", ".json", "e.json.json"), start=1)],
            None,
        ),
        ("keys a run names", [("r1@COMPLETE.json", 7_001_000_000, False), ("r1@FAIL.json", 7_000_000_000, False)], None),
        (
            "within one second, by the store's milliseconds",
            [("a.json", 8_750_000_000, False), ("b.json", 8_717_000_000, False), ("c.json", 8_689_000_000, False), ("d.json", 8_657_000_000, False)],
            None,
        ),
    ]
    now = 10_000_000_000
    cases: list[dict[str, Any]] = []
    for name, entries, limit in listings:
        # A common prefix is no object of a listing's contents, which is all the store's listing answers.
        client = _Listing([(f"_lineage_outbox/{entry}", mtime) for entry, mtime, is_dir in entries if not is_dir])
        with (
            patch.object(outbox, "object_client", lambda _options, client=client: client),
            patch.object(outbox, "fs_and_base", lambda *_: (_Fs(), base)),
            patch.object(outbox.time, "time_ns", lambda: now),
        ):
            listed = list(outbox.list_events("s3://bucket/_lineage_outbox", _NO_OPTIONS, limit=limit))
            depth, age = outbox.backlog("s3://bucket/_lineage_outbox", _NO_OPTIONS)
        cases.append(
            {
                "name": name,
                "objects": [{"key": f"_lineage_outbox/{entry}", "modified_ns": mtime, "directory": is_dir} for entry, mtime, is_dir in entries],
                "limit": limit,
                "now_ns": now,
                "listed": [[key, body.decode()] for key, body in listed],
                "depth": depth,
                "oldest_age_seconds": age,
            }
        )
    return cases


class _Listing:
    """An S3 client whose ``list_objects_v2`` answers ``objects`` (key, nanoseconds since the epoch) in one page, in the
    order given, each ``LastModified`` a ``datetime`` as botocore parses the store's answer."""

    def __init__(self, objects: list[tuple[str, int]]) -> None:
        self._objects = objects

    def get_paginator(self, operation: str) -> _Listing:
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **asked: str) -> list[dict[str, Any]]:
        assert asked == {"Bucket": "bucket", "Prefix": "_lineage_outbox/", "Delimiter": "/"}, asked
        contents = [{"Key": key, "LastModified": datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)} for key, ns in self._objects]
        return [{"Contents": contents}]


class _Fs:
    """A filesystem that reads each object as a text naming it."""

    def open_input_stream(self, path: str) -> _Stream:
        return _Stream(f"body of {path.rsplit('/', 1)[-1]}".encode())


class _Stream:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def readall(self) -> bytes:
        return self._body


def _publishes() -> list[dict[str, Any]]:
    from lakehouse import outbox
    from lakehouse.cloud_events import lineage_envelope

    envelope = lineage_envelope({"eventType": "COMPLETE", "eventTime": "2026-11-17T10:00:00Z", "run": {"runId": "r"}, "job": {"namespace": "j", "name": "n"}})

    async def published(configured: bool, staging: str, publishing: str, dropping: str) -> dict[str, Any]:
        effects: list[str] = []
        counted: list[str] = []

        def stage(*_: object) -> None:
            effects.append("stage")
            if staging == "fails":
                raise ConnectionError("the store did not answer")

        async def publish(*_: object, **__: object) -> None:
            effects.append("publish")
            if publishing == "fails":
                raise TimeoutError

        def drop(*_: object) -> None:
            effects.append("drop")
            if dropping == "fails":
                raise ConnectionError("the store did not answer")

        raised: dict[str, Any] | None = None
        with ExitStack() as patched:
            patched.enter_context(patch.object(outbox, "stage_envelope", stage))
            patched.enter_context(patch.object(outbox, "publish_event", publish))
            patched.enter_context(patch.object(outbox, "drop_event", drop))
            for counter in ("record_staged", "record_stage_failed", "record_published", "record_publish_failed", "record_drop_failed"):
                patched.enter_context(patch.object(outbox.outbox_metrics, counter, lambda _lane, counter=counter: counted.append(counter)))
            try:
                await outbox.publish_with_outbox(
                    _NO_BUS,
                    envelope,
                    outbox_uri="s3://bucket/_lineage_outbox" if configured else "",
                    storage_options=_NO_OPTIONS,
                    topic="lineage.events.v1",
                    timeout_seconds=5.0,
                    lane="lineage",
                )
            except outbox.UnpublishedEventError as exc:
                raised = {"staged": exc.staged, "left_staged": outbox.left_staged(exc)}
        return {
            "configured": configured,
            "staging": staging,
            "publishing": publishing,
            "dropping": dropping,
            "effects": effects,
            "raised": raised,
            "counted": counted,
        }

    async def every() -> list[dict[str, Any]]:
        ways = itertools.product((True, False), ("staged", "fails"), ("sent", "fails"), ("dropped", "fails"))
        return [await published(*way) for way in ways]

    return asyncio.run(every())


def _errors() -> list[dict[str, Any]]:
    from botocore.exceptions import ClientError

    from lakehouse import records
    from lakehouse.storage import BucketNotFoundError, ObjectNotFoundError, s3_errors

    cases: list[dict[str, Any]] = []
    answers = [
        (404, "NoSuchBucket"),
        (404, "NoSuchKey"),
        (404, "404"),
        (404, "NotFound"),
        (403, "AccessDenied"),
        (403, "403"),
        (412, "PreconditionFailed"),
        (412, "412"),
        (409, "PreconditionFailed"),
        (503, "SlowDown"),
        (500, "InternalError"),
        (400, "InvalidRequest"),
    ]
    for (status, code), key in itertools.product(answers, ("k.json", None)):
        response: Any = {"Error": {"Code": code, "Message": "m"}, "ResponseMetadata": {"HTTPStatusCode": status}}
        error = ClientError(response, "Op")
        try:
            with s3_errors(bucket="bucket", key=key):
                raise error
        except BucketNotFoundError:
            found = "bucket_not_found"
        except ObjectNotFoundError:
            found = "object_not_found"
        except ClientError:
            found = "refused"

        class Client:
            def put_object(self, **_: object) -> None:
                raise error  # noqa: B023 - each case's own error, raised while the case runs

        created = "refused"
        with patch.object(records, "object_client", lambda _options: Client()):
            try:
                records.create_json("s3://bucket/root", _NO_OPTIONS, key or "k.json", {})
            except records.RecordExistsError:
                created = "exists"
            except ClientError:
                pass
        cases.append({"status": status, "code": code, "key": key, "found": found, "created": created})
    return cases


def _pairs(query: list[list[str]]) -> Any:  # noqa: ANN401 - botocore takes pairs, in their order, though its stubs say a mapping
    return [(name, value) for name, value in query]


def _signatures() -> list[dict[str, Any]]:
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    requests: list[dict[str, Any]] = [
        {
            "method": "PUT",
            "url": "http://store.test:9000/bucket/_lineage_outbox/r1%40COMPLETE.json",
            "query": [],
            "headers": {},
            "body": '{"id": "r1@COMPLETE"}',
        },
        {"method": "PUT", "url": "http://store.test:9000/bucket/_timers/lineage-relay.json", "query": [], "headers": {"If-None-Match": "*"}, "body": "{}"},
        {"method": "GET", "url": "http://store.test:9000/bucket/p/a%20b%2Bc.json", "query": [], "headers": {}, "body": ""},
        {"method": "DELETE", "url": "https://s3.eu-north-1.amazonaws.com/bucket/p/k.json", "query": [], "headers": {}, "body": "", "region": "eu-north-1"},
        {
            "method": "GET",
            "url": "http://store.test:9000/bucket",
            "query": [["list-type", "2"], ["prefix", "_lineage_outbox/"], ["delimiter", "/"]],
            "headers": {},
            "body": "",
        },
        {
            "method": "GET",
            "url": "http://store.test:9000/bucket",
            "query": [["list-type", "2"], ["prefix", "a b/ü~*"], ["delimiter", "/"], ["continuation-token", "1/Zm9v+YmFy=="]],
            "headers": {},
            "body": "",
        },
        {"method": "GET", "url": "http://store.test:9000/base/path/bucket/k", "query": [], "headers": {}, "body": "", "token": None},
    ]
    cases: list[dict[str, Any]] = []
    with _signed_at_a_fixed_time():
        for request in requests:
            token = request.get("token", "fixture-session-token")
            region = request.get("region", "us-east-1")
            prepared = AWSRequest(
                method=request["method"],
                url=request["url"],
                params=_pairs(request["query"]),
                headers=dict(request["headers"]),
                data=request["body"].encode(),
            )
            S3SigV4Auth(Credentials("fixture-access", "fixture-secret", token), "s3", region).add_auth(prepared)
            signed = {name.lower(): value for name, value in prepared.headers.items()}
            cases.append(request | {"token": token, "region": region, "at": _SIGNED_AT.isoformat(), "signed": signed})
    return cases


def _settings() -> list[dict[str, Any]]:
    from pydantic import ValidationError

    from lineage.core.config import LineageSettings

    token = {"LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/rask/object-store/token", "LINEAGE_S3_STS_ENDPOINT": "http://rask-rustfs:9000"}
    environments: list[dict[str, str]] = [
        token,
        token | {"LINEAGE_S3_ENDPOINT": "http://rask-rustfs:9000"},
        token | {"LINEAGE_S3_REGION": "eu-north-1", "LINEAGE_S3_ASSUME_ROLE_ARN": "arn:aws:iam::1:role/lineage"},
        token | {"lineage_s3_session_seconds": "900"},
        token | {"LINEAGE_S3_SESSION_SECONDS": "43200"},
        token | {"LINEAGE_S3_SESSION_SECONDS": "899"},
        token | {"LINEAGE_S3_SESSION_SECONDS": "43201"},
        token | {"LINEAGE_S3_SESSION_SECONDS": "an hour"},
        token | {"LINEAGE_S3_SESSION_SECONDS": ""},
        {"LINEAGE_S3_WEB_IDENTITY_TOKEN_FILE": "/token"},
        token | {"LINEAGE_S3_ACCESS_KEY_ID": "AKIA"},
        token | {"LINEAGE_S3_SECRET_ACCESS_KEY": "secret"},
        token | {"RASK_S3_CA_BUNDLE": "/etc/rask/store-ca.pem"},
    ]
    cases: list[dict[str, Any]] = []
    for environment in environments:
        with patch.dict(os.environ, environment, clear=True):
            try:
                settings = LineageSettings()
            except ValidationError:
                cases.append({"environment": environment, "refused": True})
                continue
        identity = settings.web_identity
        assert identity is not None
        cases.append(
            {
                "environment": environment,
                "refused": False,
                "token_file": str(identity.token_file),
                "sts_endpoint": identity.endpoint,
                "region": identity.region,
                "role_arn": identity.role_arn,
                "session_seconds": settings.s3_session_seconds,
                "endpoint": settings.s3_endpoint,
            }
        )
    return cases


def _credentials(key: str) -> dict[str, Any]:
    body = (
        '<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleWithWebIdentityResult>'
        f"<Credentials><AccessKeyId>{key}</AccessKeyId><SecretAccessKey>fixture-secret-{key}</SecretAccessKey>"
        f"<SessionToken>fixture-session-token-{key}</SessionToken><Expiration>2026-11-17T11:00:00Z</Expiration></Credentials>"
        "<SubjectFromWebIdentityToken>system:serviceaccount:rask:rask-sa-lineage</SubjectFromWebIdentityToken>"
        "</AssumeRoleWithWebIdentityResult><ResponseMetadata><RequestId>1</RequestId></ResponseMetadata></AssumeRoleWithWebIdentityResponse>"
    )
    return {"status": 200, "body": body}


def _refusal(status: int, code: str) -> dict[str, Any]:
    body = f"<ErrorResponse><Error><Type>Sender</Type><Code>{code}</Code><Message>refused</Message></Error><RequestId>1</RequestId></ErrorResponse>"
    return {"status": status, "body": body}


@contextmanager
def _sts_answering(answers: list[dict[str, Any]], asked: list[dict[str, str]]) -> Generator[None]:
    """``lakehouse.store_session``'s STS client, its requests recorded and answered from ``answers`` in order."""
    from urllib.parse import parse_qsl

    from botocore.awsrequest import AWSResponse
    from botocore.compat import HTTPHeaders

    from lakehouse import store_session
    from lakehouse.storage import sts_client

    class Body:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def stream(self, **_: object) -> Iterator[bytes]:
            return iter([self._body])

    client = sts_client(region="us-east-1", endpoint=_STS)

    def answer(request: Any, **_: object) -> AWSResponse:  # noqa: ANN401 - botocore's request
        asked.append(dict(parse_qsl(request.body.decode() if isinstance(request.body, bytes) else request.body)))
        given = answers.pop(0)
        headers = HTTPHeaders()
        headers["Content-Type"] = "text/xml"
        return AWSResponse(request.url, given["status"], headers, Body(given["body"].encode()))

    client.meta.events.register("before-send", answer)
    with patch.object(store_session, "_sts", lambda *_: client):
        yield


def _exchanges() -> list[dict[str, Any]]:
    from lakehouse import store_session
    from lakehouse.governed.machine_identity import IdentityTokenUnavailableError

    answers: list[tuple[str, str | None, dict[str, Any] | None]] = [
        ("a session", "projected-1", _credentials("ASIA1")),
        ("the token file's whitespace is not sent", "  projected-2\n", _credentials("ASIA2")),
        ("a refused token", "projected-3", _refusal(403, "AccessDenied")),
        ("a subject with no policy", "projected-4", _refusal(400, "InvalidRequest")),
        ("an empty token file asks nothing", "  \n", None),
        ("no token file asks nothing", None, None),
    ]
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as directory:
        for name, token, answer in answers:
            path = Path(directory) / f"token-{len(cases)}"
            if token is not None:
                path.write_text(token)
            identity = store_session.WebIdentity(token_file=path, endpoint=_STS, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/rask")
            asked: list[dict[str, str]] = []
            with _sts_answering([answer] if answer else [], asked):
                try:
                    issued = store_session.exchange(identity, session_name="lineage", duration_seconds=3600)
                    outcome: dict[str, Any] = {"access_key_id": issued.key_pair.access_key_id, "expiration": issued.expiration.isoformat()}
                except IdentityTokenUnavailableError:
                    outcome = {"failed": "token"}
                except Exception as exc:  # recorded: which failure it was
                    outcome = {"failed": type(exc).__name__}
            cases.append({"name": name, "token": token, "answer": answer, "asked": asked, "outcome": outcome})
    return cases


def _sessions() -> list[dict[str, Any]]:
    """Each scenario: STS's answers in order, and the steps the session is put through."""

    def step(op: str, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the step's own arguments
        return {"op": op, **args}

    ok = _credentials
    denied = _refusal(403, "AccessDenied")
    scenarios: list[dict[str, Any]] = [
        {
            "name": "a boot exchange, held until a minute before it ends",
            "seconds": 900,
            "answers": [ok("ASIA1")],
            "steps": [step("boot"), step("held"), step("advance", seconds=839), step("held"), step("advance", seconds=1), step("held")],
        },
        {
            "name": "renewed once half the session is used",
            "seconds": 900,
            "answers": [ok("ASIA1"), ok("ASIA2")],
            "steps": [
                step("boot"),
                step("advance", seconds=449),
                step("pass"),
                step("advance", seconds=1),
                step("pass"),
                step("held"),
                step("pass"),
                step("advance", seconds=840),
                step("held"),
            ],
        },
        {
            "name": "the token file is read again at each exchange",
            "seconds": 3600,
            "answers": [ok("ASIA1"), ok("ASIA2")],
            "steps": [step("boot"), step("token", text="projected-rotated"), step("advance", seconds=1800), step("pass")],
        },
        {
            "name": "a failed renewal keeps the session it holds, then says it expired once",
            "seconds": 900,
            "answers": [ok("ASIA1"), denied, denied, denied, denied, ok("ASIA2")],
            "steps": [
                step("boot"),
                step("advance", seconds=450),
                step("pass"),
                step("held"),
                step("advance", seconds=390),
                step("pass"),
                step("held"),
                step("pass"),
                step("advance", seconds=15),
                step("pass"),
                step("held"),
            ],
        },
        {
            "name": "a token gone from its file fails the renewal without asking",
            "seconds": 900,
            "answers": [ok("ASIA1"), ok("ASIA2")],
            "steps": [
                step("boot"),
                step("token", text=None),
                step("advance", seconds=450),
                step("pass"),
                step("token", text="back"),
                step("pass"),
                step("held"),
            ],
        },
        {
            "name": "the boot waits out a store still being provisioned",
            "seconds": 900,
            "answers": [_refusal(400, "InvalidRequest"), denied, ok("ASIA1")],
            "steps": [step("boot"), step("held")],
        },
        {
            "name": "a boot refused twenty times fails closed",
            "seconds": 900,
            "answers": [denied] * 21,
            "steps": [step("boot"), step("held"), step("pass")],
        },
        {
            "name": "the longest session",
            "seconds": 43200,
            "answers": [ok("ASIA1"), ok("ASIA2")],
            "steps": [step("boot"), step("advance", seconds=21599), step("pass"), step("advance", seconds=1), step("pass"), step("held")],
        },
    ]
    return [_session(scenario) for scenario in scenarios]


def _session(scenario: dict[str, Any]) -> dict[str, Any]:
    from lakehouse import store_session
    from lakehouse.governed.store_credential import StoreCredentialUnavailableError

    now = [1000.0]
    answers = list(scenario["answers"])
    asked: list[dict[str, str]] = []
    said: list[str] = []
    slept: list[float] = []

    class Log:
        def info(self, event: str, **_: object) -> None:
            pass

        def warning(self, event: str, **_: object) -> None:
            said.append(event)

        def error(self, event: str, **_: object) -> None:
            said.append(event)

    with tempfile.TemporaryDirectory() as directory:
        token_file = Path(directory) / "token"
        token_file.write_text("projected-1")
        identity = store_session.WebIdentity(token_file=token_file, endpoint=_STS, region="us-east-1", role_arn="arn:aws:iam::000000000000:role/rask")
        cache = store_session.SessionCache(clock=lambda: now[0])
        session = store_session.StoreSession(identity, session_name="lineage", duration_seconds=scenario["seconds"])
        records: list[dict[str, Any]] = []
        with (
            _sts_answering(answers, asked),
            patch.object(store_session, "SESSIONS", cache),
            patch.object(store_session, "log", Log()),
            patch.object(store_session.time, "sleep", slept.append),
        ):
            for step in scenario["steps"]:
                asked.clear()
                said.clear()
                slept.clear()
                record: dict[str, Any] = dict(step)
                if step["op"] == "boot":
                    try:
                        session.exchange_at_boot()
                        record["booted"] = True
                    except RuntimeError:
                        record["booted"] = False
                    record["slept"] = list(slept)
                elif step["op"] == "pass":
                    record["renewed"] = session.refresh_if_due()
                elif step["op"] == "advance":
                    now[0] += step["seconds"]
                elif step["op"] == "token":
                    if step["text"] is None:
                        token_file.unlink()
                    else:
                        token_file.write_text(step["text"])
                elif step["op"] == "held":
                    try:
                        record["held"] = store_session.held_session(identity).access_key_id
                    except StoreCredentialUnavailableError:
                        record["held"] = None
                record["asked"] = [dict(form) for form in asked]
                record["said"] = list(said)
                records.append(record)
    return {"name": scenario["name"], "seconds": scenario["seconds"], "answers": scenario["answers"], "steps": records}


def outbox_vectors() -> dict[str, list[Any]]:
    """Every group named in the module's docstring."""
    logging.disable(logging.WARNING)
    makers: dict[str, Callable[[], list[Any]]] = {
        "object_keys": _object_keys,
        "urls": _urls,
        "listings": _listings,
        "publishes": _publishes,
        "errors": _errors,
        "signatures": _signatures,
        "settings": _settings,
        "exchanges": _exchanges,
        "sessions": _sessions,
    }
    return {name: make() for name, make in makers.items()}


if __name__ == "__main__":
    print(json.dumps(outbox_vectors(), indent=1)[:4000])
