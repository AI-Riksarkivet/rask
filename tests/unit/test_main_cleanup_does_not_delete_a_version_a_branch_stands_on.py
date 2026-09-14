"""Main cleanup and a branch rooted in main's history — measured against pylance, not assumed.

[[LH-112]] asked whether Lance honours a tag that pins a BRANCH version during MAIN cleanup, and called
it "unmeasured, and load-bearing as soon as the sweep maintains branches". It is measured here, and the
answer is sharper than the question: **the branch REFERENCE is what protects the version, and the tag
plays no part in it.**

Measured on pylance 11.0.0, 2026-09-14. A dataset at main v1..v5 with a branch rooted at v2, then
`cleanup_old_versions(older_than=0)` — the most aggressive form there is:

    main versions after cleanup : [2, 5]      (not [5], which is what an unprotected run leaves)
    branch                      : opens [2, 3], 3 rows
    tag                         : still resolves, {'branch': 'work', 'version': 3}

and the run WITH a tag and the run WITHOUT one give byte-identical results. So the retention of main v2
is structural — Lance keeps a version a branch stands on — and tagging neither adds nor is needed for
that protection.

WHY BOTH ARMS ARE IN THE TEST rather than just the reassuring one. With only the tagged arm this would
pass while crediting the tag for a guarantee the branch reference provides, which is the shape of a
control that looks like it fired and did not. The untagged arm is what distinguishes them, and it is
the assertion that would catch a future pylance making branch protection depend on tagging.

WHY `older_than=0` AND NOT A REALISTIC WINDOW: the question is whether cleanup CAN delete the version,
so the test must actually try. A window that spares everything would pass on a Lance that protects
nothing.

TAGS ARE ROOT-SCOPED, which is worth knowing before reading `tags.list()` anywhere: a tag created on a
branch is visible from MAIN and carries the branch name with it — `{'branch': 'work', 'version': 3}` —
so "the dataset's tags" is one namespace spanning every branch, not a per-branch list.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pytest


def _dataset_with_branch(root: Path, *, tag_it: bool) -> tuple[str, str]:
    """Main at v1..v5 with a branch `work` rooted at v2, optionally tagged. Returns (main, branch) uris."""
    uri = str(root / "t.lance")
    dataset = lance.write_dataset(pa.table({"id": [1]}), uri)
    for row in range(2, 6):
        dataset = lance.write_dataset(pa.table({"id": [row]}), uri, mode="append")

    branch = dataset.create_branch("work", reference=2)
    written = lance.write_dataset(pa.table({"id": [99]}), branch.uri, mode="append")
    if tag_it:
        written.tags.create("pinned", written.version)
    return uri, str(root / "t.lance" / "tree" / "work")


@pytest.mark.parametrize("tag_it", [True, False], ids=["tagged", "untagged"])
def test_main_cleanup_keeps_the_version_the_branch_is_rooted_at(tmp_path: Path, tag_it: bool) -> None:
    """The version a branch stands on survives the most aggressive cleanup main can run.

    Both arms assert the same outcome ON PURPOSE: that equality is the finding. If a future pylance made
    this protection depend on a tag, the `untagged` arm goes red and the `tagged` one does not — which
    is precisely the distinction this row existed to establish.
    """
    main_uri, branch_uri = _dataset_with_branch(tmp_path, tag_it=tag_it)

    lance.dataset(main_uri).cleanup_old_versions(older_than=datetime.timedelta(seconds=0))

    after = [v["version"] for v in lance.dataset(main_uri).versions()]
    assert 2 in after, f"main cleanup deleted v2, the version the branch is rooted at: {after}"
    assert 5 in after, f"main's own tip is gone: {after}"

    branch = lance.dataset(branch_uri)
    assert [v["version"] for v in branch.versions()] == [2, 3], branch.versions()
    assert branch.count_rows() == 3, "the branch reads, but not the rows it held"


def test_cleanup_really_does_remove_unprotected_versions(tmp_path: Path) -> None:
    """The control, and without it the test above passes on a cleanup that deletes nothing at all.

    Same shape, no branch: `older_than=0` must strip main back to its tip.
    """
    uri = str(tmp_path / "plain.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for row in range(2, 6):
        lance.write_dataset(pa.table({"id": [row]}), uri, mode="append")

    lance.dataset(uri).cleanup_old_versions(older_than=datetime.timedelta(seconds=0))

    after = [v["version"] for v in lance.dataset(uri).versions()]
    assert after == [5], f"cleanup left more than the tip, so the branch test proves nothing: {after}"


def test_a_branch_tag_is_visible_from_MAIN_and_names_its_branch(tmp_path: Path) -> None:
    """Tags are ROOT-scoped. A reader listing "the dataset's tags" sees every branch's, each carrying the
    branch it belongs to — so a caller that treated `tags.list()` as main's own would act on a pin that
    belongs to a branch it is not looking at."""
    main_uri, _ = _dataset_with_branch(tmp_path, tag_it=True)

    tags = lance.dataset(main_uri).tags.list()

    assert "pinned" in tags, tags
    assert tags["pinned"]["branch"] == "work", tags["pinned"]
    assert tags["pinned"]["version"] == 3, tags["pinned"]


def test_the_branch_tag_survives_main_cleanup(tmp_path: Path) -> None:
    """The row's literal question. The tag is not merely unharmed — it still RESOLVES afterwards, which
    is the part that matters to anyone who pinned something."""
    main_uri, _ = _dataset_with_branch(tmp_path, tag_it=True)

    lance.dataset(main_uri).cleanup_old_versions(older_than=datetime.timedelta(seconds=0))

    tags = lance.dataset(main_uri).tags.list()
    assert tags.get("pinned", {}).get("version") == 3, f"the branch tag did not survive main cleanup: {tags}"
