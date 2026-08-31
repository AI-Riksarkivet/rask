"""Read-only maintenance mode must refuse WRITES, not the entire read surface.

THE DEFECT. `is_mutating` classifies by HTTP METHOD — "anything but GET/HEAD/OPTIONS". That is the
right rule for an ordinary REST API and the wrong one here, because the Lance Namespace route grammar
is `POST /v1/<object>/{id}/<action>` and the spec puts the ACTION in the path, not in the verb. So
`describe`, `exists`, `count_rows`, `query`, `stats`, `version/list`, `tags/list`, `index/list`,
`policy/describe` and `access/my-permissions` are all POSTs.

Flipping `LANCE_MAINTENANCE_READ_ONLY=true` therefore does not produce a read-only catalog. It
produces an UNREADABLE one: every consumer's describe and count 503s while the feature's whole purpose
is to keep serving reads during a maintenance window. A safety switch that causes the outage it exists
to avoid is worse than not having it.

THE FIX IS FAIL-CLOSED, and that direction matters. Actions are classified from a READ allowlist, so
an action nobody has classified is treated as a write and refused. A new spec operation added tomorrow
is blocked by default rather than silently let through a maintenance window.
"""

from __future__ import annotations

import pytest

from catalog.api.maintenance_mode import is_mutating


#: Spec reads that are served as POST because the grammar puts the action in the path.
READS = [
    "/v1/table/pages/describe",
    "/v1/table/pages/exists",
    "/v1/table/pages/count_rows",
    "/v1/table/pages/stats",
    "/v1/table/pages/version/list",
    "/v1/table/pages/tags/list",
    "/v1/table/pages/index/list",
    "/v1/table/pages/policy/describe",
    "/v1/table/pages/access/my-permissions",
    "/v1/namespace/acme/describe",
    "/v1/namespace/acme/exists",
]

#: Writes, which must keep being refused.
WRITES = [
    "/v1/table/pages/create",
    "/v1/table/pages/insert",
    "/v1/table/pages/update",
    "/v1/table/pages/delete",
    "/v1/table/pages/drop",
    "/v1/table/pages/publish",
    "/v1/table/pages/tags/create",
    "/v1/table/pages/tags/delete",
    "/v1/table/pages/merge_insert",
    "/v1/namespace/acme/create",
    "/v1/namespace/acme/drop",
]


@pytest.mark.parametrize("path", READS)
def test_a_POST_that_only_READS_survives_a_maintenance_window(path: str) -> None:
    assert not is_mutating("POST", path), f"{path} is a READ served as POST — refusing it makes the catalog unreadable, not read-only"


@pytest.mark.parametrize("path", WRITES)
def test_a_POST_that_WRITES_is_still_refused(path: str) -> None:
    assert is_mutating("POST", path), f"{path} writes and must not pass a read-only window"


def test_an_UNCLASSIFIED_action_is_treated_as_a_write() -> None:
    """Fail-closed: a spec operation nobody has classified must not slip through the window.

    The opposite default would make every future write op a silent hole, discovered only when it
    corrupts something during a maintenance window.
    """
    assert is_mutating("POST", "/v1/table/pages/some_future_operation")


def test_ordinary_verbs_still_decide_where_there_is_no_action() -> None:
    """The method rule is not wrong, only insufficient — it still governs the non-spec surface."""
    assert not is_mutating("GET", "/v1/table")
    assert is_mutating("DELETE", "/v1/table/pages")
    assert is_mutating("PUT", "/v1/anything")
