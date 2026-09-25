"""The stage trigger is UNTRUSTED input, and `from_uri` is a credentialed read.

`handle_stage` consumes a pointer trigger off `medallion.bronze`/`medallion.silver`/`medallion.media`.
Two of its fields used to be read straight off the wire with no shape check at all:

* **`token`** — which seeds the deterministic lineage run id, rides the `lance` run facet into the
  graph, and names the stage's workflow instance. It is also the key a cascade head accepted, so the
  lane and the doors that feed it read ONE grammar: a door wider than the lane answers 202 for a
  cascade the lane then DROPs;
* **`from_uri`** — which becomes the URI the stage runner OPENS with its own object-store credentials
  (`compute.read_upstream` → `lance.dataset(uri, storage_options=settings.storage_options())`), and
  which is also forwarded into a Ray job's `runtime_env` as `FROM_URI`.

An arbitrary `from_uri` on the topic was therefore a read primitive for every bucket that credential
can reach — the rule the training trigger's consumer already applied to its own names
(`services/train.py`) simply had no counterpart here.

What is asserted below is the READ, not merely the status: `read_upstream` is stubbed to record every
URI it is handed, so "refused" means the foreign location was never opened, not that some later step
happened to fail. The refusals are all DROP — deterministic garbage that redelivery cannot fix, and a
DROP rather than a raise because a raising handler poisons the subscription (DATA-CONTRACT §7.3).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import medallion.services.transform as stage_runner
from medallion.api import ingest_media as ingest_media_api
from medallion.api import produce as produce_api
from medallion.api import rerun as rerun_api
from medallion.api.dependencies import get_dapr, get_fga_client, get_settings
from medallion.api.produce_auth import authorize_ingest_media, authorize_produce
from medallion.core.config import MedallionSettings
from medallion.services import inprocess_executor, media_produce, trigger_guards
from medallion.services.compute import UpstreamFacts, WriteResult
from medallion.services.ingest import IngestResult
from medallion.services.ingest_trigger import handle_bronze_arrival
from medallion.services.transform import handle_stage
from medallion.services.trigger_guards import StageTrigger, parse_stage_trigger, safe_token, uri_within
from service_kit.lakehouse import warehouse_registry


#: The ack CONTRACT. Compared field-wise, never whole-dict: since § Q16-7 a drop also carries a
#: `reason`, and an equality here would pin the opacity that row removed.
_DROP_STATUS = "DROP"
_SUCCESS = {"status": "SUCCESS"}


@pytest.fixture(autouse=True)
def _fresh_registry_cache() -> Any:
    """The warehouse registry caches positive resolutions per process; each test provisions its own."""
    warehouse_registry.clear_cache()
    yield
    warehouse_registry.clear_cache()


class _FakeDapr:
    """Records every publish (lineage emits and stage triggers alike)."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, **_: Any) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


class _Reads:
    """Every URI the stage runner actually opened. The claim under test is about reads, so this is the witness."""

    def __init__(self) -> None:
        self.opened: list[str] = []


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> _Reads:
    """Stub the Lance read + write: these tests are about WHICH uri is opened, not about Lance."""
    recorder = _Reads()

    def _read_upstream(uri: str, _storage_options: dict[str, str]) -> UpstreamFacts:
        recorder.opened.append(uri)
        return UpstreamFacts(uri=uri, version=1)

    monkeypatch.setattr(stage_runner, "read_upstream", _read_upstream)
    monkeypatch.setattr(inprocess_executor, "transform_stage", lambda *_a, **_k: WriteResult(version=1, row_count=1, size_bytes=1))
    return recorder


def _provision(control: Path, project: str, root: Path) -> None:
    """One ACTIVE warehouse record for ``project``, in the catalog registry's on-disk shape."""
    registry = control / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "wh1.json").write_text(json.dumps({"id": "wh1", "project": project, "root_uri": str(root), "status": "active"}))


def _stage_runner(**extra: Any) -> MedallionSettings:
    """The bronze→silver stage_runner, compute ON (the path that actually opens `from_uri`)."""
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_namespace": "bronze",
            "from_dataset": "bronze$events",
            "to_namespace": "silver",
            "to_dataset": "silver$features",
            "operation": "embed_features",
            "pub_topic": "medallion.silver",
            **extra,
        }
    )


# ── from_uri: honoured only INSIDE the root this stage resolved ───────────────────────────────────


