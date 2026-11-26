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

import asyncio
import uuid
from collections.abc import Collection
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast

import pytest
from fastapi import FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from lineage_lane import decided
from openfga_sdk import OpenFgaClient
from pydantic import ValidationError

from lakehouse.events.lineage_events import DatasetUuids, with_dataset_uuids
from lakehouse.governed import fga_ids, fga_reads
from lakehouse.governed.decider import OpenFgaDecider
from lakehouse.governed.idp_identity import IdpVerifier
from lakehouse.governed.oidc import OIDCVerifier
from lakehouse.governed.settings import OidcProvider
from lineage.api import fga_deps, security
from lineage.core.config import LineageSettings
from lineage.models import RunEvent
from lineage.schemas import EventRecord
from lineage.services.names import CatalogNames
from lineage.services.provenance import ProvenanceStore
from tests.callers import person
from tests.unit.no_uuids import NO_CATALOG_NAMES


_ISSUER = "https://idp.example.com"


def _uuid(name: str) -> str:
    """The uuid the catalog minted for the table ``name`` (ADR 0173 §10)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _object(name: str) -> str:
    """The object the table ``name``'s grants are held by, ``table:<uuid>`` ([[LH-524]])."""
    return fga_ids.table_object(_uuid(name))


class _Names:
    """``CatalogNames``' whole signature: every table name resolves to :func:`_uuid` of it, and the bearers asked
    with are kept."""

    def __init__(self) -> None:
        self.bearers: list[str | None] = []

    async def table(self, name: str, *, bearer: str | None) -> DatasetUuids:
        self.bearers.append(bearer)
        return DatasetUuids(table_uuid=_uuid(name))

    async def namespace(self, name: str, *, bearer: str | None) -> str | None:
        return None


def _settings(**values: Any) -> LineageSettings:
    """Build settings from field names (``model_validate`` runs the fail-closed validator)."""
    return LineageSettings.model_validate(values)


def _request(**state: object) -> Request:
    """A request to an app whose state holds ``state`` (the gate reads ``oidc``/``decider``)."""
    app = FastAPI()
    for name, value in state.items():
        setattr(app.state, name, value)
    return Request({"type": "http", "app": app, "method": "GET", "path": "/", "headers": []})


def _creds(value: str = "tok") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


_FULL_AUTH = {
    "oidc_enabled": True,
    "oidc_issuer": _ISSUER,
    "oidc_audience": "lance",
    "oidc_idp_id": "dex",
    "oidc_connectors": {"local": "dex"},
    "fga_enabled": True,
    "fga_store_id": "store",
}


# --------------------------------------------------------------------------- #
# Fail-closed config
# --------------------------------------------------------------------------- #


def test_oidc_enabled_requires_issuer_and_audience() -> None:
    with pytest.raises(ValidationError):
        _settings(oidc_enabled=True, oidc_idp_id="dex", oidc_connectors={"local": "dex"})


def test_fga_enabled_requires_oidc() -> None:
    with pytest.raises(ValidationError):
        _settings(fga_enabled=True, oidc_enabled=False)


# --------------------------------------------------------------------------- #
# authenticate (authn)
# --------------------------------------------------------------------------- #


def test_authenticate_enabled_unwired_verifier_fails_closed() -> None:
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance", oidc_idp_id="dex", oidc_connectors={"local": "dex"})
    with pytest.raises(ServiceUnavailableError):
        security.authenticate(_request(), settings, _creds())


def test_authenticate_enabled_verifies_token(dex_issuer: Any) -> None:  # noqa: ANN401 — the root conftest's DexIssuer
    """The caller is the person the verifier the lifespan installs reads off a token the issuer signed."""
    provider = OidcProvider(issuer=dex_issuer.issuer, idp_id="dex", connectors={"local": "dex"})
    settings = _settings(
        oidc_enabled=True, oidc_issuer=dex_issuer.issuer, oidc_allow_insecure=True, oidc_audience="lance", oidc_idp_id="dex", oidc_connectors={"local": "dex"}
    )
    verifier = IdpVerifier(OIDCVerifier(dex_issuer.issuer, "lance", 300, allow_insecure=True), [provider])
    token = security.authenticate(_request(oidc=verifier), settings, _creds(dex_issuer.mint("dee", connector="local", audience="lance")))
    assert token is not None
    assert token.subject == "dex~dee"


