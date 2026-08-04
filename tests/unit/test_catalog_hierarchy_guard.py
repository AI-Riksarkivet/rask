"""The containment rule, enforced at the door: project > warehouse > namespace > table.

`model.fga` has always said `table.parent: [namespace]`. The write path did not: a single-segment
table produced `parent_object() -> None`, the create went through, and the row got an owner grant
with no containment edge. It was invisible to every warehouse- or namespace-level grant and
unreachable by the hierarchy the UI navigates — an orphan that nothing reported.

These tests pin the refusal AND its wording, because a guard whose message does not say which rung
is missing just moves the confusion one layer out.
"""

from __future__ import annotations

import pytest
from catalog.api import fga_deps
from fastapi import HTTPException


DELIM = "$"


def test_a_table_with_no_namespace_is_refused() -> None:
    """The orphan case, which used to succeed silently."""
    with pytest.raises(HTTPException) as exc:
        fga_deps.require_parent("table", ["orphan"], delimiter=DELIM)
    assert exc.value.status_code == 422


def test_the_refusal_names_the_rule_and_the_fix() -> None:
    """A caller must learn what is wrong and what to do from the detail alone.

    Asserted on CONTENT rather than an exact string, so the wording can improve without breaking the
    test — but the three load-bearing parts (the identifier, the hierarchy, a corrected example)
    cannot quietly disappear.
    """
    with pytest.raises(HTTPException) as exc:
        fga_deps.require_parent("table", ["sales"], delimiter=DELIM)
    detail = str(exc.value.detail)
    assert "sales" in detail
    assert "namespace" in detail
    assert "project > warehouse > namespace > table" in detail
    # …and it shows the shape that WOULD work, with the delimiter this deployment uses.
    assert f"<namespace>{DELIM}sales" in detail


@pytest.mark.parametrize(
    "segments",
    [
        ["bronze", "pages"],  # one namespace deep — the ordinary case
        ["acme", "silver", "features"],  # nested namespaces, the self-nesting rung
    ],
)
def test_a_table_inside_a_namespace_is_allowed(segments: list[str]) -> None:
    """The guard must refuse orphans only. Anything with a namespace above it passes untouched."""
    fga_deps.require_parent("table", segments, delimiter=DELIM)


@pytest.mark.parametrize("resource", ["namespace", "materialized_view", "transaction"])
def test_only_tables_are_gated_here(resource: str) -> None:
    """Namespaces are deliberately NOT rejected.

    A top-level namespace legitimately parents to the catalog's warehouse root, and until a warehouse
    can be NAMED in the identifier, refusing one would refuse the only shape the API can express.
    That gap is real and tracked; encoding it as a rejection now would break every namespace create
    without giving callers a way to comply.
    """
    fga_deps.require_parent(resource, ["anything"], delimiter=DELIM)


def test_the_guard_agrees_with_what_the_grant_path_would_have_done() -> None:
    """The guard and `parent_object` must not drift.

    `require_parent` refuses exactly the inputs for which `parent_object` returns None — that is the
    definition of an orphan, and the two deriving it separately is how they diverge later.
    """
    from service_kit.governed import fga

    for segments in (["orphan"], ["bronze", "pages"], ["acme", "silver", "features"]):
        would_orphan = fga.parent_object("table", segments, delimiter=DELIM, root_object="warehouse:lance_catalog") is None
        refused = False
        try:
            fga_deps.require_parent("table", segments, delimiter=DELIM)
        except HTTPException:
            refused = True
        assert refused is would_orphan, f"guard and parent_object disagree on {segments}"
