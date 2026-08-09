"""The stage trigger is UNTRUSTED input, and `from_uri` is a credentialed read.

`handle_stage` consumes a pointer trigger off `medallion.bronze`/`medallion.silver`/`medallion.media`.
Two of its fields used to be read straight off the wire with no shape check at all:

* **`token`** — which seeds the deterministic lineage run id, rides the `lance` run facet into the
  graph, and folds into a Ray submission id. `ray_kit.submit.submission_id` builds
  `ray-<stage>-<token>-<digest>` and then replaces every character outside `[A-Za-z0-9_-]` with `-`,
  so the hazard there is not injection but COLLISION: two different tokens land on one id, and
  `submit_or_reattach` reads that as a successful re-attach — the second stage's work never runs;
* **`from_uri`** — which becomes the URI the mover OPENS with its own object-store credentials
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
import json
from pathlib import Path
from typing import Any, cast

import medallion.services.transform as mover
import pytest
from dapr.aio.clients import DaprClient
from medallion.core.config import MedallionSettings
from medallion.services.compute import UpstreamFacts, WriteResult
from medallion.services.train import _safe_name as _train_safe_name
from medallion.services.transform import handle_stage
from medallion.services.trigger_guards import SAFE_TOKEN_PATTERN, StageTrigger, parse_stage_trigger, safe_token, uri_within

from service_kit.lakehouse import warehouse_registry


_DROP = {"status": "DROP"}
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
    """Every URI the mover actually opened. The claim under test is about reads, so this is the witness."""

    def __init__(self) -> None:
        self.opened: list[str] = []


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> _Reads:
    """Stub the Lance read + write: these tests are about WHICH uri is opened, not about Lance."""
    recorder = _Reads()

    def _read_upstream(uri: str, _storage_options: dict[str, str]) -> UpstreamFacts:
        recorder.opened.append(uri)
        return UpstreamFacts(uri=uri, version=1)

    monkeypatch.setattr(mover, "read_upstream", _read_upstream)
    monkeypatch.setattr(mover, "transform_stage", lambda *_a, **_k: WriteResult(version=1, row_count=1, size_bytes=1))
    return recorder


def _provision(control: Path, project: str, root: Path) -> None:
    """One ACTIVE warehouse record for ``project``, in the catalog registry's on-disk shape."""
    registry = control / "_warehouses"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / "wh1.json").write_text(json.dumps({"id": "wh1", "project": project, "root_uri": str(root), "status": "active"}))


def _mover(**extra: Any) -> MedallionSettings:
    """The bronze→silver mover, compute ON (the path that actually opens `from_uri`)."""
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
    """THE SECURITY GUARD. A trigger naming a location the mover has no business reading is DROPped.

    Before the confinement the handler did `from_uri = str(supplied)` and handed it to
    `read_upstream`, so anything that could publish onto the stage topic could name any dataset the
    mover's S3 credential can open — a different tenant's warehouse included — and the resulting rows
    would then be written into THIS stage's target under real-looking lineage.
    """
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "decoy-bronze"), to_uri=str(tmp_path / "decoy-silver"), control_root=str(control))

    trigger = {"data": {"token": "t", "project": "acme", "from_uri": "s3://someone-elses-warehouse/medallion/bronze"}}
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))

    assert status == _DROP
    assert reads.opened == [], f"the mover opened a location outside its own root: {reads.opened}"
    assert dapr.published == [], "a refused trigger must leave no lineage and no downstream trigger"


def test_the_catalogs_vended_location_inside_the_root_is_still_honoured(tmp_path: Path, reads: _Reads) -> None:
    """The other half: confinement must not break I2.

    The catalog vends `<root>/<hash>_<ns>$<name>`, a path the mover's composed
    `<root>/medallion/<namespace>` never equals — reading the composed path is why the cascade woke
    and found nothing. That vended location lives inside the SAME root the registry resolved, so it
    passes containment and is still what gets opened.
    """
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    vended = f"{wh}/abc123_bronze$events"
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "decoy-bronze"), to_uri=str(tmp_path / "decoy-silver"), control_root=str(control))

    trigger = {"data": {"token": "t", "project": "acme", "from_uri": vended}}
    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger))

    assert status == _SUCCESS
    assert reads.opened == [vended], "the trigger-named upstream must still win over the composed path"