def test_a_from_uri_outside_the_resolved_root_is_refused_and_never_opened(tmp_path: Path, reads: _Reads) -> None:
    """THE SECURITY GUARD. A trigger naming a location the stage runner has no business reading is DROPped.

    Before the confinement the handler did `from_uri = str(supplied)` and handed it to
    `read_upstream`, so anything that could publish onto the stage topic could name any dataset the
    stage runner's S3 credential can open — a different tenant's warehouse included — and the resulting rows
    would then be written into THIS stage's target under real-looking lineage.
    """
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "decoy-bronze"), to_uri=str(tmp_path / "decoy-silver"), control_root=str(control))

    trigger = {"data": {"token": "t", "project": "acme", "from_uri": "s3://someone-elses-warehouse/medallion/bronze"}}
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))

    assert status["status"] == _DROP_STATUS
    assert reads.opened == [], f"the stage_runner opened a location outside its own root: {reads.opened}"
    assert dapr.published == [], "a refused trigger must leave no lineage and no downstream trigger"


def test_the_catalogs_vended_location_inside_the_root_is_still_honoured(tmp_path: Path, reads: _Reads) -> None:
    """The other half: confinement must not break I2.

    The catalog vends `<root>/<hash>_<ns>$<name>`, a path the stage runner's composed
    `<root>/medallion/<namespace>` never equals — reading the composed path is why the cascade woke
    and found nothing. That vended location lives inside the SAME root the registry resolved, so it
    passes containment and is still what gets opened.
    """
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    vended = f"{wh}/abc123_bronze$events"
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "decoy-bronze"), to_uri=str(tmp_path / "decoy-silver"), control_root=str(control))

    trigger = {"data": {"token": "t", "project": "acme", "from_uri": vended}}
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))

    assert status == _SUCCESS
    assert reads.opened == [vended], "the trigger-named upstream must still win over the composed path"


def test_a_traversal_segment_cannot_climb_back_out_of_the_root(tmp_path: Path, reads: _Reads) -> None:
    """`..` satisfies a prefix check and then escapes — so containment refuses the segment outright."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "decoy-bronze"), to_uri=str(tmp_path / "decoy-silver"), control_root=str(control))

    trigger = {"data": {"token": "t", "project": "acme", "from_uri": f"{wh}/../globex-wh/medallion/bronze"}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))["status"] == _DROP_STATUS
    assert reads.opened == []


def test_a_from_uri_is_refused_when_the_stage_has_no_root_to_confine_it_to(tmp_path: Path, reads: _Reads) -> None:
    """Fail closed. With no project and no configured upstream there is no storage domain, and
    "everything the credential can reach" is the wrong default for the empty case."""
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri="", to_uri=str(tmp_path / "silver"))  # MEDALLION_FROM_URI unset — the default

    trigger = {"data": {"token": "t", "from_uri": str(tmp_path / "somebody-elses" / "bronze")}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))["status"] == _DROP_STATUS
    assert reads.opened == []


def test_a_non_string_from_uri_is_refused_rather_than_coerced(tmp_path: Path, reads: _Reads) -> None:
    """`str(supplied)` accepted ANY json value and stringified it into a URI; a wrong type is malformed."""
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    trigger = {"data": {"token": "t", "from_uri": {"bucket": "elsewhere"}}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))["status"] == _DROP_STATUS
    assert reads.opened == []


def test_no_from_uri_still_uses_the_configured_upstream(tmp_path: Path, reads: _Reads) -> None:
    """The default path stays exactly as it was: absent `from_uri` → the stage runner's own configured URI."""
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))

    assert status == _SUCCESS
    assert reads.opened == [str(tmp_path / "bronze")]


# ── token: the shape every head that mints one already promises, or nothing ───────────────────────


@pytest.mark.parametrize(
    "token",
    [
        "../../etc/passwd",  # traversal + separators
        "..",  # a traversal, not a name
        "a..b",
        "tok en",  # whitespace — no head can mint it, so its presence says the value was not minted
        "tok\nname: evil",  # a newline: harmless in the JSON sinks, and a log line it would forge
        "tok\n",  # a TRAILING newline: an anchored `match` passes it, only `fullmatch` refuses it
        "a" * 65,  # past the Idempotency-Key ceiling (max_length=64)
        "",  # present-but-empty is a claim of "no token", made wrongly
        "bronze$events",  # `$` is the catalog's identifier delimiter, not a token character
        "tok/../../etc",
    ],
)
def test_a_token_outside_the_shape_is_dropped(tmp_path: Path, reads: _Reads, token: str) -> None:
    """The token is not decorative: it seeds the deterministic lineage run id, rides the `lance` facet
    into the graph and names the stage's workflow instance, so a value no door can accept is refused."""
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": token}}))["status"] == _DROP_STATUS
    assert reads.opened == [] and dapr.published == []


