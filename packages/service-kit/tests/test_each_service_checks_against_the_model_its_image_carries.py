"""Each service checks against the authorization model its own image carries, and no image rewrites anyone else's.

[[LH-201]]. Every service resolves the store at boot, and the live estate pins nothing (measured
2026-09-27: no `RASK_FGA_STORE_ID` or `RASK_FGA_MODEL_ID` on any deployment, 1,353 model versions in the
one `lance-catalog` store). `resolve` took the store's NEWEST model, so a pod checked against whichever
model was written last rather than the one its image was built with; and the store's newest is written by
the catalog's boot, by the chart's hook and by older images that still provision their own model.

The rule now: the model an image carries is found in the store's history by its canonical body, and that
model's id is what the image checks against. The newest decides nothing, so a body held anywhere in the
history is not written again, and only a body the store has never held is written. `resolve` never writes;
it waits, up to a deadline, for the store and the model to exist, re-reading one page per poll.
"""

from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from lance_namespace import ServiceUnavailableError

from service_kit.governed import fga
from service_kit.governed.auth import write_model
from service_kit.governed.fga import ModelHistoryTooLongError


def _model(can_read_data: dict[str, Any], *, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    """A `warehouse` whose derived permission is a parameter: two models can differ in a rule body alone,
    under identical relation names, so the narrowing guard has nothing to refuse."""
    direct = ("reader", "pass_grants", *extra)
    return {
        "schema_version": "1.1",
        "type_definitions": [
            {"type": "user"},
            {
                "type": "warehouse",
                "relations": {**{name: {"this": {}} for name in direct}, "can_read_data": can_read_data},
                "metadata": {"relations": {name: {"directly_related_user_types": [{"type": "user"}]} for name in direct}},
            },
        ],
    }


_READER = {"computedUserset": {"relation": "reader"}}
_READER_OR_PASS = {"union": {"child": [_READER, {"computedUserset": {"relation": "pass_grants"}}]}}
#: The image's model: `can_read_data` is `reader` alone.
OLDER = _model(_READER)
#: A later model: `can_read_data` widened to `reader or pass_grants`.
NEWER = _model(_READER_OR_PASS)


def _stored(model_id: str, model: dict[str, Any]) -> dict[str, Any]:
    return {"id": model_id, **copy.deepcopy(model)}


# --------------------------------------------------------------------------- the SDK side: resolve, provision


class _OpenFga:
    """One OpenFGA as the SDK sees it, one model per page, recording what each read touched.

    ``listings`` replays one store listing per `list_stores` call (the last repeats); an exception in it is
    raised instead. ``histories`` replays one history per history read in the same way.
    """

    def __init__(self, listings: list[Any], histories: list[list[dict[str, Any]]], *, history_failures: int = 0) -> None:
        self.listings = listings
        self.histories = histories
        self.history_failures = history_failures
        self.written: list[tuple[str, Any]] = []
        self.pages_per_read: list[int] = []
        self.listed = 0

    def next_listing(self) -> Any:
        self.listed += 1
        return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]

    def next_history(self) -> list[dict[str, Any]]:
        return self.histories.pop(0) if len(self.histories) > 1 else self.histories[0]


def _client_class(fake: _OpenFga) -> type:
    class _Client:
        def __init__(self, configuration: Any) -> None:
            self._store_id = getattr(configuration, "store_id", None)
            self._pages: list[dict[str, Any]] = []

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def list_stores(self) -> Any:
            listing = fake.next_listing()
            if isinstance(listing, Exception):
                raise listing
            return SimpleNamespace(stores=[SimpleNamespace(**store) for store in listing])

        async def create_store(self, request: Any) -> Any:
            raise AssertionError("the store exists; nothing here may create one")

        async def read_authorization_models(self, options: dict[str, Any] | None = None) -> Any:
            token = int((options or {}).get("continuation_token") or 0)
            if token == 0 and fake.history_failures:
                fake.history_failures -= 1
                raise aiohttp.ClientConnectionError("connection refused")
            if token == 0:
                self._pages = fake.next_history()
                fake.pages_per_read.append(0)
            fake.pages_per_read[-1] += 1
            page = [SimpleNamespace(**m) for m in self._pages[token : token + 1]]
            more = token + 1 < len(self._pages)
            return SimpleNamespace(authorization_models=page, continuation_token=str(token + 1) if more else "")

        async def write_authorization_model(self, request: Any) -> Any:
            fake.written.append((str(self._store_id), request))
            return SimpleNamespace(authorization_model_id="model-WRITTEN")

    return _Client