# --------------------------------------------------------------------------- #
# require_metadata_access (read authz gate)
# --------------------------------------------------------------------------- #


def test_a_gate_with_no_decider_fails_closed() -> None:
    # `None` is what the lifespan sets where it built no decider; with authorization on that is an outage, never an allow.
    request = _request(decider=None)
    with pytest.raises(ServiceUnavailableError, match="authorization service is not available"):
        asyncio.run(
            fga_deps.require_metadata_access("a$b", request=request, settings=_settings(**_FULL_AUTH), token=person(), names=NO_CATALOG_NAMES, bearer=None)
        )


def test_gate_unauthenticated_raises() -> None:
    with pytest.raises(UnauthenticatedError):
        asyncio.run(
            fga_deps.require_metadata_access(
                "a$b",
                request=_request(decider=OpenFgaDecider(cast("Any", object()))),
                settings=_settings(**_FULL_AUTH),
                token=None,
                names=NO_CATALOG_NAMES,
                bearer=None,
            )
        )


def test_gate_denies_without_permission(openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """alice is refused the table she holds nothing on, named by its name, and admitted once she reads the uuid the name
    resolved to as her: the decision is on that object, asked of the real model."""
    names = _Names()

    def gate(decider: OpenFgaDecider) -> Any:  # noqa: ANN401 — the gate's coroutine
        return fga_deps.require_metadata_access(
            "a$b", request=_request(decider=decider), settings=_settings(**_FULL_AUTH), token=person(), names=cast(CatalogNames, names), bearer="tok"
        )

    openfga_store.write((fga_ids.user_object("dex~bob"), "owner", _object("a$b")))
    with pytest.raises(PermissionDeniedError, match=r"can_get_metadata required on table:a\$b"):
        decided(openfga_store, gate)
    openfga_store.write((fga_ids.user_object("dex~alice"), "reader", _object("a$b")))
    decided(openfga_store, gate)
    assert names.bearers == ["tok", "tok"]


# --------------------------------------------------------------------------- #
# enforce_author (provenance forgery prevention)
# --------------------------------------------------------------------------- #


def _table(name: str) -> dict[str, Any]:
    """A governed table as the catalog names it on an event: its name, and its uuid in the ``rask_lance`` facet."""
    return {"namespace": "silver", "name": name, "facets": {"rask_lance": {"tableUuid": _uuid(name)}}}


def _event(claimed_author: str = "anon", outputs: list[str] | None = None, inputs: list[str] | None = None) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-06-24T00:00:00Z",
            "run": {"runId": "r1", "facets": {"rask_author": {"name": claimed_author}}},
            "job": {"namespace": "jobs", "name": "promote"},
            "inputs": [_table(n) for n in (inputs or [])],
            "outputs": [_table(n) for n in (outputs or [])],
        }
    )


# --------------------------------------------------------------------------- #
# DatasetFilter — transitive-disclosure filtering (audit w8u4rc2tg, security medium)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Handler-body behavior the route-dependency introspection can't see:
# the read handler must APPLY the filter, and ingest must bind the verified author.
# --------------------------------------------------------------------------- #


class _FakeWindow:
    """One read of `_FakeStore`'s canned feed, handing out its rows in the order they are canned."""

    def __init__(self, events: list[EventRecord], bounds: tuple[int, int] | None, log: list[tuple[str, int]]) -> None:
        self._events = events
        self._bounds = bounds
        self._log = log

    async def bounds(self) -> tuple[int, int] | None:
        return self._bounds

    async def take(self, n: int) -> list[EventRecord]:
        self._log.append(("read", n))
        read, self._events = self._events[:n], self._events[n:]
        return read