#: `(key, whether the stage lane runs it)`. Each row is answered by a door AND by the stage lane that
#: door feeds; a row on which they disagree is a 202 for a cascade that never runs.
_CASCADE_KEYS = [
    ("idem-test", True),
    ("0f1c2d3e4f5a", True),  # a `uuid4().hex[:12]`
    ("8e1c9b7a-2f3d-4c5b-9a01-1234567890ab", True),  # a dashed UUID, `_cascade_token`'s runId fallback
    ("my.retry.key", True),
    ("-leading-dash", True),
    (".", True),
    (".lead", True),
    ("trail.", True),
    ("a" * 64, True),
    ("..", False),
    ("a..b", False),
    ("trail..", False),
    ("a" * 65, False),
    ("", False),
    ("has space", False),
    ("tok$en", False),
    ("a/b", False),
]


def _door(router: APIRouter, settings: MedallionSettings, bus: _FakeDapr, overrides: dict[Callable[..., Any], Callable[[], object]]) -> TestClient:
    """One door, served over HTTP, publishing onto ``bus`` — the same bus its consumer is fed from.

    ``overrides`` replaces the door's authentication only; the grammar under test is not behind it.
    """
    app = FastAPI()
    app.include_router(router)
    app.state.dapr = bus
    app.dependency_overrides[get_dapr] = lambda: bus
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides.update(overrides)
    return TestClient(app)


def _stage_run_token(bus: _FakeDapr, settings: MedallionSettings, since: int) -> str:
    """The token on the COMPLETE the stage emitted — proof the lane RAN the key, not merely parsed it."""
    run = next(p for p in bus.published[since:] if p["topic"] == settings.lineage_topic)
    return run["data"]["run"]["facets"]["lance"]["token"]


@pytest.mark.parametrize(("key", "runs"), _CASCADE_KEYS)
def test_produce_202s_exactly_the_keys_its_stage_lane_runs(tmp_path: Path, reads: _Reads, key: str, runs: bool) -> None:
    """`POST /produce` -> `/bronze-arrival` -> the bronze->silver stage, on ONE bus.

    The key reaches the stage as the bronze write's `lance.token`, and each hop is fed exactly what the
    hop before it published — so a door wider than the lane, a door that rewrites its key, or a head
    that drops it fails here. A refused key publishes nothing.
    """
    bus = _FakeDapr()
    head = MedallionSettings.model_validate({})  # compute off: the head emits its bronze write without seeding one

    door = _door(produce_api.router, head, bus, {authorize_produce: lambda: None}).post("/produce", headers={"Idempotency-Key": key})

    assert door.status_code == (202 if runs else 422), door.text
    if not runs:
        assert bus.published == [], "a refused key published a cascade head"
        return
    assert door.json()["token"] == key
    (bronze_write,) = bus.published
    assert asyncio.run(handle_bronze_arrival(cast(DaprClient, bus), head, {"data": bronze_write["data"]})) == _SUCCESS
    trigger = bus.published[-1]
    assert trigger["topic"] == head.bronze_topic
    stage = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))
    since = len(bus.published)
    assert asyncio.run(handle_stage(cast(DaprClient, bus), stage, {"data": trigger["data"]})) == _SUCCESS
    assert _stage_run_token(bus, stage, since) == key


_MEDIA_BRONZE = "s3://lake/medallion/bronze-media"


@pytest.mark.parametrize(("key", "runs"), _CASCADE_KEYS)
def test_ingest_media_202s_exactly_the_keys_its_stage_lane_runs(tmp_path: Path, reads: _Reads, monkeypatch: pytest.MonkeyPatch, key: str, runs: bool) -> None:
    """`POST /ingest-media` -> the bronze-media->silver-media stage, on ONE bus. The media head publishes
    its own trigger, so the key reaches the lane in one hop."""
    bus = _FakeDapr()
    head = MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "s3_endpoint": "http://rustfs:9000",
            "s3_secret_access_key": "k",
            "media_bronze_uri": _MEDIA_BRONZE,
            "media_source_bucket": "media-src",
        }
    )
    landed = IngestResult(version=1, row_count=1, source_uris=["s3://media-src/batch/a.png"], fields=[])
    monkeypatch.setattr(media_produce, "_seed_and_ingest", lambda *_a: landed)

    door = _door(ingest_media_api.router, head, bus, {authorize_ingest_media: lambda: None}).post("/ingest-media", headers={"Idempotency-Key": key})

    assert door.status_code == (202 if runs else 422), door.text
    if not runs:
        assert bus.published == [], "a refused key published a media head"
        return
    assert door.json()["token"] == key
    trigger = next(p for p in bus.published if p["topic"] == head.media_topic)
    stage = _stage_runner(
        from_namespace=head.media_bronze_namespace,
        from_dataset=head.media_bronze_dataset,
        to_namespace="silver-media",
        to_dataset="silver-media$objects",
        from_uri=_MEDIA_BRONZE,
        to_uri=str(tmp_path / "silver-media"),
    )
    since = len(bus.published)
    assert asyncio.run(handle_stage(cast(DaprClient, bus), stage, {"data": trigger["data"]})) == _SUCCESS
    assert _stage_run_token(bus, stage, since) == key


