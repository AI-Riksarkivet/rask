"""A commit's or a lifecycle change's run id is derived, not stored: a finish, a retry or a replica after a deploy derives
it again and must reach the id the first announcement carried, or lineage records one change as two runs. The ids below
are what lance-rest-catalog cef22fbb0 derives (version and variant bits set by hand), so a change to the derivation that
moves any id fails here."""

from collections.abc import Callable

import pytest

from catalog.core.commit_ids import commit_run_id, lifecycle_run_id


KEY = b"k" * 32


@pytest.mark.parametrize(
    ("derived", "released"),
    [
        pytest.param(
            lambda: commit_run_id(KEY, table_uuid="t-1", branch=None, branch_identifier=None, version=7),
            "bc05f6ca-2cc0-8d2d-bda7-616dcc079c7c",
            id="a-commit-on-main",
        ),
        pytest.param(
            lambda: commit_run_id(KEY, table_uuid="t-1", branch="work", branch_identifier="b-1", version=7),
            "fb5cd53d-2d7d-83dc-9e97-65df731e60de",
            id="a-commit-on-a-branch",
        ),
        pytest.param(
            lambda: lifecycle_run_id(KEY, table_uuid="t-1", operation="drop", announced_at="2026-11-25T00:00:00Z"),
            "4fef917e-c4e3-8f92-b304-13b60b906846",
            id="a-lifecycle-change",
        ),
    ],
)
def test_a_run_id_derives_to_the_id_the_released_catalog_announced(derived: Callable[[], str], released: str) -> None:
    assert derived() == released
