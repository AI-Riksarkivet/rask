"""The containment rule, enforced at the door: project > warehouse > namespace > table.

`model.fga` says `table.parent: [namespace]`. A single-segment table has no namespace for its `parent`
edge to name (`parent_object_of` answers `None`), so a create that went through would give the row an
owner grant with no containment edge: invisible to every warehouse- or namespace-level grant and
unreachable by the hierarchy the UI navigates — an orphan that nothing reported.

The refusal is the spec's own InvalidInput (code 13 -> HTTP 400, RFC 9457 problem body via
install_problem_handlers) — the Lance Namespace error model has no 422, and client SDKs dispatch on
the numeric code. These tests pin the refusal AND its wording, because a guard whose message does not say which rung
is missing just moves the confusion one layer out.
"""

from __future__ import annotations

import asyncio

import pytest
from lance_namespace import InvalidInputError

from catalog.api import fga_deps
from catalog.core.config import Settings
from tests.unit.named_resolver import NamedResolver


DELIM = "$"


def test_the_refusal_names_the_rule_and_the_fix() -> None:
    """A caller must learn what is wrong and what to do from the detail alone.

    Asserted on CONTENT rather than an exact string, so the wording can improve without breaking the
    test — but the three load-bearing parts (the identifier, the hierarchy, a corrected example)
    cannot quietly disappear.
    """
    with pytest.raises(InvalidInputError) as exc:
        fga_deps.require_parent("table", ["sales"])
    detail = str(exc.value)
    assert "sales" in detail
    assert "namespace" in detail
    assert "project > warehouse > namespace > table" in detail
    # …and it shows the shape that WOULD work, with the spec's delimiter.
    assert f"<namespace>{DELIM}sales" in detail


@pytest.mark.parametrize("resource", ["namespace"])
def test_only_tables_are_gated_here(resource: str) -> None:
    """Namespaces are deliberately NOT rejected.

    A top-level namespace legitimately parents to the catalog's warehouse root, and until a warehouse
    can be NAMED in the identifier, refusing one would refuse the only shape the API can express.
    That gap is real and tracked; encoding it as a rejection now would break every namespace create
    without giving callers a way to comply.
    """
    fga_deps.require_parent(resource, ["anything"])


def test_the_guard_agrees_with_what_the_grant_path_would_have_done() -> None:
    """The guard and `parent_object_of` must not drift.

    `require_parent` refuses exactly the inputs for which `parent_object_of` returns None — that is the
    definition of an orphan, and the two deriving it separately is how they diverge later.
    """
    settings = Settings.model_validate({"LANCE_S3_ACCESS_KEY_ID": "k", "LANCE_S3_SECRET_ACCESS_KEY": "s"})
    for segments in (["orphan"], ["bronze", "pages"], ["acme", "silver", "features"]):
        would_orphan = asyncio.run(fga_deps.parent_object_of(NamedResolver(), settings, "table", segments)) is None
        refused = False
        try:
            fga_deps.require_parent("table", segments)
        except InvalidInputError:
            refused = True
        assert refused is would_orphan, f"guard and parent_object disagree on {segments}"


# --------------------------------------------------------------------------- #
# A top-level namespace must belong to a warehouse.
# --------------------------------------------------------------------------- #


def test_the_namespace_refusal_points_at_the_warehouse_route() -> None:
    """A refusal that does not say where to go instead is just an outage."""
    with pytest.raises(InvalidInputError) as exc:
        fga_deps.require_warehouse_scoped(["bronze"], warehouses_enabled=True)
    detail = str(exc.value)
    assert "bronze" in detail
    assert "/management/v1/warehouses/{warehouse_id}/namespaces" in detail
    assert "project > warehouse > namespace > table" in detail
    # …and the other legal shape: nesting under an existing (already bound) namespace.
    assert f"<parent>{DELIM}bronze" in detail


@pytest.mark.parametrize("segments", [["acme", "silver"]])
def test_a_nested_namespace_inherits_its_parents_binding(segments: list[str]) -> None:
    """Only the TOP rung names a warehouse; everything below inherits it, so nesting stays open."""
    fga_deps.require_warehouse_scoped(segments, warehouses_enabled=True)


# --------------------------------------------------------------------------- #
# The control-plane id shape is ONE rule, not one per door.
# --------------------------------------------------------------------------- #


def test_a_control_plane_id_cannot_end_in_a_newline() -> None:
    """Python's ``$`` also matches just BEFORE a trailing newline, and two of the three copies of this
    pattern were ``$``-anchored — so ``"acme\\n"`` passed the shape check.

    That is not cosmetic. The id becomes a registry FILENAME and an OpenFGA object id, and OpenFGA
    rejects whitespace in the latter: ``put_project`` lands, ``seed_project_admin`` fails, and the
    result is a record with no tuples — the exact record-without-tuples drift Decision 1 exists to
    prevent, produced by the guard meant to stop it.
    """
    from service_kit.lakehouse.naming import CONTROL_ID_RE

    assert CONTROL_ID_RE.match("acme") is not None
    assert CONTROL_ID_RE.match("acme\n") is None
    assert CONTROL_ID_RE.match("acme\nevil") is None


def test_every_control_plane_door_shares_ONE_id_pattern() -> None:
    """The three doors validating a project/warehouse id must use the same object, not three copies.

    They were three copies of one sentence ("the same DNS-safe shape the warehouse control plane
    enforces") and they had already drifted on their end anchor — an id refused by ``POST /management/v1/projects``
    was accepted by ``POST /management/v1/warehouses``. Identity (``is``), not equality: two equal-but-separate
    compilations would drift again exactly the same way.
    """
    from catalog.api.v1.endpoints import policies, projects, warehouses
    from service_kit.lakehouse.naming import CONTROL_ID_RE

    assert projects._ID_RE is CONTROL_ID_RE
    assert warehouses._ID_RE is CONTROL_ID_RE
    assert policies._PROJECT_ID_RE is CONTROL_ID_RE