def test_a_traversal_segment_cannot_climb_back_out_of_the_root(tmp_path: Path, reads: _Reads) -> None:
    """`..` satisfies a prefix check and then escapes — so containment refuses the segment outright."""
    control, wh = tmp_path / "control", tmp_path / "acme-wh"
    _provision(control, "acme", wh)
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "decoy-bronze"), to_uri=str(tmp_path / "decoy-silver"), control_root=str(control))

    trigger = {"data": {"token": "t", "project": "acme", "from_uri": f"{wh}/../globex-wh/medallion/bronze"}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger)) == _DROP
    assert reads.opened == []


def test_a_from_uri_is_refused_when_the_stage_has_no_root_to_confine_it_to(tmp_path: Path, reads: _Reads) -> None:
    """Fail closed. With no project and no configured upstream there is no storage domain, and
    "everything the credential can reach" is the wrong default for the empty case."""
    dapr = _FakeDapr()
    settings = _mover(from_uri="", to_uri=str(tmp_path / "silver"))  # MEDALLION_FROM_URI unset — the default

    trigger = {"data": {"token": "t", "from_uri": str(tmp_path / "somebody-elses" / "bronze")}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger)) == _DROP
    assert reads.opened == []


def test_a_non_string_from_uri_is_refused_rather_than_coerced(tmp_path: Path, reads: _Reads) -> None:
    """`str(supplied)` accepted ANY json value and stringified it into a URI; a wrong type is malformed."""
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    trigger = {"data": {"token": "t", "from_uri": {"bucket": "elsewhere"}}}

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, trigger)) == _DROP
    assert reads.opened == []


def test_no_from_uri_still_uses_the_configured_upstream(tmp_path: Path, reads: _Reads) -> None:
    """The default path stays exactly as it was: absent `from_uri` → the mover's own configured URI."""
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    status = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "t"}}))

    assert status == _SUCCESS
    assert reads.opened == [str(tmp_path / "bronze")]


# ── token: the shape every head that mints one already promises, or nothing ───────────────────────


@pytest.mark.parametrize(
    "token",
    [
        "../../etc/passwd",  # traversal + separators
        "..",  # the one dotted value the header pattern admits that is not a name
        "tok en",  # whitespace — no head can mint it, so its presence says the value was not minted
        "tok\nname: evil",  # a newline: harmless in the JSON sinks, and a log line it would forge
        "a" * 65,  # past the Idempotency-Key ceiling (max_length=64)
        "",  # present-but-empty is a claim of "no token", made wrongly
        "bronze$events",  # `$` is the catalog's identifier delimiter, not a token character
        "tok/../../etc",
    ],
)
def test_a_token_outside_the_shape_is_dropped(tmp_path: Path, reads: _Reads, token: str) -> None:
    """The token is not decorative: it seeds the deterministic lineage run id, rides the `lance` facet
    into the graph, and folds into the Ray submission id — where characters outside `[A-Za-z0-9_-]` are
    replaced, so two unshaped tokens can COLLIDE onto one id and the second stage's work never runs."""
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": token}})) == _DROP
    assert reads.opened == [] and dapr.published == []


@pytest.mark.parametrize(
    "token",
    [
        "0f1c2d3e4f5a",  # produce()'s own uuid4().hex[:12]
        "8e1c9b7a-2f3d-4c5b-9a01-1234567890ab",  # _cascade_token's runId fallback
        "my.retry.key",  # a dotted Idempotency-Key — `/produce` advertises `^[A-Za-z0-9._-]+$`
        "-leading-dash",  # also inside that header pattern
        "a" * 64,
    ],
)
def test_every_token_the_estates_own_heads_can_mint_is_accepted(tmp_path: Path, reads: _Reads, token: str) -> None:
    """A consumer stricter than the head that feeds it is not safety, it is a silent DROP of a cascade
    the head already 202'd. `/produce`, `/ingest-media` and `/train` all pin `Idempotency-Key` to
    `^[A-Za-z0-9._-]+$` (max 64) and thread it through as the cascade token, so the mover honours
    exactly that shape — nothing narrower.
    """
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": token}})) == _SUCCESS
    lineage = next(p for p in dapr.published if p["topic"] == settings.lineage_topic)
    assert lineage["data"]["run"]["facets"]["lance"]["token"] == token


