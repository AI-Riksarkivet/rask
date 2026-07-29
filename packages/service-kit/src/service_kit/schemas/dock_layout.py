"""Server-side envelope for a subject's dock workbench layouts.

These are `@rask/dockview` layouts as dockview itself serialized them (`DockviewApi.toJSON()` →
``SerializedDockview``). One document holds ALL of a user's workbenches, keyed by a workbench id the
ZONE chooses — so a zone adding a workbench never needs a release of this package, and
:class:`~service_kit.governed.user_state.UserStateDocument` stays the closed key space it exists to be.

**Why the interiors are opaque.** ``grid.root`` is a recursive branch/leaf tree owned by dockview, and
``panels`` is a map of its own panel state. Mirroring either field-for-field would be a model that
breaks on every dockview minor release while buying nothing: this service never interprets a layout,
it hands it back to the same library that wrote it. So the ENVELOPE is asserted — the keys whose
absence means "this is not a dock layout at all" — and the interiors ride as :data:`JsonValue`. That
is the honest boundary: validate what a mis-stored document would actually fail on, and do not
pretend to own another library's format.

**``extra="allow"`` — and this is where it diverges from :mod:`service_kit.schemas.workflow`.** That
module documents unknown keys being DROPPED as acceptable ("a zone shipping a new field ahead of the
catalog loses the field, not the document"). For a dock layout that reasoning inverts: ``floatingGroups``,
``popoutGroups``, ``edgeGroups`` and ``activeGroup`` are all optional in ``SerializedDockview``, so a
catalog that silently dropped one would hand the user back a layout with their floating panels
*deleted* — a lossy round trip that reads as data loss, not as a missing feature. The document must
survive this service unchanged, so unknown keys are preserved.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class DockGrid(BaseModel):
    """The grid half of a serialized dock: a recursive node tree plus the size it was measured at."""

    model_config = ConfigDict(extra="allow")

    #: The branch/leaf tree. Opaque — dockview's own recursive ``SerializedGridObject``.
    root: JsonValue
    width: float
    height: float
    #: ``'HORIZONTAL'`` / ``'VERTICAL'`` today. Deliberately ``str`` and not a ``Literal``: the
    #: vocabulary belongs to dockview, and a ``Literal`` would 422 a layout written by a newer client.
    orientation: str


class SerializedDock(BaseModel):
    """One workbench's layout, exactly as ``DockviewApi.toJSON()`` produced it.

    Only ``grid`` and ``panels`` are required — they are the two fields ``SerializedDockview`` declares
    as non-optional, so a document missing either is not a dock layout and must not be stored as one.
    Everything else dockview writes rides through ``extra="allow"``.
    """

    model_config = ConfigDict(extra="allow")

    grid: DockGrid
    #: Per-panel state keyed by panel id — component name, title, params, size constraints.
    panels: dict[str, JsonValue]


class DockLayouts(BaseModel):
    """Every workbench layout one subject has arranged.

    ``extra="forbid"`` here, unlike the models above: this envelope is OURS, not dockview's, so an
    unexpected top-level key is a client bug worth a 422 rather than a forward-compatibility case.
    """

    model_config = ConfigDict(extra="forbid")

    #: workbench id -> that workbench's layout. The id is the zone's choice (e.g. ``"lineage"``);
    #: this package neither mints nor interprets it.
    workbenches: dict[str, SerializedDock] = Field(default_factory=dict)


#: A generous cap that still turns "you blew the per-document byte ceiling" — a 400 nobody can act on —
#: into "at most 50 views per workbench", which a user can. A serialized dock runs ~1.5-2 KB.
MAX_VIEWS_PER_WORKBENCH = 50


class DockView(BaseModel):
    """One NAMED layout — a "view" the user saved deliberately, as opposed to the implicit draft.

    ``extra="forbid"``: this envelope is ours. The interior ``layout`` is where dockview's own
    forward-compatibility lives, since :class:`SerializedDock` is ``extra="allow"``.
    """

    model_config = ConfigDict(extra="forbid")

    #: Stable across renames, so a client can address a view without depending on its name.
    id: str = Field(min_length=1, max_length=64)
    #: What the sidebar shows. Renameable, and NOT uniqueness-checked: two views may legitimately share
    #: a name until the user notices, and a 422 mid-rename is worse than a duplicate row.
    name: str = Field(min_length=1, max_length=120)
    #: The arrangement itself, exactly as ``DockviewApi.toJSON()`` produced it.
    layout: SerializedDock
    #: ISO-8601, set by the CLIENT. The server does not stamp it: this document is the caller's own
    #: work, and a server clock would only disagree with the one that ordered the list.
    updated: str = Field(min_length=1, max_length=40)


class DockWorkbenchViews(BaseModel):
    """One workbench's library. A LIST, not a map — order is user-visible in the sidebar."""

    model_config = ConfigDict(extra="forbid")

    views: list[DockView] = Field(default_factory=list, max_length=MAX_VIEWS_PER_WORKBENCH)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        """A duplicate id is a client bug that makes load/rename/delete silently ambiguous."""
        ids = [v.id for v in self.views]
        if len(ids) != len(set(ids)):
            duplicates = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate view ids: {duplicates}")
        return self


class DockLayoutLibrary(BaseModel):
    """Every NAMED dock view one subject has saved, keyed by workbench id.

    Deliberately a SEPARATE user-state document from :class:`DockLayouts`, never a key inside it — see
    :attr:`service_kit.governed.user_state.UserStateDocument.DOCK_LAYOUT_LIBRARY` for the three
    reasons. The consequence worth restating: the implicit autosave in :class:`DockLayouts` is
    untouched by this model, byte for byte, so the live-arrangement path — the one whose failure
    destroys a user's workspace — keeps working exactly as it did.
    """

    model_config = ConfigDict(extra="forbid")

    #: workbench id -> that workbench's saved views.
    workbenches: dict[str, DockWorkbenchViews] = Field(default_factory=dict)
