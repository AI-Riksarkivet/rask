"""Unit tests for the lineage service's auth gate (audit ``w8u4rc2tg``, P0 #1/#2).

Infra-free: no catalog, no network. Drives the async gate with stdlib ``asyncio.run`` (the project
convention — see ``test_fga_resilience.py``) and fakes the OpenFGA check, the OIDC verifier and the store.

Covers:
* fail-closed config (a half-configured auth layer is a startup error),
* authn (off → open; on → token required / verified / fail-closed when unwired),
* the read authz gate (off → allow; unwired → 503; unauthenticated → 401; deny → 403; allow),
* author-forgery prevention (the verified subject overrides any body-claimed author),
* the governed feed (`GET /events`): what a caller may see, and how its cursors move.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from pydantic import ValidationError

from lineage.api import fga_deps, security
from lineage.core.config import LineageSettings
from lineage.models import RunEvent
from lineage.schemas import EventRecord
from lineage.services.provenance import ProvenanceStore
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


_ISSUER = "https://idp.example.com"


def _settings(**values: Any) -> LineageSettings:
    """Build settings from field names (``model_validate`` runs the fail-closed validator)."""
    return LineageSettings.model_validate(values)


def _token(sub: str = "alice") -> IDToken:
    return IDToken(iss=_ISSUER, sub=sub, aud="lance", exp=0, iat=0)


def _request(**state: object) -> Request:
    """A fake request exposing only ``request.app.state`` (the gate reads ``oidc``/``fga``)."""
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state))))


def _creds(value: str = "tok") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


_FULL_AUTH = {
    "oidc_enabled": True,
    "oidc_issuer": _ISSUER,
    "oidc_audience": "lance",
    "fga_enabled": True,
    "fga_store_id": "store",
    "fga_model_id": "model",
}


# --------------------------------------------------------------------------- #
# Fail-closed config
# --------------------------------------------------------------------------- #


def test_oidc_enabled_requires_issuer_and_audience() -> None:
    with pytest.raises(ValidationError):
        _settings(oidc_enabled=True)


def test_fga_enabled_requires_oidc() -> None:
    with pytest.raises(ValidationError):
        _settings(fga_enabled=True, oidc_enabled=False)


# --------------------------------------------------------------------------- #
# authenticate (authn)
# --------------------------------------------------------------------------- #


def test_authenticate_enabled_unwired_verifier_fails_closed() -> None:
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance")
    with pytest.raises(ServiceUnavailableError):
        security.authenticate(_request(), settings, _creds())


def test_authenticate_enabled_verifies_token() -> None:
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance")
    verifier = SimpleNamespace(verify=lambda _t: _token("dee"))
    token = security.authenticate(_request(oidc=verifier), settings, _creds())
    assert token is not None and token.sub == "dee"


# --------------------------------------------------------------------------- #
# require_metadata_access (read authz gate)
# --------------------------------------------------------------------------- #


def test_gate_disabled_allows() -> None:
    # FGA off → no check, no raise (dev/test default, like the catalog).
    asyncio.run(fga_deps.require_metadata_access("a$b", _request(), _settings(), None))


def test_gate_unwired_client_fails_closed() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(fga_deps.require_metadata_access("a$b", _request(), _settings(**_FULL_AUTH), _token()))


def test_gate_unauthenticated_raises() -> None:
    with pytest.raises(UnauthenticatedError):
        asyncio.run(fga_deps.require_metadata_access("a$b", _request(fga=object()), _settings(**_FULL_AUTH), None))


def test_gate_denies_without_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _deny(_client: object, *, user: str, relation: str, obj: str) -> bool:
        captured.update(user=user, relation=relation, obj=obj)
        return False

    monkeypatch.setattr(fga, "check", _deny)
    client = cast(OpenFgaClient, object())
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps.require_metadata_access("a$b", _request(fga=client), _settings(**_FULL_AUTH), _token()))
    # The denial must be on the right user/object/relation, not just "some" denial.
    assert captured == {"user": "alice", "relation": "can_get_metadata", "obj": "table:a$b"}


# --------------------------------------------------------------------------- #
# enforce_author (provenance forgery prevention)
# --------------------------------------------------------------------------- #


def _event(claimed_author: str = "anon", outputs: list[str] | None = None, inputs: list[str] | None = None) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-06-24T00:00:00Z",
            "run": {"runId": "r1", "facets": {"author": {"name": claimed_author}}},
            "job": {"namespace": "jobs", "name": "promote"},
            "inputs": [{"namespace": "silver", "name": n} for n in (inputs or [])],
            "outputs": [{"namespace": "silver", "name": n} for n in (outputs or [])],
        }
    )


# --------------------------------------------------------------------------- #
# DatasetFilter — transitive-disclosure filtering (audit w8u4rc2tg, security medium)
# --------------------------------------------------------------------------- #


async def _batch_allow_a(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
    """Fake batch_check: only ``table:a`` is visible."""
    return {o: o == "table:a" for o in objects}


# --------------------------------------------------------------------------- #
# Handler-body behavior the route-dependency introspection can't see:
# the read handler must APPLY the filter, and ingest must bind the verified author.
# --------------------------------------------------------------------------- #


class _FakeStore:
    """The store surface the feed door and the ingest door reach: a canned feed, and what was recorded."""

    def __init__(self) -> None:
        self.recorded: RunEvent | None = None
        self.events: list[EventRecord] = []
        self.list_events_calls: list[dict[str, object]] = []
        self.oldest: int | None = None

    async def record(self, event: RunEvent) -> None:
        self.recorded = event

    async def run_author(self, _run_id: str) -> str | None:
        return None

    async def list_events(self, limit: int = 500, *, after: int | None = None, since: int | None = None, summary: bool = False) -> list[EventRecord]:
        self.list_events_calls.append({"limit": limit, "after": after, "summary": summary})
        return self.events

    async def feed_bounds(self) -> tuple[int, int] | None:
        return (self.oldest, max((event.seq for event in self.events), default=self.oldest)) if self.oldest is not None else None


def test_get_events_filters_to_visible_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    # #22: the durable events feed is governed — drop events referencing a dataset the caller can't see.
    from lineage.api.v1.endpoints.events import get_events

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeStore()
    repo.events = [
        EventRecord(seq=2, outputs=["a"], inputs=[], event={}),
        EventRecord(seq=1, outputs=["b"], inputs=[], event={}),  # "b" not visible → dropped
    ]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings))
    assert [e.outputs for e in result.events] == [["a"]]  # the "b" event is filtered out


def test_get_events_hides_dataset_less_events_when_governed(monkeypatch: pytest.MonkeyPatch) -> None:
    # #22 audit: a dataset-less event (empty inputs+outputs) must NOT pass the gate vacuously when FGA is
    # on — it would otherwise leak the run/author/full event JSON to a caller with zero grants.
    from lineage.api.v1.endpoints.events import get_events

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)
    settings = _settings(**_FULL_AUTH)
    repo = _FakeStore()
    repo.events = [EventRecord(seq=1, outputs=[], inputs=[], event={"run": {"runId": "secret"}})]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings))
    assert result.events == []  # dataset-less event is hidden under governance


def test_get_events_pagination_never_leaks_past_governance(monkeypatch: pytest.MonkeyPatch) -> None:
    # §2 perf (2026-07-11) guardrail pin: the governance filter runs BEFORE the slice on EVERY page —
    # a small `limit` must page through VISIBLE rows only; a hidden row never appears on any page.
    from lineage.api.v1.endpoints.events import get_events

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeStore()
    repo.events = [
        EventRecord(seq=3, outputs=["a"], inputs=[], event={}),
        EventRecord(seq=2, outputs=["b"], inputs=[], event={}),  # hidden — must never surface
        EventRecord(seq=1, outputs=["a"], inputs=[], event={}),
    ]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings, limit=1))
    assert [e.seq for e in result.events] == [3]
    assert result.next_cursor == 3  # continue below the last VISIBLE row — not below the hidden one
    # auth ON keeps the wide fetch window so filtered-out rows don't starve the page
    assert repo.list_events_calls[-1]["limit"] == 2000


def test_get_events_cursor_and_fetch_width(monkeypatch: pytest.MonkeyPatch) -> None:
    # Auth OFF → governed() is pass-through, so the endpoint reads EXACTLY `limit` rows (the 2s
    # poll no longer pays a 2000-row window), and the keyset cursor advances / terminates correctly.
    from lineage.api.v1.endpoints.events import get_events

    settings = _settings()  # auth off
    repo = _FakeStore()
    flt = fga_deps.DatasetFilter(_request(), settings, None)
    repo.events = [
        EventRecord(seq=5, outputs=["a"], inputs=[], event={}),
        EventRecord(seq=4, outputs=["a"], inputs=[], event={}),
    ]
    page = asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings, limit=2))
    assert repo.list_events_calls[-1] == {"limit": 2, "after": None, "summary": False}
    assert [e.seq for e in page.events] == [5, 4] and page.next_cursor == 4  # full page → continue

    repo.events = [EventRecord(seq=3, outputs=["a"], inputs=[], event={})]
    page = asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings, after=4, limit=2))
    assert repo.list_events_calls[-1] == {"limit": 2, "after": 4, "summary": False}
    assert [e.seq for e in page.events] == [3] and page.next_cursor is None  # short page → exhausted

    # summary rides through to the store's read untouched
    asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings, summary=True))
    assert repo.list_events_calls[-1]["summary"] is True


def test_ingest_handler_binds_verified_author() -> None:
    from lineage.api.v1.endpoints.ingest import ingest_event

    repo = _FakeStore()
    event = _event(claimed_author="attacker")
    asyncio.run(ingest_event(event.model_dump(by_alias=True), _request(), cast(ProvenanceStore, repo), _settings(), _token("real-user")))
    assert repo.recorded is not None and repo.recorded.author == "real-user"  # body claim overridden


def test_ingest_handler_keeps_body_author_when_oidc_off() -> None:
    from lineage.api.v1.endpoints.ingest import ingest_event

    repo = _FakeStore()
    event = _event(claimed_author="claimed")
    asyncio.run(ingest_event(event.model_dump(by_alias=True), _request(), cast(ProvenanceStore, repo), _settings(), None))
    assert repo.recorded is not None and repo.recorded.author == "claimed"


# --------------------------------------------------------------------------- #
# #2 — output-scoped ingest authz: a producer may only record provenance for
# outputs it can WRITE (can_write_data), not just that it's authenticated.
# --------------------------------------------------------------------------- #


def test_output_authz_unwired_client_fails_closed() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(fga_deps.enforce_output_authz(_event(outputs=["a$b"]), _request(), _settings(**_FULL_AUTH), _token()))


def test_output_authz_output_less_still_requires_auth() -> None:
    # REGRESSION (bug hunt 2026-07-13): an event with NO outputs must STILL require a valid principal.
    # The pre-fix code short-circuited (`if not outputs: return`) BEFORE the auth checks, so an
    # unauthenticated caller could record a forged inputs-only run in the governed feed.
    with pytest.raises(UnauthenticatedError):
        asyncio.run(fga_deps.enforce_output_authz(_event(inputs=["a$b"]), _request(fga=object()), _settings(**_FULL_AUTH), None))


def test_output_authz_denies_non_writable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # alice may write a$b but NOT c$d → the whole ingest is denied (403).
    async def _batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        return {o: (o == "table:a$b") for o in objects}

    monkeypatch.setattr(fga, "batch_check", _batch)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            fga_deps.enforce_output_authz(
                _event(outputs=["a$b", "c$d"]),
                _request(fga=cast(OpenFgaClient, object())),
                _settings(**_FULL_AUTH),
                _token(),
            )
        )


# --- A service principal on the HTTP ingest ----------------------------------------------------------
# The Ray TRAIN job is a sidecar-less IN-CLUSTER producer (RAY-TRAIN.md D2): it POSTs the HTTP ingest with its
# pod's projected token, which the door verifies into a `ServicePrincipal` (`service_kit.governed.machine_identity`). The
# door itself is driven over HTTP in `services/lineage/tests/test_a_claimed_service_name_authenticates_nobody.py`;
# this pins what the ingest gates do with the principal it hands them.


def test_service_principal_is_attributed_and_fga_bounded() -> None:
    """The ServicePrincipal flows through BOTH ingest gates: it is stamped as author AND FGA-checked.

    This is what keeps D5 intact end-to-end — the trainer records provenance only for what its rung
    (`writer` on `namespace:models`) actually permits, and is *stricter* than the Dapr subscription route
    it mirrors (which trusts the producer-stamped author outright).
    """
    principal = security.ServicePrincipal(service_account="system:serviceaccount:default:rask-sa-ray")
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-13T09:00:00+00:00",
            "producer": "https://github.com/Borg93/lance-ns",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {}},
            "job": {"namespace": "ray-jobs", "name": "train.churn"},
            "inputs": [],
            "outputs": [{"namespace": "models", "name": "models$churn", "facets": {}}],
        }
    )
    # (1) attributed — the run's author is the service's verified subject
    fga_deps.enforce_author(event, principal)
    trainer = "kubernetes~system:serviceaccount:default:rask-sa-ray"
    assert event.run.facets["author"] == {"name": trainer, "sub": trainer}

    # (2) authorized — the FGA check runs as the SERVICE subject, and a denial is a 403
    checked: dict[str, Any] = {}

    async def _batch_check(_client: Any, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        checked.update(user=user, relation=relation, objects=objects)
        return dict.fromkeys(objects, False)  # the trainer may NOT write this output

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fga, "batch_check", _batch_check)
        with pytest.raises(PermissionDeniedError):
            asyncio.run(
                fga_deps.enforce_output_authz(
                    event,
                    _request(fga=cast(OpenFgaClient, object())),
                    _settings(**_FULL_AUTH),
                    principal,
                )
            )
    assert checked["user"] == trainer  # NOT an opaque Dex sub
    assert checked["relation"] == "can_write_data"
    assert checked["objects"] == ["table:models$churn"]


def test_events_governs_on_columnlineage_source_datasets() -> None:
    # AUDIT: /events returned the full raw payload but governed only on top-level inputs/outputs. The
    # columnLineage facet names SOURCE datasets NOT in inputs, so a caller who can see the output would get
    # input datasets' column schemas. The ref-extractor now unions those in (event hidden unless the caller
    # can read every dataset the payload discloses).
    from lineage.api.v1.endpoints.events import _column_lineage_datasets

    event = {
        "outputs": [
            {
                "name": "gold$t",
                "facets": {"columnLineage": {"fields": {"col": {"inputFields": [{"namespace": "bronze", "name": "bronze$secret"}]}}}},
            }
        ]
    }
    assert _column_lineage_datasets(event) == {"bronze$secret"}
    assert _column_lineage_datasets({}) == set()  # summary=True → no payload → no extra refs, nothing hidden


# ── the public front door must not be able to mint a service principal ────────


def test_get_events_reports_the_feed_FLOOR_so_a_consumer_can_see_what_it_lost() -> None:
    """`oldest_seq` — the oldest row the feed still holds.

    Lineage prunes its feed on the relay tick, dropping the rows it received longest ago. That
    prune cannot consult any consumer's cursor: the notifications reconciler's mark lives in ITS Dapr
    state store, which lineage is not scoped to and must never be — reaching across would couple the
    producer of the feed to one of its readers.

    So lineage reports its FLOOR and the consumer draws its own conclusion. Without it a reader whose
    mark fell below the floor walks to the end of the feed, sees `next_cursor: None`, and concludes it
    is caught up — the rows in between were deleted before it read them, and nothing anywhere says so.

    UNGOVERNED on purpose: a seq number discloses no dataset, and it is exactly the number a caller
    needs in order to know a page it CANNOT see is missing.
    """
    from lineage.api.v1.endpoints.events import get_events

    settings = _settings()  # auth off
    repo = _FakeStore()
    repo.oldest = 8999
    flt = fga_deps.DatasetFilter(_request(), settings, None)
    repo.events = [EventRecord(seq=9000, outputs=["a"], inputs=[], event={})]

    page = asyncio.run(get_events(cast(ProvenanceStore, repo), flt, settings, limit=2))

    assert page.oldest_seq == 8999
