"""OpenFGA's connection pool is a declared share of the Postgres it runs on, not a driver default.

[[XC-016]]. `rask-age` serves OpenFGA from a server that runs Postgres' stock `max_connections=100` — the
chart sets nothing — so OpenFGA's pool is a declared share of that server, not whatever its driver's default
happens to be.

THE ROW'S PREMISE WAS WRONG AND THE MEASUREMENT SAYS SO. It read "every governed read and write blocks
on a Check against that pool". Sampled 2026-09-23 twelve times over 2.5 minutes, spanning a maintenance
sweep tick: **28 connections, ALL idle, 0 active, every single sample**; the server 49 of 100, 21 of
them held by its other consumers and an operator's session. There is no contention — raising `maxOpenConns`, which
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
#: What is kept for every session that is not openfga's pool: its migrate Job, the backup's dump and an operator's psql.
#: Sized from the 21 connections measured beside openfga's on 2026-09-23, rounded up, so it is an observation rather
#: than a contract, and it is deliberately generous.
OTHER_CONSUMERS = 25
#: The share of the server this arithmetic may claim. A pool can exceed its steady state under burst,
#: and a refused connection is an outage for every governed read, so the budget is not spent to the rim.
USABLE_FRACTION = 0.75


def _openfga_datastore() -> dict[str, Any]:
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    return values["openfga"]["datastore"]


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
