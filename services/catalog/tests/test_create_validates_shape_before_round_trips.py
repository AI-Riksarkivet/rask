"""catalog-api-19 — ``POST /v1/table/{id}/create`` refuses a malformed request before it dials out.

The handler's first two statements were both round trips: a parent-existence describe against the
namespace backend and a trash-registry read on the object store. Every FREE check — the multi-base
allowlist, the ``properties`` JSON parse, the LANCE-ONLY format guard — ran after them. So the most
common way to get a create wrong (a typo'd ``data_base``, unparseable ``properties``) cost two network
round trips before the server said what was actually wrong, and under an outage of either dependency
a request that is invalid on its face answered 503/404 instead of 400.

Nothing about WHICH refusals exist changes; only the order. Driven through the real handler with the
two round trips replaced by recorders, so the assertion is "they were never dialled", not a reading of
the source.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError, LanceNamespace

from catalog.api import fga_deps
from catalog.api.v1.endpoints import data as ep
from catalog.core.config import Settings


def _settings() -> Settings:
    return Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x", "multibase_data_bases": "s3://approved"})


@pytest.fixture
def no_round_trips(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the two dial-outs with recorders that also FAIL — a create that reaches either of them
    for a request this malformed has already lost the point of the ordering."""
    dialled: list[str] = []

    async def _parent(*_a: Any, **_kw: Any) -> None:
        dialled.append("require_parent_exists")

    async def _trash(*_a: Any, **_kw: Any) -> None:
        dialled.append("require_no_live_trash")

    monkeypatch.setattr(fga_deps, "require_parent_exists", _parent)
    monkeypatch.setattr(fga_deps, "require_no_live_trash", _trash)
    return dialled


def _create(**over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "id": "db1$t",
        "ns": cast("LanceNamespace", object()),
        "settings": _settings(),
        "token": None,
        "client": None,
        "emitter": None,
        "control": None,
        "so": {},
        "data": b"",
    }
    kwargs.update(over)
    return asyncio.run(ep.create_table(**kwargs))


def test_the_drive_reaches_the_round_trips_for_a_WELL_FORMED_request(no_round_trips: list[str]) -> None:
    """Guards the gate: if the handler never called either dependency, the assertions below would pass
    for the wrong reason. A request with nothing wrong on its face must still reach both."""
    with pytest.raises(Exception):  # noqa: B017 — it fails later, at the write; the guards ran first
        _create()
    assert no_round_trips == ["require_parent_exists", "require_no_live_trash"], no_round_trips


def test_an_off_allowlist_data_base_is_refused_without_dialling_out(no_round_trips: list[str]) -> None:
    with pytest.raises(InvalidInputError, match="allowlist"):
        _create(data_base=["s3://rogue"])
    assert not no_round_trips, f"dialled {no_round_trips} before checking a free, purely-local allowlist"


def test_unparseable_properties_are_refused_without_dialling_out(no_round_trips: list[str]) -> None:
    with pytest.raises(InvalidInputError, match="not valid JSON"):
        _create(properties="{not json")
    assert not no_round_trips, f"dialled {no_round_trips} before parsing the request's own properties"


def test_a_non_lance_format_is_refused_without_dialling_out(no_round_trips: list[str]) -> None:
    with pytest.raises(InvalidInputError):
        _create(properties='{"write.format.default": "parquet"}')
    assert not no_round_trips, f"dialled {no_round_trips} before the LANCE-ONLY format guard"
