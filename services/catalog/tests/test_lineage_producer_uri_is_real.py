"""CAT-CORE-18 — the OpenLineage ``producer`` URI stamped on every catalog event must resolve.

``producer`` is what a Marquez-style consumer records as "which software emitted this", and it is the
only pointer an operator reading a run event has back to the code. The catalog's stamped the WRONG
repository (``Borg93/lance-ns``, the pre-rename name) at a path that never existed in any repo
(``services/catalog/core/…`` — the module lives under ``services/catalog/src/catalog/core/``), so the
one link on every event 404s.

Checked against the tree, not against a string: the URI's repo must be the estate's own, and the path
it points at must be a file that actually exists here. ``packages/lineage-kit``'s ``PRODUCER`` is the
form this follows.
"""

from __future__ import annotations

import pathlib

from catalog.core.lineage_emit import _PRODUCER


_REPO = pathlib.Path(__file__).resolve().parents[3]
_PREFIX = "https://github.com/Borg93/rask/tree/main/"


def test_the_repo_root_is_where_this_test_thinks_it_is() -> None:
    assert (_REPO / "services" / "catalog" / "src" / "catalog" / "core" / "lineage_emit.py").is_file(), (
        f"{_REPO} is not the rask checkout — the path assertion below would be vacuous"
    )


def test_the_producer_uri_names_this_repository() -> None:
    assert _PRODUCER.startswith(_PREFIX), f"producer URI {_PRODUCER!r} does not point at {_PREFIX}"


def test_the_producer_uri_points_at_a_path_that_exists() -> None:
    relative = _PRODUCER.removeprefix(_PREFIX)
    assert relative, "producer URI carries no path"
    assert (_REPO / relative).exists(), f"producer URI path {relative!r} does not exist in the tree — the link 404s"
