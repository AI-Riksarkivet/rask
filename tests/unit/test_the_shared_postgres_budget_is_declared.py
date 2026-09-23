"""OpenFGA's connection pool is a declared share of a Postgres three components use, not a driver default.

[[XC-016]]. `rask-age` serves the lineage graph AND OpenFGA AND Dapr's state store from one server, and
that server runs Postgres' stock `max_connections=100` — the chart sets nothing. None of the three
consumers declared a pool bound either, so the estate's whole connection budget was whatever three
independent driver defaults happened to add up to.

THE ROW'S PREMISE WAS WRONG AND THE MEASUREMENT SAYS SO. It read "every governed read and write blocks
on a Check against that pool". Sampled 2026-09-23 twelve times over 2.5 minutes, spanning a maintenance
sweep tick: **28 connections, ALL idle, 0 active, every single sample**; estate-wide 49 of 100
(openfga 28, daprstate 10, lineage 5, admin 1). There is no contention — raising `maxOpenConns`, which
the row proposed, would change nothing.

WHAT IS REAL IS THE ALLOCATION. One component holds 28% of a shared budget and does no measurable work
with it, because pgxpool's default ceiling is 30 and nobody chose it. So the fix is to DECLARE the
share with the measurement beside it, which is also this row's own closing bar — not to enlarge it.

THE SAMPLING'S LIMIT, stated rather than glossed: an FGA Check is sub-millisecond, so twelve samples
finding zero active queries does not prove no checks ran. It proves none was long enough to be caught,
which is the same evidence for "the pool is not saturated" and none at all for "the pool is unused".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO = Path(__file__).resolve().parents[2]
VALUES = REPO / "chart/values.yaml"

#: Postgres' stock ceiling on `rask-age`, which the chart does not override. Measured live 2026-09-23.
PG_MAX_CONNECTIONS = 100
#: What the OTHER consumers of this server were measured holding, plus room for an operator's psql.
#: daprstate 10 + lineage 5 + admin 1, rounded up — neither of those declares a bound of its own yet,
#: so this is an observation rather than a contract, and it is deliberately generous.
OTHER_CONSUMERS = 25
#: The share of the server this arithmetic may claim. A pool can exceed its steady state under burst,
#: and a refused connection is an outage for every governed read, so the budget is not spent to the rim.
USABLE_FRACTION = 0.75


def _openfga_datastore() -> dict[str, Any]:
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    return values["openfga"]["datastore"]


def test_the_pool_bounds_are_declared_rather_than_inherited() -> None:
    """A driver default is not a decision. `maxOpenConns` unset means pgxpool's 30, chosen by nobody."""
    datastore = _openfga_datastore()
    missing = [key for key in ("maxOpenConns", "maxIdleConns") if key not in datastore]
    assert missing == [], f"openfga.datastore leaves {missing} to the driver on a Postgres three components share"


def test_the_declared_pool_fits_the_server_it_shares() -> None:
    """The invariant: one component's ceiling plus what the others hold must fit inside the server."""
    datastore = _openfga_datastore()
    needed = int(datastore["maxOpenConns"]) + OTHER_CONSUMERS
    usable = PG_MAX_CONNECTIONS * USABLE_FRACTION
    assert needed <= usable, (
        f"openfga may open {datastore['maxOpenConns']} connections and the other consumers hold about "
        f"{OTHER_CONSUMERS} — {needed} against {usable:.0f} usable ({PG_MAX_CONNECTIONS} x {USABLE_FRACTION}). "
        "Lower the pool or raise max_connections on rask-age."
    )


def test_idle_is_not_larger_than_open() -> None:
    """A pool that may KEEP more than it may OPEN is a configuration nobody meant."""
    datastore = _openfga_datastore()
    assert int(datastore["maxIdleConns"]) <= int(datastore["maxOpenConns"])


def test_the_gate_refuses_the_configuration_it_replaced() -> None:
    """A gate that cannot fail is not a gate. pgxpool's inherited default sat at 30 with ~25 held by
    the others; it fits. What must NOT fit is the enlargement the row proposed — this records the
    ceiling so a later 'just raise it' is refused by arithmetic rather than by memory."""
    usable = PG_MAX_CONNECTIONS * USABLE_FRACTION
    assert usable >= 30 + OTHER_CONSUMERS, "the measured status quo should fit — if not, the constants are wrong"
    assert not (usable >= 60 + OTHER_CONSUMERS), "a doubled pool must be refused by this arithmetic"