class _FakeStore:
    """The store surface the feed door and the ingest door reach: a canned feed, and what was recorded."""

    def __init__(self) -> None:
        self.recorded: RunEvent | None = None
        self.events: list[EventRecord] = []
        self.windows: list[dict[str, object]] = []
        #: Each read a window made and each judgment, in the order they happened.
        self.log: list[tuple[str, int]] = []
        self.oldest: int | None = None

    async def record(self, event: RunEvent, *, aliased: Collection[str]) -> None:
        self.recorded = event

    async def run_author(self, _run_id: str) -> str | None:
        return None

    async def window(self, *, after: int | None = None, since: int | None = None, summary: bool = False) -> _FakeWindow:
        self.windows.append({"after": after, "since": since, "summary": summary})
        bounds = (self.oldest, max((event.seq for event in self.events), default=self.oldest)) if self.oldest is not None else None
        return _FakeWindow(list(self.events), bounds, self.log)


def test_get_events_hides_dataset_less_events_when_governed(openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    # #22 audit: a dataset-less event (empty inputs+outputs) must NOT pass the gate vacuously when FGA is
    # on — it would otherwise leak the run/author/full event JSON to a caller, here one who reads a table.
    from lineage.api.v1.endpoints.events import get_events

    openfga_store.write((fga_ids.user_object("dex~alice"), "reader", _object("a")))
    settings = _settings(**_FULL_AUTH)
    repo = _FakeStore()
    repo.events = [EventRecord(seq=1, outputs=[], inputs=[], event={"run": {"runId": "secret"}})]

    def read(decider: OpenFgaDecider) -> Any:  # noqa: ANN401 — the door's coroutine
        flt = fga_deps.DatasetFilter(_request(decider=decider), settings, person())
        return get_events(provenance=cast(ProvenanceStore, repo), datasets=flt, settings=settings, names=NO_CATALOG_NAMES, bearer=None)

    result = decided(openfga_store, read)
    assert result.events == []  # dataset-less event is hidden under governance


@pytest.mark.parametrize(
    ("limit", "visible_at", "own_tables", "cursor", "log"),
    [
        pytest.param(
            8,
            {150, 100, 99, 98, 97, 96, 95, 94},
            True,
            94,
            # The first chunk is judged before anything more is read. The second (64 rows) is judged on a full batch of
            # objects while the last chunk's first 64 rows are read, and the rest of the window after the verdict; the
            # window holds 128 rows more, and they fill the page.
            [
                ("read", 8),
                ("judging", 8),
                ("judged", 8),
                ("read", 64),
                ("judging", 64),
                ("read", 64),
                ("judged", 64),
                ("read", 1864),
                ("judging", 121),
                ("judged", 121),
            ],
            id="judged-on-full-batches-reads-ahead",
        ),
        pytest.param(
            2,
            {190, 189},
            False,
            189,
            # The second chunk is judged on one undecided object, too little to hide a read, and its verdict fills the page.
            [("read", 2), ("judging", 1), ("judged", 1), ("read", 16), ("judging", 1), ("judged", 1)],
            id="judged-on-one-object-reads-nothing-ahead",
        ),
        pytest.param(
            60,
            set(),
            True,
            None,
            # The first chunk is judged on a full batch of objects, and nothing is read beside it: its verdict fills most
            # pages. The second read empties the window, so nothing is read beside its judging either.
            [("read", 60), ("judging", 60), ("judged", 60), ("read", 480), ("judging", 140), ("judged", 140)],
            id="the-first-chunk-reads-nothing-ahead",
        ),
        pytest.param(
            50,
            {190, 180, 170, 160},
            False,
            None,
            # Four of the first fifty rows are visible: the page still needs 46, some 575 rows at that share, and reads
            # four times that, which is the rest of its window.
            [("read", 50), ("judging", 2), ("judged", 2), ("read", 1950)],
            id="a-sparse-first-chunk-reads-the-rest-second",
        ),
        pytest.param(
            1,
            set(),
            False,
            None,
            # A page that finds no visible row reads its window in three reads: one row, eight more, then the rest.
            [("read", 1), ("judging", 1), ("judged", 1), ("read", 8), ("read", 1991)],
            id="all-hidden-reads-the-window-in-three",
        ),
    ],
)
def test_a_sparse_page_reads_its_window_in_three_reads_at_most_the_next_beside_a_long_judging(
    monkeypatch: pytest.MonkeyPatch, limit: int, visible_at: set[int], own_tables: bool, cursor: int | None, log: list[tuple[str, int]]
) -> None:
    """A governed page reads its window in at most three reads; from the second on, a chunk judged on a full OpenFGA
    batch of objects has the next chunk's first part, at most its own size, read beside its judging ([[LH-590]])."""
    from lineage.api.v1.endpoints.events import get_events

    settings = _settings(**_FULL_AUTH)
    repo = _FakeStore()

    async def judged_after_a_yield(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        repo.log.append(("judging", len(objects)))
        await asyncio.sleep(0)
        repo.log.append(("judged", len(objects)))
        return {o: o == _object("a") for o in objects}

    def table(seq: int) -> str:
        """The table a row names: `a` where the caller may see it, else a table of its own or one shared hidden table."""
        if seq in visible_at:
            return "a"
        return f"hidden-{seq}" if own_tables else "b"

    monkeypatch.setattr(fga_reads, "batch_check", judged_after_a_yield)
    repo.events = [EventRecord(seq=seq, outputs=["t"], inputs=[], table_uuids=[_uuid(table(seq))], event={}) for seq in range(200, 0, -1)]
    flt = fga_deps.DatasetFilter(_request(decider=OpenFgaDecider(cast(OpenFgaClient, object()))), settings, person())

    page = asyncio.run(get_events(provenance=cast(ProvenanceStore, repo), datasets=flt, settings=settings, limit=limit, names=NO_CATALOG_NAMES, bearer=None))

    assert ([e.seq for e in page.events], page.next_cursor) == (sorted(visible_at, reverse=True), cursor)
    assert repo.log == log


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
    page = asyncio.run(get_events(provenance=cast(ProvenanceStore, repo), datasets=flt, settings=settings, limit=2, names=NO_CATALOG_NAMES, bearer=None))
    assert repo.windows[-1] == {"after": None, "since": None, "summary": False}
    assert [e.seq for e in page.events] == [5, 4] and page.next_cursor == 4  # full page → continue

    repo.events = [EventRecord(seq=3, outputs=["a"], inputs=[], event={})]
    page = asyncio.run(
        get_events(provenance=cast(ProvenanceStore, repo), datasets=flt, settings=settings, after=4, limit=2, names=NO_CATALOG_NAMES, bearer=None)
    )
    assert repo.windows[-1] == {"after": 4, "since": None, "summary": False}
    assert [e.seq for e in page.events] == [3] and page.next_cursor is None  # short page → exhausted

    # summary rides through to the store's read untouched
    asyncio.run(get_events(provenance=cast(ProvenanceStore, repo), datasets=flt, settings=settings, summary=True, names=NO_CATALOG_NAMES, bearer=None))
    assert repo.windows[-1]["summary"] is True


def test_ingest_handler_binds_verified_author() -> None:
    from lineage.api.v1.endpoints.ingest import ingest_event

    repo = _FakeStore()
    event = _event(claimed_author="attacker")
    asyncio.run(
        ingest_event(
            body=event.model_dump(by_alias=True),
            request=_request(),
            provenance=cast(ProvenanceStore, repo),
            settings=_settings(),
            token=person("real-user"),
            names=NO_CATALOG_NAMES,
            bearer=None,
            decider=None,
        )
    )
    assert repo.recorded is not None and repo.recorded.author == "dex~real-user"  # body claim overridden


def test_ingest_handler_keeps_body_author_when_oidc_off() -> None:
    from lineage.api.v1.endpoints.ingest import ingest_event

    repo = _FakeStore()
    event = _event(claimed_author="claimed")
    asyncio.run(
        ingest_event(
            body=event.model_dump(by_alias=True),
            request=_request(),
            provenance=cast(ProvenanceStore, repo),
            settings=_settings(),
            token=None,
            names=NO_CATALOG_NAMES,
            bearer=None,
            decider=None,
        )
    )
    assert repo.recorded is not None and repo.recorded.author == "claimed"


# --------------------------------------------------------------------------- #
# #2 — output-scoped ingest authz: a producer may only record provenance for
# outputs it can WRITE (can_write_data), not just that it's authenticated.
# --------------------------------------------------------------------------- #


def test_output_authz_with_no_decider_fails_closed() -> None:
    with pytest.raises(ServiceUnavailableError, match="authorization service is not available"):
        asyncio.run(fga_deps.enforce_output_authz(_event(outputs=["a$b"]), _request(decider=None).app, _settings(**_FULL_AUTH), token=person()))


def test_output_authz_output_less_still_requires_auth() -> None:
    # REGRESSION (bug hunt 2026-07-13): an event with NO outputs must STILL require a valid principal.
    # The pre-fix code short-circuited (`if not outputs: return`) BEFORE the auth checks, so an
    # unauthenticated caller could record a forged inputs-only run in the governed feed.
    with pytest.raises(UnauthenticatedError):
        asyncio.run(
            fga_deps.enforce_output_authz(
                _event(inputs=["a$b"]), _request(decider=OpenFgaDecider(cast("Any", object()))).app, _settings(**_FULL_AUTH), token=None
            )
        )


# --- A service principal on the HTTP ingest ----------------------------------------------------------
# An in-cluster producer with no bus user (a user's pipeline job on its own compute, ADR 0155) POSTs the HTTP ingest with
# its pod's projected token, which the door verifies into a `ServicePrincipal` (`lakehouse.governed.machine_identity`).
# The door itself is driven over HTTP in `services/lineage/tests/test_a_claimed_service_name_authenticates_nobody.py`;
# this pins what the ingest gates do with the principal it hands them.


_SERVICE_ACCOUNT = "system:serviceaccount:default:rask-sa-pipeline"
#: The OpenFGA subject the service principal above is.
_SERVICE = f"kubernetes~{_SERVICE_ACCOUNT}"


def _service_run() -> RunEvent:
    """A run the service principal posts, writing `gold$scores`."""
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-13T09:00:00+00:00",
            "producer": "https://github.com/Borg93/lance-ns",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {}},
            "job": {"namespace": "pipelines", "name": "score.daily"},
            "inputs": [],
            "outputs": [{"namespace": "gold", "name": "gold$scores", "facets": {"rask_lance": {"tableUuid": _uuid("gold$scores")}}}],
        }
    )


