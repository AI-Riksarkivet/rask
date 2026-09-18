"""An `IndexWorkItem` names the REF its index is built on, and two refs are two units.

[[LH-019]]. The catalog's `maintenance/reindex` door refused `branch` outright, and the refusal was
correct while this model could not carry one: the door publishes a unit and answers 202, so the
WORKER opens the dataset. Accepting `branch` at the door without carrying it here would have the
worker rebuild MAIN's index while the API reported the branch's — the wrong-but-plausible answer
that row exists to remove.

`uri` cannot express it. A branch lives at `tree/{branch}/` with its own `_versions/` and no `data/`
(`lance_docs/file_format.md:2746-2761`), so it is not openable by path; the ref has to be checked out
from the dataset the URI names. That is why this is a field rather than a spelling of the URI.

THE UNIT ID IS THE SECOND HALF, and it is the half that is easy to miss. `unit_id` is answered to the
caller as the spec's `transaction_id` and is derived so a redelivered publish names the same unit. If
it ignored the ref, rebuilding one index on `main` and on `work` would produce ONE id for two
different builds — and a caller following `DescribeTableIndexStats` would be told about whichever
landed last.
"""

from __future__ import annotations

from typing import Any

from service_kit.lakehouse.work_items import IndexWorkItem


def _unit(**over: Any) -> IndexWorkItem:
    base: dict[str, Any] = {"uri": "s3://wh/ns$tbl", "table_id": "ns$tbl", "column": "vec", "index_type": "IVF_PQ", "name": "vec_idx"}
    return IndexWorkItem(**(base | over))


def test_a_unit_carries_the_branch_it_was_published_for() -> None:
    assert _unit(branch="work").branch == "work"


def test_a_unit_with_no_branch_means_main() -> None:
    """Absent, not `"main"` — the same convention every other door here uses, so a producer that
    never heard of branches keeps publishing exactly what it published before."""
    assert _unit().branch == ""


def test_two_refs_are_two_units() -> None:
    """Otherwise one `transaction_id` describes two builds and the caller follows the wrong one."""
    assert _unit(branch="work").unit_id != _unit().unit_id, "a branch build and a main build of the same index share one unit id"


def test_two_branches_are_two_units() -> None:
    assert _unit(branch="work").unit_id != _unit(branch="other").unit_id


def test_the_same_request_still_names_the_same_unit() -> None:
    """The property `unit_id` exists for: a redelivered publish must not look like a second build."""
    assert _unit(branch="work").unit_id == _unit(branch="work").unit_id