def test_an_absent_token_still_proceeds(tmp_path: Path, reads: _Reads) -> None:
    """Over-tightening guard: absent makes no claim (the estate's rule for every optional trigger
    field), and `submission_id` already has a `notoken` branch for exactly this case."""
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {}})) == _SUCCESS


# ── the envelope itself ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", ["not-an-envelope", {"data": "not-a-payload"}, {"data": ["nope"]}, {}, None])
def test_an_unparseable_envelope_is_dropped_instead_of_transformed(tmp_path: Path, reads: _Reads, event: Any) -> None:
    """A payload that is not a trigger must not run a stage.

    Every field guard used to be an independent `if isinstance(data, dict)`, so an envelope carrying
    no readable payload at all fell through with every field `None` and the mover did a full
    transform + emit on the strength of its own env config — a run nothing asked for.
    """
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))

    assert asyncio.run(handle_stage(cast(DaprClient, dapr), settings, event)) == _DROP
    assert reads.opened == [] and dapr.published == []


def test_unknown_fields_are_tolerated(tmp_path: Path, reads: _Reads) -> None:
    """DATA-CONTRACT §7.4 is additive-only: a publisher may add optional fields and an older consumer
    must keep working. `from_version`/`to_version` already ride this payload and nothing reads them here."""
    dapr = _FakeDapr()
    settings = _mover(from_uri=str(tmp_path / "bronze"), to_uri=str(tmp_path / "silver"))
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


def test_the_token_grammar_is_a_strict_superset_of_the_training_consumers() -> None:
    """The two bus consumers' token rules, pinned in RELATION so neither drifts unnoticed.

    They are not identical, and that is the point of stating it: `services/train.py`'s private
    `_safe_name` rejects the dots its OWN head (`POST /train`, `Idempotency-Key` =
    `^[A-Za-z0-9._-]+$`) 202s — so a dotted key there is accepted and then silently DROPped at the
    consumer, which is the defect this module refuses to copy. Every value train accepts must stay
    acceptable here (a superset — never a new DROP), and both must keep refusing the dangerous set.
    """
    accepted_by_train = ["ok", "Ok-1_2", "a" * 64, "0f1c2d3e4f5a", "8e1c9b7a-2f3d-4c5b-9a01-1234567890ab"]
    assert all(_train_safe_name(s) for s in accepted_by_train)
    assert all(safe_token(s) for s in accepted_by_train), "the mover must not DROP a token the trainer accepts"

    dangerous = ["", "has space", "has/slash", "has$dollar", "..", "a/../b", "a\nb", "tok\t", "a" * 65]
    assert not any(_train_safe_name(s) for s in dangerous)
    assert not any(safe_token(s) for s in dangerous)
    assert safe_token(1) is False and _train_safe_name(1) is False

    # The head contract this grammar is derived from, spelled out so a change to either is deliberate.
    assert SAFE_TOKEN_PATTERN == r"[A-Za-z0-9._-]{1,64}"
    assert safe_token("my.retry.key") and not _train_safe_name("my.retry.key")


def test_parse_stage_trigger_returns_none_rather_than_raising() -> None:
    """The handler contract is a status dict; an exception escaping into the subscription route is
    what poisons a subscription, so the parser reports failure by value."""
    assert parse_stage_trigger({"data": {"token": "../evil"}}) is None
    assert parse_stage_trigger("not-an-envelope") is None
    parsed = parse_stage_trigger({"data": {"token": "t", "dataset": "bronze$events"}})
    assert isinstance(parsed, StageTrigger) and parsed.token == "t" and parsed.dataset == "bronze$events"
