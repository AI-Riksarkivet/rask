"""Namespace creation is depth-capped, and the cap is an OpenFGA limit rather than a taste.

`warehouse#can_get_metadata` and `namespace#can_get_metadata` are both recursive
(`reader or can_get_metadata from child`, over `namespace#parent: [warehouse, namespace]`), and
OpenFGA abandons a resolution that needs too many rewrite rules instead of answering it. Measured
2026-08-16 against the SHIPPED model with the real evaluator (`fga model test`):

    depth 12 -> Checks 1/1 passing
    depth 13 -> got=N/A, error=rpc error: code = Code(2002) desc = Authorization Model resolution
                required too many rewrite rules to be resolved.

`N/A` is the whole problem. It is not a deny — it is an ERROR, and the fleet fails closed on an
unrecognised authz error, so it surfaces as a 503 on the browse path. And it is LATERAL: the check
that dies is the one rooted at the WAREHOUSE, so a single pathological branch takes metadata reads
down for every object in that bucket and every user of it, owners included. A plain `writer` — the
rung that may create namespaces — is enough to build one.

Both read walkers (`tables.py::_collect_tables`, `namespaces.py::_collect_descendants`) had capped
their recursion for a while. Nothing capped CREATION, so the estate could be driven into a shape its
own authorization model cannot evaluate. These tests pin the door.

Three separate things now have to agree about the number, which is why it is ONE constant in
`catalog.core.identifiers` rather than a literal per site — F10 item 10 was exactly two walkers
disagreeing about how deep a tree may go.
"""

from __future__ import annotations

import pytest
from lance_namespace import InvalidInputError

from catalog.api import fga_deps
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, parse_identifier
from service_kit.lakehouse.naming import CONTROL_ID_RE


DELIM = "$"

#: The depth at which the real evaluator stopped answering, measured as documented above. The ceiling
#: must stay strictly below it — equality is not safe, because the failing resolution is the NEGATIVE
#: check, whose cost depends on the branching of the tree and not only on its depth.
MEASURED_EVALUATOR_LIMIT = 13

#: `MAX_NAMESPACE_DEPTH` is an EXCLUSIVE bound — both read walkers stop at `>=` it — so the deepest
#: tree the doors actually admit is one rung shallower than the number.
DEEPEST_LEGAL_DEPTH = MAX_NAMESPACE_DEPTH - 1


# --------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------- #


def test_one_level_past_the_ceiling_is_refused() -> None:
    fga_deps.require_namespace_depth([f"n{i}" for i in range(DEEPEST_LEGAL_DEPTH)], delimiter=DELIM)
    with pytest.raises(InvalidInputError):
        fga_deps.require_namespace_depth([f"n{i}" for i in range(DEEPEST_LEGAL_DEPTH + 1)], delimiter=DELIM)


# --------------------------------------------------------------------------- #
# ONE constant (the F10 item 10 lesson)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# both doors
# --------------------------------------------------------------------------- #


def test_the_warehouse_door_is_NOT_structurally_shallow() -> None:
    """The premise of capping the warehouse door, checked rather than asserted in a comment.

    A "top-level" namespace sounds like it cannot be deep. It can: the door validates the name with
    `CONTROL_ID_RE`, which permits hyphens, and `LANCE_NS_DELIMITER` is operator-settable — so under
    a hyphen delimiter one legal 63-character name splits into far more segments than the evaluator
    can resolve. If `_validate_id` is ever tightened to forbid hyphens, the warehouse door's guard
    becomes genuinely dead and this test says so, instead of a comment quietly ceasing to be true.
    """
    deep_but_legal = "-".join(f"n{i}" for i in range(MEASURED_EVALUATOR_LIMIT + 1))
    assert len(deep_but_legal) <= 63, "keep the probe inside what CONTROL_ID_RE's length bound allows"
    assert CONTROL_ID_RE.match(deep_but_legal), "a hyphenated name is a legal control id"
    segments = parse_identifier(deep_but_legal, "-")
    assert len(segments) > MEASURED_EVALUATOR_LIMIT, "…and under a hyphen delimiter it is a tree past the limit"
    with pytest.raises(InvalidInputError):
        fga_deps.require_namespace_depth(segments, delimiter="-")
