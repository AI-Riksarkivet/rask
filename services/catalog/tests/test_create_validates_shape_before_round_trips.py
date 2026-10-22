"""catalog-api-19 — ``POST /v1/table/{id}/create`` refuses a malformed request before it dials out.

The handler's first round trip is a parent-existence describe against the namespace backend. Every FREE
check — the multi-base allowlist, the ``properties`` JSON parse, the LANCE-ONLY format guard — runs before
it, so the most common way to get a create wrong (a typo'd ``data_base``, unparseable ``properties``) costs
no network round trip before the server says what is actually wrong, and under an outage of the backend a
request that is invalid on its face answers 400, not 503/404.

Driven through the real handler with the round trip replaced by a recorder, so the assertion is "it was
never dialled", not a reading of the source.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError, LanceNamespace

from catalog.api import fga_deps
from catalog.api.v1.endpoints import data as ep
from catalog.core.config import Settings
from service_kit.lancekit.arrow_ipc import encode_arrow_stream


def _settings() -> Settings:
    return Settings.model_validate({"s3_access_key_id": "x", "s3_secret_access_key": "x", "multibase_data_bases": "s3://approved"})


@pytest.fixture
def no_round_trips(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the dial-out with a recorder — a create that reaches it for a request this malformed has
    already lost the point of the ordering."""
    dialled: list[str] = []

    async def _parent(*_a: Any, **_kw: Any) -> None:
        dialled.append("require_parent_exists")

    monkeypatch.setattr(fga_deps, "require_parent_exists", _parent)
    return dialled


def _create(**over: Any) -> Any:
    kwargs: dict[str, Any] = {
        "id": "db1$t",
        "ns": cast("LanceNamespace", object()),
        "root": "s3://root",
        "settings": _settings(),
        "token": None,
        "client": None,
        "emitter": None,
        "uuids": None,
        "control": None,
        "so": {},
        "data": encode_arrow_stream(pa.table({"id": [1]})),
        "run": None,
        # Every refusal here lands before a name is resolved, so the resolver is never asked.
        "resolver": None,
    }
    kwargs.update(over)
    return asyncio.run(ep.create_table(**kwargs))


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


def test_a_body_that_is_not_an_arrow_stream_is_refused_without_dialling_out(no_round_trips: list[str]) -> None:
    with pytest.raises(InvalidInputError, match="not a valid Arrow IPC stream"):
        _create(data=b"this is not an arrow ipc stream")
    assert not no_round_trips, f"dialled {no_round_trips} before reading the request's own body"
