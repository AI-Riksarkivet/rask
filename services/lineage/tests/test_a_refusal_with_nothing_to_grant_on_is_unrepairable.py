"""A refusal naming outputs that carry no tuple raises the UNREPAIRABLE type ([[LH-166]]).

The consumer's ack is chosen by the EXCEPTION TYPE, so this is where the two refusals are separated —
`test_an_unrepairable_event_is_consumed_not_parked.py` proves the consumer honours the split, and this
proves the gate makes it correctly. Neither test is sufficient alone: a gate that always raised the
plain error would pass the consumer's suite, and a consumer that acked everything would pass this one.

WHY THE LINE IS "IS THERE ANYTHING TO GRANT ON". A named person denied on a GOVERNED table is
repairable — write the tuple, and the same event succeeds on its next presentation — so that refusal
keeps the DROP that parks it. A table FGA has no record of cannot be granted on at all, so parking the
event appends a dead-letter copy per restart about something that can never be accepted.

MEASURED on the deployed estate 2026-09-19: all 7 parks in a six-hour window named four outputs
(`e2e-ns$t74eff1b3`, `lh144b5e42f0dfns$aa`, `lh144b5e42f0dfns$bb`, `lh098bbff657cns$frag`) and a direct
`POST /stores/{id}/read` on `table:<id>` returned ZERO tuples for every one. The first also answers 404
from the catalog. So the repairable arm was holding nothing repairable.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import PermissionDeniedError

from lineage.api.fga_deps import _none_are_governed
from lineage.models import UngovernedOutputError
from service_kit.governed import fga


_NAMES = ["gone-ns$a", "gone-ns$b"]
#: The probe reaches OpenFGA only through the patched `read_object_tuples`, so the client is never
#: touched — naming that here beats a bare `None` that reads like an oversight.
_NO_CLIENT = cast(Any, None)


def _tuples_for(mapping: dict[str, list[object]]) -> Any:
    async def read(_client: object, obj: str, **_kw: object) -> list[object]:
        return mapping.get(obj, [])

    return read


@pytest.mark.asyncio
async def test_every_name_untupled_is_ungoverned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fga, "read_object_tuples", _tuples_for({}))

    assert await _none_are_governed(_NO_CLIENT, names=_NAMES, object_type="table") is True


@pytest.mark.asyncio
async def test_ONE_governed_name_makes_the_whole_refusal_repairable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denial naming a governed table and an unknown one is still repairable — granting on the
    governed one changes the answer. Acking here would discard provenance a tuple away."""
    monkeypatch.setattr(fga, "read_object_tuples", _tuples_for({"table:gone-ns$b": [object()]}))

    assert await _none_are_governed(_NO_CLIENT, names=_NAMES, object_type="table") is False


@pytest.mark.asyncio
async def test_an_UNREADABLE_store_falls_back_to_the_repairable_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable is not evidence of absence. Parking a repairable event costs a duplicate; acking an
    unreadable one deletes provenance, and only the second cannot be undone — so the doubt resolves
    toward keeping the event."""

    async def boom(_client: object, _obj: str, **_kw: object) -> list[object]:
        raise RuntimeError("openfga unreachable")

    monkeypatch.setattr(fga, "read_object_tuples", boom)

    assert await _none_are_governed(_NO_CLIENT, names=_NAMES, object_type="table") is False


def test_the_unrepairable_type_is_still_a_permission_denial() -> None:
    """It must keep answering 403 on the HTTP door while changing only the BUS ack — the two doors
    share this gate, and a new top-level type would have fallen out of every `except` that handles a
    refusal on the request path."""
    assert issubclass(UngovernedOutputError, PermissionDeniedError)