@pytest.mark.parametrize(("key", "runs"), _CASCADE_KEYS)
def test_rerun_202s_exactly_the_tokens_its_stage_lane_runs(tmp_path: Path, reads: _Reads, monkeypatch: pytest.MonkeyPatch, key: str, runs: bool) -> None:
    """The operator's re-run verb re-mints a stage trigger carrying the caller's `token` verbatim, so
    its body field is a door onto the same lane and answers to the same grammar."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    bus = _FakeDapr()
    verb = MedallionSettings.model_validate(
        {"stage_runner_gates": {"silver": {"to_namespace": "gold", "required_action": "can_promote"}}, "transform_routes": {"silver": "medallion.silver"}}
    )

    async def _allowed(*_a: object, **_k: object) -> bool:
        return True

    monkeypatch.setattr(rerun_api.fga, "check", _allowed)
    client = _door(rerun_api.router, verb, bus, {rerun_api.authenticate_subject: lambda: "alice", get_fga_client: object})
    door = client.post("/stage-runners/stages/rerun", json={"object_id": "table:acme-silver$features", "project": "acme", "to_version": 2, "token": key})

    assert door.status_code == (202 if runs else 422), door.text
    if not runs:
        assert bus.published == [], "a refused token published a stage trigger"
        return
    assert door.json()["token"] == key
    (trigger,) = bus.published
    stage = _stage_runner(
        from_namespace="silver",
        from_dataset="silver$features",
        to_namespace="gold",
        to_dataset="gold$features",
        operation="aggregate_gold",
        pub_topic="medallion.gold",
        control_root=str(control),
    )
    assert asyncio.run(handle_stage(cast(DaprClient, bus), stage, {"data": trigger["data"]})) == _SUCCESS
    assert _stage_run_token(bus, stage, 1) == key


def test_an_absent_token_still_proceeds(tmp_path: Path, reads: _Reads) -> None:
    """Over-tightening guard: absent makes no claim (the estate's rule for every optional trigger
    field), and `submission_id` already has a `notoken` branch for exactly this case."""
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {}})) == _SUCCESS


# ── the envelope itself ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", ["not-an-envelope", {"data": "not-a-payload"}, {"data": ["nope"]}, {}, None])
def test_an_unparseable_envelope_is_dropped_instead_of_transformed(tmp_path: Path, reads: _Reads, event: Any) -> None:
    """A payload that is not a trigger must not run a stage.

    Every field guard used to be an independent `if isinstance(data, dict)`, so an envelope carrying
    no readable payload at all fell through with every field `None` and the stage runner did a full
    transform + emit on the strength of its own env config — a run nothing asked for.
    """
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, event))["status"] == _DROP_STATUS
    assert reads.opened == [] and dapr.published == []


def test_unknown_fields_are_tolerated(tmp_path: Path, reads: _Reads) -> None:
    """DATA-CONTRACT §7.4 is additive-only: a publisher may add optional fields and an older consumer
    must keep working. `from_version`/`to_version` already ride this payload and nothing reads them here."""
    dapr = _FakeDapr()
    settings = _stage_runner(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))
    trigger = {"data": {"token": "t", "namespace": "bronze", "from_version": 3, "to_version": 4, "invented_later": True}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger)) == _SUCCESS


# ── the guard module itself ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("base", "candidate", "expected"),
    [
        ("s3://acme-wh", "s3://acme-wh/abc_bronze$events", True),
        ("s3://acme-wh/", "s3://acme-wh/abc_bronze$events", True),  # a trailing slash on the root is the same root
        ("s3://acme-wh", "s3://acme-wh", True),  # the root itself
        ("s3://acme-wh", "s3://acme-wh-evil/x", False),  # the reason containment appends the separator
        ("s3://acme-wh", "s3://acme-whx", False),
        ("s3://acme-wh", "s3://other/x", False),
        ("s3://acme-wh", "s3://acme-wh/../other/x", False),  # prefix-passes, then escapes
        ("s3://acme-wh", "s3://acme-wh/a\nb", False),  # a newline: no vended location holds one, so it was not vended
        ("s3://acme-wh", "s3://acme-wh@evil/x", False),  # a userinfo-looking authority is a different host
        ("s3://acme-wh", "s3://acme-wh/%2e%2e/other", True),  # STATED LIMIT: containment is lexical, not URI-normalizing
        ("", "s3://anything/at/all", False),  # no root → no containment, ever
        ("s3://acme-wh", "", False),
    ],
)
def test_uri_within(base: str, candidate: str, expected: bool) -> None:
    """The containment table, including the ONE case that passes and looks like it should not.

    `%2e%2e` is not decoded here, and that is a bounded decision rather than an oversight: the
    boundary being enforced is the storage DOMAIN, an S3 keyspace is flat so no key can climb out of
    a bucket, and object_store does not percent-decode a bare filesystem root. Pinned so that a
    future change which starts normalizing the URI before opening it has to come back through here.
    """
    assert uri_within(base, candidate) is expected


def _head_accepts(module: str, path: str) -> Callable[[str], bool]:
    """Whether the head serving `path` accepts a key, read off its declared `Idempotency-Key` header."""
    route = next(r for r in importlib.import_module(module).router.routes if isinstance(r, APIRoute) and r.path == path)
    field = next(f for f in route.dependant.header_params if f.alias == "Idempotency-Key")
    limits = {type(c).__name__: c for c in field.field_info.metadata}
    pattern = next(c.pattern for c in field.field_info.metadata if getattr(c, "pattern", None))
    low, high = limits["MinLen"].min_length, limits["MaxLen"].max_length
    return lambda key: low <= len(key) <= high and re.fullmatch(pattern, key) is not None


#: Keys either side of every edge the grammar has: length, the alphabet, one dot, two dots, and the
#: trailing newline an anchored pattern lets `match` through.
_KEY_PROBES = [
    "",
    "a",
    "a" * 64,
    "a" * 65,
    "a.b",
    ".",
    ".a",
    "a.",
    "..",
    "a..b",
    "a..",
    "-lead",
    "_x",
    "8e1c9b7a-2f3d-4c5b",
    "a b",
    "a/b",
    "a$b",
    "a\nb",
    "a\n",
    "tok\t",
]


@pytest.mark.parametrize(("module", "path"), [("medallion.api.produce", "/produce"), ("medallion.api.ingest_media", "/ingest-media")])
def test_each_head_declares_exactly_the_stage_token_grammar(module: str, path: str) -> None:
    """A head's declared `Idempotency-Key` is the stage lane's `safe_token`, key for key.

    Read off the declaration FastAPI resolved — the one its OpenAPI schema is rendered from — so a
    head that admits a key the lane drops, or refuses one it runs, fails here. The training lane is fed
    by neither head: `tests/unit/test_train.py` holds `POST /train` to its own `TOKEN_PATTERN`.
    """
    head_accepts = _head_accepts(module, path)
    assert [k for k in _KEY_PROBES if head_accepts(k) != safe_token(k)] == []


def test_the_stage_token_grammar_is_pinned() -> None:
    """Three doors and one lane read this grammar, so a change to it is made on purpose."""
    assert trigger_guards.SAFE_TOKEN_PATTERN == r"^\.?(?:[A-Za-z0-9_-]+\.)*[A-Za-z0-9_-]*$"
    assert trigger_guards.SAFE_TOKEN_MAX_LENGTH == 64
    dangerous = ["", "has space", "has/slash", "has$dollar", "..", "a..b", "a/../b", "a\nb", "tok\t", "a" * 65]
    assert not any(safe_token(s) for s in dangerous)
    assert safe_token(1) is False


def test_parse_stage_trigger_returns_none_rather_than_raising() -> None:
    """The handler contract is a status dict; an exception escaping into the subscription route is
    what poisons a subscription, so the parser reports failure by value."""
    assert parse_stage_trigger({"data": {"token": "../evil"}}) is None
    assert parse_stage_trigger("not-an-envelope") is None
    parsed = parse_stage_trigger({"data": {"token": "t", "dataset": "bronze$events"}})
    assert isinstance(parsed, StageTrigger) and parsed.token == "t" and parsed.dataset == "bronze$events"
