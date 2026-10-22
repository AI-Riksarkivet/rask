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

from lineage.api.fga_deps import _none_are_governed
from service_kit.governed import fga
from service_kit.governed.decider import OpenFgaDecider


#: Two tables' objects, ``table:<uuid>`` ([[LH-524]]).
_OBJECTS = ["table:7b0c1f52-3c8e-4b8a-9a7e-0f1d2c3b4a51", "table:1e2d3c4b-5a69-4788-9a1b-2c3d4e5f6a7b"]
#: The probe reaches OpenFGA only through the patched `read_object_tuples`, so the client is never
#: touched — naming that here beats a bare `None` that reads like an oversight.
_NO_CLIENT = cast(Any, None)


@pytest.mark.asyncio
async def test_an_UNREADABLE_store_falls_back_to_the_repairable_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable is not evidence of absence. Parking a repairable event costs a duplicate; acking an
    unreadable one deletes provenance, and only the second cannot be undone — so the doubt resolves
    toward keeping the event."""

    read: list[str] = []

    async def boom(_client: object, obj: str, **_kw: object) -> list[object]:
        read.append(obj)
        raise RuntimeError("openfga unreachable")

    monkeypatch.setattr(fga, "read_object_tuples", boom)

    assert await _none_are_governed(OpenFgaDecider(_NO_CLIENT), objects=_OBJECTS) is False
    assert read == _OBJECTS[:1], "the probe never reached the store, so the fallback was not the one under test"
