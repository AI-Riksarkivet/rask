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

import re
from pathlib import Path
from types import ModuleType

import pytest
from lance_namespace import InvalidInputError

from catalog.api import fga_deps
from catalog.api.v1.endpoints import namespaces as ns_endpoint
from catalog.api.v1.endpoints import warehouses as wh_endpoint
from catalog.core.identifiers import CONTROL_ID_RE, MAX_NAMESPACE_DEPTH, parse_identifier


DELIM = "$"
REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_YAML = REPO_ROOT / "packages" / "service-kit" / "src" / "service_kit" / "governed" / "auth" / "model.fga.yaml"

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


def test_a_namespace_at_the_ceiling_is_allowed() -> None:
    """The cap is a ceiling, not a fence one short of it — the deepest legal tree must still be creatable."""
    fga_deps.require_namespace_depth([f"n{i}" for i in range(DEEPEST_LEGAL_DEPTH)], delimiter=DELIM)


def test_one_level_past_the_ceiling_is_refused() -> None:
    fga_deps.require_namespace_depth([f"n{i}" for i in range(DEEPEST_LEGAL_DEPTH)], delimiter=DELIM)
    with pytest.raises(InvalidInputError):
        fga_deps.require_namespace_depth([f"n{i}" for i in range(DEEPEST_LEGAL_DEPTH + 1)], delimiter=DELIM)


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_ordinary_trees_pass_untouched(depth: int) -> None:
    """The medallion is two levels. A guard that inconveniences the normal case is the wrong guard."""
    fga_deps.require_namespace_depth([f"n{i}" for i in range(depth)], delimiter=DELIM)


def test_the_refusal_is_the_SPEC_error_so_clients_dispatch_on_the_code() -> None:
    """`InvalidInputError` is spec code 13 -> HTTP 400 via `install_problem_handlers`.

    A hand-picked `HTTPException` was shipped once for a domain error and no generated client
    understood it. A depth violation is a malformed request — the caller's id is the wrong SHAPE —
    not a permission problem, so answering 403 would send them hunting for a grant that cannot help.
    """
    with pytest.raises(InvalidInputError):
        fga_deps.require_namespace_depth([f"n{i}" for i in range(DEEPEST_LEGAL_DEPTH + 1)], delimiter=DELIM)


def test_the_refusal_says_how_deep_how_deep_is_allowed_and_why() -> None:
    """Asserted on content, not an exact string: the wording may improve, the three facts may not go.

    Without the reason, the obvious reading of a depth cap is "arbitrary platform limit, ask them to
    raise it" — and raising it is precisely the change that breaks the estate.
    """
    segments = [f"lvl{i}" for i in range(DEEPEST_LEGAL_DEPTH + 1)]
    with pytest.raises(InvalidInputError) as exc:
        fga_deps.require_namespace_depth(segments, delimiter=DELIM)
    detail = str(exc.value)
    assert DELIM.join(segments) in detail, "must name the identifier it refused"
    assert str(len(segments)) in detail and str(DEEPEST_LEGAL_DEPTH) in detail, "must give both depths"
    assert "warehouse" in detail, "must say the blast radius is the warehouse, not this one namespace"


def test_the_message_uses_THIS_deployment_s_delimiter() -> None:
    """`LANCE_NS_DELIMITER` is operator-settable; an id echoed with the wrong separator is unusable."""
    segments = [f"lvl{i}" for i in range(DEEPEST_LEGAL_DEPTH + 1)]
    with pytest.raises(InvalidInputError) as exc:
        fga_deps.require_namespace_depth(segments, delimiter="-")
    assert "-".join(segments) in str(exc.value)


# --------------------------------------------------------------------------- #
# ONE constant (the F10 item 10 lesson)
# --------------------------------------------------------------------------- #

#: Every module allowed to name the number. Only the shape-rule module may DEFINE it.
_CONSUMERS = (
    "services/catalog/src/catalog/api/fga_deps.py",
    "services/catalog/src/catalog/api/v1/endpoints/tables.py",
    "services/catalog/src/catalog/api/v1/endpoints/namespaces.py",
)


def test_no_consumer_REDEFINES_the_depth_as_a_literal() -> None:
    """A source-level check, deliberately, because the obvious runtime one is vacuous.

    `tables.MAX_NAMESPACE_DEPTH is MAX_NAMESPACE_DEPTH` looks like it proves single-sourcing and
    proves nothing: CPython interns small integers, so two independent `= 8` literals are the same
    object and the assertion passes on exactly the drift it exists to catch. What actually cannot be
    faked is that no consumer assigns the number at all.
    """
    for rel in _CONSUMERS:
        source = (REPO_ROOT / rel).read_text()
        offenders = re.findall(r"^_?MAX_NAMESPACE_DEPTH\s*[:=][^=].*?(\d+)\s*$", source, re.MULTILINE)
        assert not offenders, f"{rel} redefines the depth as literal {offenders} instead of importing it"
        assert "from catalog.core.identifiers import" in source and "MAX_NAMESPACE_DEPTH" in source


