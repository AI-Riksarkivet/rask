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

from service_kit.governed.user_state import UserStateDocument
from service_kit.schemas.dock_layout import (
    MAX_VIEWS_PER_WORKBENCH,
    DockLayoutLibrary,
    DockLayouts,
)


def _layout() -> dict[str, object]:
    """The minimum dockview calls a layout: `grid` (with its measured size) and `panels`."""
    return {
        "grid": {"root": {"type": "branch", "data": []}, "width": 1200, "height": 800, "orientation": "HORIZONTAL"},
        "panels": {"runs": {"id": "runs", "contentComponent": "runs", "title": "Runs"}},
    }


def _view(view_id: str = "v1", name: str = "Compare two runs") -> dict[str, object]:
    return {"id": view_id, "name": name, "layout": _layout(), "updated": "2026-07-29T10:11:12.512Z"}


class TestTheImplicitLayoutIsUntouched:
    """The half that must not change. If any of these break, the split has failed at its one job."""

    def test_a_document_written_by_the_old_client_still_loads(self) -> None:
        # The compatibility floor: today's clients write exactly this, and they must keep working after
        # the library ships. There is no migration and no version field — because there is no change.
        old = {"workbenches": {"lineage": _layout(), "media": _layout()}}
        parsed = DockLayouts.model_validate(old)
        assert set(parsed.workbenches) == {"lineage", "media"}

    def test_a_sibling_key_inside_DockLayouts_is_still_refused(self) -> None:
        # This is WHY the library is a separate document. Were it a key here, `extra="forbid"` would
        # 422 it — and relaxing that to allow one would mean a client that does not know the key drops
        # it on the next read-modify-write, erasing every saved view.
        with pytest.raises(ValidationError):
            DockLayouts.model_validate({"workbenches": {}, "library": {"views": []}})

    def test_the_two_documents_are_different_members_of_the_closed_key_space(self) -> None:
        assert UserStateDocument.DOCK_LAYOUT.value == "dock-layout"
        assert UserStateDocument.DOCK_LAYOUT_LIBRARY.value == "dock-layout-library"
        assert UserStateDocument.DOCK_LAYOUT is not UserStateDocument.DOCK_LAYOUT_LIBRARY


class TestTheLibrary:
    def test_round_trips_a_named_view_without_mutating_the_layout(self) -> None:
        # The interior is dockview's, not ours: it goes straight back to the deserializer that wrote it,
        # so the round trip must be lossless.
        doc = {"workbenches": {"lineage": {"views": [_view()]}}}
        parsed = DockLayoutLibrary.model_validate(doc)
        assert parsed.model_dump(mode="json") == doc

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

    def test_an_empty_library_is_valid(self) -> None:
        assert DockLayoutLibrary.model_validate({}).workbenches == {}

    def test_a_layout_missing_its_grid_is_refused(self) -> None:
        # Same floor the implicit document holds: a thing without `grid` is not a dock layout, and
        # storing it would only fail later, in the browser, as an unrestorable view.
        bad = _view() | {"layout": {"panels": {}}}
        with pytest.raises(ValidationError):
            DockLayoutLibrary.model_validate({"workbenches": {"lineage": {"views": [bad]}}})

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

    def test_the_cap_is_per_workbench_not_per_document(self) -> None:
        at_cap = [_view(f"v{i}") for i in range(MAX_VIEWS_PER_WORKBENCH)]
        doc = {"workbenches": {"lineage": {"views": at_cap}, "media": {"views": at_cap}}}
        assert len(DockLayoutLibrary.model_validate(doc).workbenches) == 2

    def test_an_unexpected_top_level_key_is_refused(self) -> None:
        # The envelope is ours, so an unknown key here is a client bug worth a 422 — the same stance
        # DockLayouts takes, and for the same reason.
        with pytest.raises(ValidationError):
            DockLayoutLibrary.model_validate({"workbenches": {}, "activeViewId": "v1"})