@pytest.mark.parametrize(
    ("held", "outcome"),
    [
        pytest.param({(_SERVICE, "writer", _object("gold$scores"))}, nullcontext(), id="the-service-writes-the-output"),
        pytest.param(set(), pytest.raises(PermissionDeniedError), id="the-service-holds-nothing-on-the-output"),
    ],
)
def test_enforce_output_authz_bounds_a_service_principal_by_its_own_rung(
    held: set[tuple[str, str, str]],
    outcome: AbstractContextManager[object],
    openfga_store: Any,  # noqa: ANN401 — the root conftest's OpenFgaStore
) -> None:
    """A service records provenance only for what its rung (`writer` on the output's namespace) permits.

    The bus route authorizes the subject the event's signature binds (`enforce_bus_authz`); this door authorizes the
    principal its bearer verified, so the decision is the service's own `can_write_data` on the output and never a
    person's. bob owns the output, so it is governed and a refusal is the service's own.
    """
    openfga_store.write((fga_ids.user_object("dex~bob"), "owner", _object("gold$scores")), *((fga_ids.user_object(u), r, o) for u, r, o in held))

    def authorize(decider: OpenFgaDecider) -> Any:  # noqa: ANN401 — the gate's coroutine
        # The feed lineage's lifespan always wires, here one holding no run, so the event founds its run.
        app = _request(decider=decider, provenance=_FakeStore()).app
        return fga_deps.enforce_output_authz(_service_run(), app, _settings(**_FULL_AUTH), token=security.ServicePrincipal.for_account(_SERVICE_ACCOUNT))

    with outcome:
        decided(openfga_store, authorize)