def test_only_the_shape_rule_module_defines_it() -> None:
    source = (REPO_ROOT / "services/catalog/src/catalog/core/identifiers.py").read_text()
    assert re.search(r"^MAX_NAMESPACE_DEPTH = \d+$", source, re.MULTILINE)


def test_the_guard_and_both_walkers_use_the_SAME_comparison() -> None:
    """The number being shared is half of it; the operator is the other half.

    The walkers stop descending at `>= MAX_NAMESPACE_DEPTH`. A door comparing `>` would admit a
    namespace exactly at that depth — legal to create, and never descended into by either walker, so
    its tables are enumerated by nothing and a cascade drop leaves them behind. Same class of bug as
    two copies of the number, reached through an off-by-one instead.
    """
    for rel in _CONSUMERS:
        source = (REPO_ROOT / rel).read_text()
        for comparison in re.findall(r"len\([a-z_]+\)\s*([<>=]+)\s*_?MAX_NAMESPACE_DEPTH", source):
            assert comparison == ">=", f"{rel} compares depth with {comparison!r}, not '>='"


def test_the_ceiling_stays_below_what_the_evaluator_can_resolve() -> None:
    """The ceiling is derived from a measurement, so it must not be raised without redoing it.

    Strictly below, with room: the resolution that dies is the negative check, and its cost grows
    with the tree's BRANCHING as well as its depth — a chain probe is the cheapest case, not the
    worst one. If this is ever raised, re-measure against the real evaluator first; the deep-chain
    probe in `model.fga.yaml` is the harness.
    """
    assert MAX_NAMESPACE_DEPTH < MEASURED_EVALUATOR_LIMIT


def test_the_model_s_deep_chain_probe_is_exactly_as_deep_as_the_cap() -> None:
    """The yaml probe is what proves the ceiling resolves — so it has to track the ceiling.

    Raise `MAX_NAMESPACE_DEPTH` and leave the probe where it is and the model tests keep passing
    while saying nothing about the depth now permitted. This test is the link: raising the constant
    forces the chain to be extended, and extending it past what OpenFGA can resolve turns
    `fga model test` red on the spot instead of turning production 503 later.

    It walks the `child` EDGES rather than counting rung names, and that distinction is load-bearing:
    a first version matched `namespace:depth_(\\d+)` anywhere in the file and passed happily when an
    edge was deleted, because every rung is also named by the `parent` tuple pointing back at it. It
    was measuring that the names had been typed, not that they were connected — the exact vacuity a
    depth probe cannot afford, since a broken chain resolves shallower than it reads.
    """
    # Walk the chain from the warehouse down, so a missing rung shows up as a BREAK in the walk
    # rather than as a set that happens to still contain every name.
    raw = MODEL_YAML.read_text()
    chain: list[int] = []
    parent = "warehouse:depth_probe"
    while True:
        found = re.search(rf"user: 'namespace:depth_(\d+)', relation: child, object: '{re.escape(parent)}'", raw)
        if not found:
            break
        rung = int(found.group(1))
        assert rung not in chain, "the probe chain loops back on itself"
        chain.append(rung)
        parent = f"namespace:depth_{rung}"
    assert chain, "the depth-ceiling probe chain has gone missing from model.fga.yaml"
    assert len(chain) == DEEPEST_LEGAL_DEPTH, (
        f"the probe resolves {len(chain)} rungs deep ({chain}); it must be an unbroken chain of "
        f"exactly {DEEPEST_LEGAL_DEPTH} — the deepest tree the doors now allow"
    )
    # …and the grant that makes the positive assertion meaningful sits at the BOTTOM of that walk.
    assert f"relation: reader, object: 'namespace:depth_{chain[-1]}'" in raw, (
        "the probe's reader grant is not on the deepest rung, so its positive check no longer exercises the full chain"
    )


# --------------------------------------------------------------------------- #
# both doors
# --------------------------------------------------------------------------- #


def _door_body(module: ModuleType, name: str) -> str:
    source = Path(module.__file__ or "").read_text()
    return source.split(f"def {name}(", 1)[1].split("\n@router", 1)[0]


def test_BOTH_create_doors_call_the_guard() -> None:
    """`create_namespace` takes a nested id directly. `create_warehouse_namespace` looks top-level and
    is not — see the test below. Capping only one door leaves the hole open.
    """
    assert "require_namespace_depth" in _door_body(ns_endpoint, "create_namespace")
    assert "require_namespace_depth" in _door_body(wh_endpoint, "create_warehouse_namespace")


def test_the_guard_runs_BEFORE_the_native_create_at_both_doors() -> None:
    """Order is the rule: identity -> shape -> parent exists -> authz -> conflict -> native write.

    A shape refusal that lands after `native.call` has already created a real Lance object with no
    authz parent — the exact orphan the check order exists to prevent.
    """
    for module, door in ((ns_endpoint, "create_namespace"), (wh_endpoint, "create_warehouse_namespace")):
        body = _door_body(module, door)
        assert body.index("require_namespace_depth") < body.index('"create_namespace"'), f"{door} caps too late"


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
