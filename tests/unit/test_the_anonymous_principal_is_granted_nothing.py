"""No seed grants the identity a logged-out visitor borrows.

Q17-8 / §F2-4. `bff.ts:191-196` and `runs-feed.ts:224-231` send
`x-lance-service-identity: <LINEAGE_SERVICE_ID>` ONLY when there is no session — a signed-in user's
bearer wins in both. So that subject is not "the web service": it is the ANONYMOUS PRINCIPAL, and
whatever it is granted is what the public can read.

It was granted a great deal. Measured on the live store 2026-09-07: 7 reader grants across 6
namespaces spanning TWO tenants, plus a `writer` on `table:bronze$events`. Four of the readers came
from `seed_medallion_fga.sh` and three from `seed_estate.py` — and `values-prod.yaml:18` names the
first as a PRODUCTION PREREQUISITE, so this was not a demo-only mistake. The writer came from neither
and is estate residue (Q15-3's pattern: nothing removes what a test creates).

FAIL CLOSED IS THE RULING (owner, 2026-09-07): the subject starts with nothing, and an operator grants
it exactly what should be public — at which point "what can the public see" is a readable, revocable
set of tuples instead of whatever a service happens to hold.

A SUITE THAT READS LINEAGE AS `service-web` is what makes these grants look load-bearing: it asserts
what a logged-out visitor sees while appearing to assert governance.
"""

from __future__ import annotations

import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]
#: The default `frontend.serviceIdentity`, i.e. what `LINEAGE_SERVICE_ID` carries.
ANONYMOUS = "service-web"
SEEDS = ("scripts/seed_estate.py",)


@pytest.mark.parametrize("seed", SEEDS)
def test_no_seed_grants_the_anonymous_principal(seed: str) -> None:
    """A grant here is a grant to every logged-out visitor of the estate."""
    granting = [line.strip() for line in (REPO / seed).read_text().splitlines() if ANONYMOUS in line and not line.lstrip().startswith(("#", "//"))]
    assert not granting, (
        f"{seed} grants the anonymous principal {ANONYMOUS!r} — that is a grant to the PUBLIC, not to a "
        f"service. Grant a human, or grant this subject deliberately and say so: {granting}"
    )