_SECRET = {"namespace": "bronze", "name": "bronze$secret", "field": "col"}


def _column_lineage(*entries: dict[str, str]) -> dict[str, object]:
    return {"fields": {"col": {"inputFields": list(entries)}}}


@pytest.mark.parametrize(
    ("output_slots", "input_slots", "unresolved"),
    [
        pytest.param({"facets": _column_lineage(_SECRET)}, {}, {"bronze$secret"}, id="an-upstream-the-door-checked"),
        pytest.param({"facets": _column_lineage(), "outputFacets": _column_lineage(_SECRET)}, {}, {"bronze$secret"}, id="a-second-copy-in-another-slot"),
        pytest.param({"facets": {"fields": {}, "dataset": [_SECRET]}}, {}, {"bronze$secret"}, id="the-whole-dataset-list"),
        pytest.param({}, {"facets": _column_lineage(_SECRET)}, {"bronze$secret"}, id="an-inputs-own-facet"),
        pytest.param(
            {"facets": _column_lineage({"namespace": "bronze", "name": "bronze$secret"})}, {}, {"gold$t", "gold$in"}, id="a-stored-entry-the-door-refuses"
        ),
        pytest.param({"facets": {"_deleted": True}}, {}, {"gold$t", "gold$in"}, id="a-deleted-facet-the-spec-still-requires-fields-of"),
    ],
)
def test_a_feed_row_is_governed_on_the_column_upstreams_its_writer_was_checked_against(
    output_slots: dict[str, dict[str, object]], input_slots: dict[str, dict[str, object]], unresolved: set[str]
) -> None:
    """A columnLineage facet, on any dataset and in any slot, names source tables beyond the inputs, so a caller who can
    see the row must also see each upstream; a stored facet the ingest door's reading refuses names upstreams no check was
    asked about, so the row is hidden. A summary row (no payload) is governed on what it recorded alone."""
    from lineage.api.v1.endpoints.events import _governed_datasets

    def dataset(name: str, slots: dict[str, dict[str, object]]) -> dict[str, Any]:
        payload = with_dataset_uuids({"namespace": "gold", "name": name}, DatasetUuids(table_uuid=_uuid(name)), producer="rask://test")
        for slot, facet in slots.items():
            payload.setdefault(slot, {})["columnLineage"] = facet
        return payload

    event = {"inputs": [dataset("gold$in", input_slots)], "outputs": [dataset("gold$t", output_slots)]}
    uuids = [_uuid("gold$in"), _uuid("gold$t")]
    governed_on = _governed_datasets(EventRecord(seq=1, inputs=["gold$in"], outputs=["gold$t"], table_uuids=uuids, event=event))
    summary = _governed_datasets(EventRecord(seq=1, inputs=["gold$in"], outputs=["gold$t"], table_uuids=uuids, event={}))

    assert (governed_on.unresolved, summary) == (unresolved, fga_deps.Governed(objects=frozenset(fga_ids.table_object(uuid) for uuid in uuids)))


def test_get_events_reports_the_feed_floor_so_a_consumer_can_see_what_it_lost() -> None:
    """`oldest_seq` — the oldest row the feed still holds.

    Lineage prunes its feed on the relay tick, dropping the rows it received longest ago. That
    prune cannot consult any consumer's cursor: a reader's mark lives in ITS own store, which lineage
    is not scoped to and must never be — reaching across would couple the producer of the feed to one
    of its readers.

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

    page = asyncio.run(get_events(provenance=cast(ProvenanceStore, repo), datasets=flt, settings=settings, limit=2, names=NO_CATALOG_NAMES, bearer=None))

    assert page.oldest_seq == 8999