_ESTATE = {"id": "store-ESTATE", "name": "lance-catalog", "created_at": "2026-07-29T08:40:19Z"}
_SCRATCH = {"id": "store-SCRATCH", "name": "fga-probe", "created_at": "2026-09-27T20:00:00Z"}


def _install(monkeypatch: pytest.MonkeyPatch, fake: _OpenFga, image_model: dict[str, Any]) -> None:
    monkeypatch.setattr(fga, "OpenFgaClient", _client_class(fake))
    monkeypatch.setattr(fga, "load_model", lambda: copy.deepcopy(image_model))


@pytest.mark.asyncio
async def test_a_service_checks_against_the_model_its_image_carries_not_the_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OpenFga([[_ESTATE]], [[_stored("model-NEWER", NEWER), _stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, OLDER)

    assert await fga.resolve("http://fga:8080", deadline_seconds=0.0) == ("store-ESTATE", "model-OLDER")


@pytest.mark.asyncio
async def test_a_wait_for_an_absent_model_rereads_one_page_per_poll_then_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first read scans the whole history; a new model can only be prepended, so a later read stops at
    the newest one already scanned. A full re-scan of the live store's 1,353 models measured ~16 s a read,
    which would put a two-minute wait past the fleet's 300 s startup budget."""
    history = [_stored("model-C", NEWER), _stored("model-B", NEWER), _stored("model-A", NEWER)]
    fake = _OpenFga([[_ESTATE]], [history])
    _install(monkeypatch, fake, OLDER)

    assert await fga.resolve("http://fga:8080", deadline_seconds=0.2, poll_seconds=0.02) is None
    assert fake.pages_per_read[0] == 3, "the first read did not scan the whole history"
    assert len(fake.pages_per_read) > 2, "the wait did not re-read"
    assert set(fake.pages_per_read[1:]) == {1}, f"a re-read scanned past the model it had already seen: {fake.pages_per_read}"


@pytest.mark.asyncio
async def test_a_model_written_while_the_service_waits_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OpenFga([[_ESTATE]], [[], [_stored("model-OLDER", OLDER)], [_stored("model-NEWER", NEWER), _stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, NEWER)

    assert await fga.resolve("http://fga:8080", deadline_seconds=5.0, poll_seconds=0.0) == ("store-ESTATE", "model-NEWER")


@pytest.mark.asyncio
async def test_a_store_that_appears_while_the_service_waits_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first install: the catalog creates the store at boot, and a service booting beside it must not
    answer 503 for its whole life because it looked a moment too early."""
    fake = _OpenFga([[], [_SCRATCH], [_SCRATCH, _ESTATE]], [[_stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, OLDER)

    assert await fga.resolve("http://fga:8080", deadline_seconds=5.0, poll_seconds=0.0) == ("store-ESTATE", "model-OLDER")


@pytest.mark.asyncio
async def test_an_openfga_that_is_not_answering_yet_is_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OpenFga([aiohttp.ClientConnectionError("connection refused"), [_ESTATE]], [[_stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, OLDER)

    assert await fga.resolve("http://fga:8080", deadline_seconds=5.0, poll_seconds=0.0) == ("store-ESTATE", "model-OLDER")


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [None, "store-ESTATE"], ids=["by-name", "pinned"])
async def test_a_history_read_that_fails_at_first_is_waited_out(monkeypatch: pytest.MonkeyPatch, pinned: str | None) -> None:
    """With a pinned store the history read is resolve's first call to OpenFGA, and unpinned it can fail
    after the store listing answered; either way an OpenFGA that is not answering yet is waited for."""
    fake = _OpenFga([[_ESTATE]], [[_stored("model-OLDER", OLDER)]], history_failures=2)
    _install(monkeypatch, fake, OLDER)

    assert await fga.resolve("http://fga:8080", store_id=pinned, deadline_seconds=5.0, poll_seconds=0.0) == ("store-ESTATE", "model-OLDER")


@pytest.mark.asyncio
async def test_a_pinned_store_is_used_without_a_name_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook and `bootstrap-admin` honour `RASK_FGA_STORE_ID`, so every reader must, or the model is
    written to one store and looked for in another."""
    fake = _OpenFga([[_SCRATCH]], [[_stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, OLDER)

    assert await fga.resolve("http://fga:8080", store_id="store-PINNED", deadline_seconds=0.0) == ("store-PINNED", "model-OLDER")
    assert await fga.provision("http://fga:8080", store_id="store-PINNED") == ("store-PINNED", "model-OLDER")
    assert fake.listed == 0, "a pinned store was looked up by name"


@pytest.mark.asyncio
async def test_a_body_the_store_holds_below_its_newest_is_not_written_again(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A body-only difference passes the narrowing guard, so an older catalog image rewrote the newest."""
    fake = _OpenFga([[_ESTATE]], [[_stored("model-NEWER", NEWER), _stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, OLDER)

    with caplog.at_level(logging.INFO, logger=fga.log.name):
        assert await fga.provision("http://fga:8080") == ("store-ESTATE", "model-OLDER")

    assert fake.written == [], "a held body was written again"
    assert any(record.getMessage() == "openfga_model_held_below_newest" for record in caplog.records)


@pytest.mark.asyncio
async def test_a_body_that_removes_what_the_newest_defines_is_still_written_and_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog checks against the model its own image carries, so a body the store has never held is
    written and used even when it lacks a relation the newest defines: it governs only the pods built with
    it, and answering with the newest's id would put the catalog on rules its code does not carry."""
    wide = _model(_READER, extra=("maintainer",))
    fake = _OpenFga([[_ESTATE]], [[_stored("model-WIDE", wide), _stored("model-OLDER", OLDER)]])
    _install(monkeypatch, fake, NEWER)

    assert await fga.provision("http://fga:8080") == ("store-ESTATE", "model-WRITTEN")
    assert [store for store, _request in fake.written] == ["store-ESTATE"]


@pytest.mark.asyncio
async def test_a_store_holding_no_model_yet_gets_this_images(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _OpenFga([[_ESTATE]], [[]])
    _install(monkeypatch, fake, OLDER)

    assert await fga.provision("http://fga:8080") == ("store-ESTATE", "model-WRITTEN")


@pytest.mark.asyncio
async def test_a_store_whose_history_cannot_be_read_is_not_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unread history is not an absent model: the catalog, which is fatal, does not boot on a guess."""
    fake = _OpenFga([[_ESTATE]], [[_stored("model-OLDER", OLDER)]], history_failures=5)
    _install(monkeypatch, fake, OLDER)

    with pytest.raises(ServiceUnavailableError):
        await fga.provision("http://fga:8080", retry_attempts=1)
    assert fake.written == []


@pytest.mark.asyncio
async def test_a_history_past_the_page_bound_is_refused_not_read_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent is what makes a writer write, so a history too long to read must not answer absent."""
    monkeypatch.setattr(fga, "MODEL_MAX_PAGES", 2)
    fake = _OpenFga([[_ESTATE]], [[_stored(f"model-{n}", NEWER) for n in range(3)]])
    _install(monkeypatch, fake, OLDER)

    with pytest.raises(ModelHistoryTooLongError):
        await fga.provision("http://fga:8080", retry_attempts=1)
    assert fake.written == []


# --------------------------------------------------------------------------- the chart's hook


class _Http:
    """OpenFGA's HTTP API as `write_model._call` reaches it, one model per page."""

    def __init__(self, stores: list[dict[str, Any]], histories: dict[str, list[dict[str, Any]]], *, unreachable: bool = False) -> None:
        self.stores = stores
        self.histories = histories
        self.unreachable = unreachable
        self.written: list[str] = []

    def __call__(self, api: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        del api, timeout
        if self.unreachable:
            raise OSError("connection refused")
        if path == "/stores":
            return {"stores": self.stores}
        store_id = path.split("/")[2]
        if body is not None:
            self.written.append(store_id)
            return {"authorization_model_id": "model-WRITTEN"}
        query = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&")) if "?" in path else {}
        token = int(query.get("continuation_token") or 0)
        models = self.histories[store_id]
        more = token + 1 < len(models)
        return {"authorization_models": models[token : token + 1], "continuation_token": str(token + 1) if more else ""}


def _run_hook(monkeypatch: pytest.MonkeyPatch, http: _Http, image_model: dict[str, Any], *, pinned: str | None = None) -> int:
    monkeypatch.setenv("FGA_API_URL", "http://fga:8080")
    if pinned is None:
        monkeypatch.delenv("RASK_FGA_STORE_ID", raising=False)
    else:
        monkeypatch.setenv("RASK_FGA_STORE_ID", pinned)
    monkeypatch.setattr(write_model, "_call", http)
    monkeypatch.setattr(write_model, "model_document", lambda: json.loads(json.dumps(image_model)))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return write_model.main()


def test_the_hook_writes_into_the_store_the_estate_uses_not_the_first_one_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _Http([_SCRATCH, _ESTATE], {"store-SCRATCH": [], "store-ESTATE": [_stored("model-OLDER", OLDER)]})

    assert _run_hook(monkeypatch, http, NEWER) == 0
    assert http.written == ["store-ESTATE"]


def test_the_hook_writes_into_a_pinned_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every service honours `RASK_FGA_STORE_ID`, so the hook must write the model into that store and not
    into a newer `lance-catalog` beside it."""
    newer = {"id": "store-NEWER", "name": "lance-catalog", "created_at": "2026-09-28T00:00:00Z"}
    http = _Http([_ESTATE, newer], {"store-ESTATE": [_stored("model-OLDER", OLDER)], "store-NEWER": []})

    assert _run_hook(monkeypatch, http, NEWER, pinned="store-ESTATE") == 0
    assert http.written == ["store-ESTATE"]


def test_the_hook_does_not_rewrite_a_body_the_store_holds_below_its_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _Http([_ESTATE], {"store-ESTATE": [_stored("model-NEWER", NEWER), _stored("model-OLDER", OLDER)]})

    assert _run_hook(monkeypatch, http, OLDER) == 0
    assert http.written == [], "the hook rewrote a body the store already holds"


@pytest.mark.parametrize(("stores", "unreachable"), [([], False), ([_ESTATE], True)], ids=["no-store-yet", "openfga-unreachable"])
def test_nothing_to_pre_write_does_not_fail_the_upgrade(monkeypatch: pytest.MonkeyPatch, stores: list[dict[str, Any]], unreachable: bool) -> None:
    """Pre-upgrade, a failed hook refuses the upgrade before any manifest applies, including one that
    repairs OpenFGA; the catalog writes its image's model when it boots, so there is nothing to protect."""
    http = _Http(stores, {"store-ESTATE": []}, unreachable=unreachable)

    assert _run_hook(monkeypatch, http, NEWER) == 0
    assert http.written == []


def test_the_hooks_history_past_the_page_bound_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(write_model, "MODEL_MAX_PAGES", 2)
    http = _Http([_ESTATE], {"store-ESTATE": [_stored(f"model-{n}", NEWER) for n in range(3)]})
    monkeypatch.setattr(write_model, "_call", http)

    with pytest.raises(ModelHistoryTooLongError):
        write_model.history("http://fga:8080", "store-ESTATE", OLDER)
