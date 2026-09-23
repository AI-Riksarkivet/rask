"""A produce whose seed fails must not leave a catalog record pointing at bytes that never arrived.

GOVERNANCE PRECEDES THE FIRST ROW, and that ordering is deliberate: `produce` registers
`bronze$events` BEFORE `seed_bronze` runs, because the cascade head was the one tier the catalog had
never heard of — no `table:` object, so `policy/set` answered 404, no protection record was reachable
and no FGA grant could name it. Registering first closes that.

WHAT IT OPENS IS THE OTHER HALF, AND NOTHING CLOSED IT. `seed_bronze` is not guarded, so a write that
raises — an object-store outage, a full disk, a refused prefix — propagates out of `produce` and
leaves the registration behind. The record then governs a location that holds nothing: a policy set on
it polices no bytes, a protection record guards nothing, and an FGA grant keys off a table whose data
is not there.

MEASURED ON THE LIVE ESTATE 2026-09-23, and this is that state: the drift report's `absent_datasets`
names `bronze$events -> s3://lance-catalog/medallion/bronze`, a catalog record whose path holds no
bytes, while the eight prefixes that DO hold bronze data sit in tenant warehouses the record does not
name. The detector that found it ([[LH-192]]) is the safety net, not the fix.

THE UNWIND IS BEST-EFFORT AND LOUD. If the deregister also fails the record is genuinely orphaned, so
the caller is told which of the two happened and the drift report keeps reporting it — a silent
swallow here would hide exactly the state this whole pair exists to prevent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import produce as produce_module


class _Recorder:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.deregistered: list[str] = []


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    def _register(**kw: Any) -> None:
        rec.registered.append(str(kw["table_id"]))

    def _deregister(**kw: Any) -> None:
        rec.deregistered.append(str(kw["table_id"]))

    monkeypatch.setattr(produce_module.catalog_register, "register_written_dataset", _register)
    monkeypatch.setattr(produce_module.catalog_register, "deregister_dataset", _deregister, raising=False)

    def _seed(*_a: Any, **_k: Any) -> Any:
        raise OSError("object store refused the write")

    monkeypatch.setattr(produce_module, "seed_bronze", _seed)
    return rec


def _settings() -> MedallionSettings:
    """The REAL settings object, not a stand-in: a narrow double cannot see a field the door starts
    reading, and `app_api_token` was exactly that field."""
    return MedallionSettings.model_validate(
        {
            "MEDALLION_BRONZE_URI": "s3://lance-catalog/medallion/bronze",
            "MEDALLION_BRONZE_NAMESPACE": "bronze",
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_CATALOG_URL": "http://catalog.test",
        }
    )


def test_a_failed_seed_takes_its_registration_with_it(wired: _Recorder) -> None:
    """The record must not survive the write it was created to govern."""
    result = asyncio.run(produce_module.produce(cast(Any, None), _settings(), token="t1"))
    assert wired.registered == ["bronze$events"], wired.registered
    assert wired.deregistered == ["bronze$events"], (
        "the seed failed after the register and the record was left behind — exactly the `absent_datasets` state measured on the live estate"
    )
    assert result.get("status") == "seed_failed", result


def test_a_SUCCESSFUL_seed_keeps_its_registration(monkeypatch: pytest.MonkeyPatch, wired: _Recorder) -> None:
    """The control. Without it every assertion above passes on a producer that deregisters always."""
    monkeypatch.setattr(
        produce_module,
        "seed_bronze",
        lambda *_a, **_k: SimpleNamespace(version=1, row_count=10, size_bytes=99, fields=[]),
    )
    asyncio.run(produce_module.produce(cast(Any, None), _settings(), token="t2"))
    assert wired.registered == ["bronze$events"]
    assert wired.deregistered == [], "a successful write must keep its record"
