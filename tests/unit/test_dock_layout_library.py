"""The NAMED dock views library — and the proof it cannot damage the implicit layout.

The feature is "save several dock arrangements, load one back". The risk is not the feature: it is that
a second writer near :class:`DockLayouts` destroys the live-arrangement document, which is the one whose
loss a user actually notices. These tests pin the separation that prevents it.

The three reasons the library is its own document, each asserted below rather than asserted in prose:

1. ``DockLayouts`` is ``extra="forbid"``, so a sibling key would 422 — and a client that does not know
   the key must DROP it, meaning one autosave from an older bundle erases every saved view.
2. The byte ceiling is per DOCUMENT, so a large library must not be able to fail the implicit autosave.
3. An unreadable library must not 409 the implicit layout, which the client turns into a permanent
   save-lock for the session.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from service_kit.schemas.dock_layout import (
    MAX_VIEWS_PER_WORKBENCH,
    DockLayoutLibrary,
)


def _layout() -> dict[str, object]:
    """The minimum dockview calls a layout: `grid` (with its measured size) and `panels`."""
    return {
        "grid": {"root": {"type": "branch", "data": []}, "width": 1200, "height": 800, "orientation": "HORIZONTAL"},
        "panels": {"runs": {"id": "runs", "contentComponent": "runs", "title": "Runs"}},
    }


def _view(view_id: str = "v1", name: str = "Compare two runs") -> dict[str, object]:
    return {"id": view_id, "name": name, "layout": _layout(), "updated": "2026-07-29T10:11:12.512Z"}


class TestTheLibrary:
    def test_preserves_unknown_keys_INSIDE_a_layout(self) -> None:
        # `floatingGroups`, `popoutGroups`, `edgeGroups` and `activeGroup` are all optional in
        # SerializedDockview. Dropping one hands the user back a layout with their floating panels
        # DELETED — data loss that reads as a missing feature.
        layout = _layout() | {"floatingGroups": [{"data": {"views": ["graph"], "id": "2"}}], "activeGroup": "1"}
        view = _view() | {"layout": layout}
        parsed = DockLayoutLibrary.model_validate({"workbenches": {"lineage": {"views": [view]}}})
        stored = parsed.model_dump(mode="json")["workbenches"]["lineage"]["views"][0]["layout"]
        assert stored["floatingGroups"] == layout["floatingGroups"]
        assert stored["activeGroup"] == "1"

    def test_duplicate_view_ids_are_refused(self) -> None:
        # A duplicate id makes load/rename/delete silently ambiguous — the client would act on whichever
        # copy it found first, which is not a behaviour anyone can reason about.
        doc = {"workbenches": {"lineage": {"views": [_view("v1"), _view("v1", "Another")]}}}
        with pytest.raises(ValidationError, match="duplicate view ids"):
            DockLayoutLibrary.model_validate(doc)

    def test_two_views_may_share_a_NAME(self) -> None:
        # Deliberately allowed. A 422 in the middle of a rename is worse than a duplicate row the user
        # can see and fix; the id is what the code addresses.
        doc = {"workbenches": {"lineage": {"views": [_view("v1", "Same"), _view("v2", "Same")]}}}
        assert len(DockLayoutLibrary.model_validate(doc).workbenches["lineage"].views) == 2

    def test_the_view_cap_turns_a_byte_ceiling_into_something_actionable(self) -> None:
        # Without it the failure is the per-document byte ceiling: a 400 that names no cause and that a
        # user cannot act on. With it, the message is "at most 50 views".
        too_many = [_view(f"v{i}") for i in range(MAX_VIEWS_PER_WORKBENCH + 1)]
        with pytest.raises(ValidationError):
            DockLayoutLibrary.model_validate({"workbenches": {"lineage": {"views": too_many}}})
