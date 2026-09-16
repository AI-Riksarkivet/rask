"""A `/maintenance/` verb that runs IN-POD and mints a new version must emit lineage for it.

[[LH-153]]. Both mutating maintenance verbs have two lanes, chosen off the same topic name the
maintenance service reads: with a work queue the door publishes one unit and answers 202, and the
executor on the other side emits; with no queue configured the rewrite runs here in the request
handler. The deployed estate runs the second lane — `maintenance.workTopic` is `""` on the release's
values — and that lane emitted nothing, so the graph had no account of who compacted a table or when.

The loss is narrow and real: no row changes and the presser is audited at the FGA gate, but `WROTE`
is what answers "what touched this table at 03:00?". `COMPACT_TABLE`'s own definition in
`catalog.core.lineage_emit` states the obligation — recorded "for the same reason the index ops are —
the version moved" — and the queued lane, the scheduled sweep and the sibling `/compaction_commit`
door all honour it. The in-pod lane was the one exception, and nothing anywhere recorded the silence
as intended.

DERIVED FROM THE MOUNTED ROUTES, like the branch-refusal gate beside it: every POST under
`/{id}/maintenance/` must be classified here as either minting a version or not. A new verb that is
neither fails this file rather than inheriting whichever answer happens to be convenient — which is
how the reindex door came to repeat the compact door's silence on the day it was added.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from catalog.api.v1.endpoints import maintenance


#: Verbs whose in-pod lane COMMITS A NEW VERSION. Lance mints one for a fragment rewrite and one for an
#: index replace, so each is a version an operator can be asked to account for.
_MINTS_A_VERSION = ("/v1/table/{id}/maintenance/compact", "/v1/table/{id}/maintenance/reindex")

#: Verbs that mint no version, with the reason, because an unexplained exemption is how a door quietly
#: stops being checked. `preview` never mutates. `run` reclaims OLD versions and creates none — whether
#: reclaiming history deserves its own event is a separate question with no operation in
#: `catalog.core.lineage_emit` to carry it, and is deliberately not decided by this gate.
_MINTS_NO_VERSION = ("/v1/table/{id}/maintenance/preview", "/v1/table/{id}/maintenance/run")


def _maintenance_handlers() -> list[tuple[str, Callable[..., Any]]]:
    found: list[tuple[str, Callable[..., Any]]] = []
    for route in maintenance.router.routes:
        path = getattr(route, "path", "")
        endpoint = getattr(route, "endpoint", None)
        if "POST" in getattr(route, "methods", set()) and "/v1/table/{id}/maintenance/" in path and callable(endpoint):
            found.append((path, endpoint))
    return found


def test_every_mounted_maintenance_verb_is_classified() -> None:
    """A verb in neither list is unclassified, and an unclassified verb is one nobody decided about."""
    mounted = {path for path, _ in _maintenance_handlers()}
    classified = set(_MINTS_A_VERSION) | set(_MINTS_NO_VERSION)

    assert mounted == classified, f"unclassified: {sorted(mounted - classified)}; classified but not mounted: {sorted(classified - mounted)}"


@pytest.mark.parametrize("path", _MINTS_A_VERSION)
def test_the_in_pod_lane_emits_lineage_for_the_version_it_mints(path: str) -> None:
    """The queued lane's 202 is answered by an executor that emits; the in-pod lane has no such partner.

    Asserted against the shared trailer by name: `emit_measured_write` is what the eleven versioned write
    doors call, and a door that hand-rolled its emit would drift from the single pinned open those doors
    rely on to keep a concurrent writer's schema off this version's edge.
    """
    handler = dict(_maintenance_handlers())[path]
    source = inspect.getsource(handler)

    assert "emit_measured_write" in source, f"{path} rewrites in-pod and records no lineage, so the graph cannot say who did it or when"
