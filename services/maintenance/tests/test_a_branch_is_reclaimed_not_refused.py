"""A BRANCH of a protected root is maintained; only the cross-root relations are refused.

THE DISTINCTION IS THE FORMAT'S, NOT THIS ESTATE'S. `lance_docs/file_format.md:2744` — "Each branch
dataset is technically a shallow clone of the source dataset" — is the sentence that made all four
containment relations look alike, and the LAYOUT two lines below it is what separates them:
`tree/{branch_name}/` holds the branch's own `_versions/`, `_transactions/`, `_deletions/` and
`_indices/` and **no `data/`**, so a branch registers no new base path. `:3103` — "When a file's
`base_id` is absent, the file is located relative to the dataset root" — so a branch's inherited files
are the root's own and it introduces no cross-dataset reference at all.

The cross-root case is where Lance disclaims responsibility, in as many words: `:3187`, of a shallow
clone into another root, "Source dataset remains immutable and can be garbage collected
independently". Nothing in the format protects that clone, which is why the estate-wide pre-pass
exists and why the EQUALITY relation must keep refusing. An in-root ref is Lance's own business; a
cross-root base is the catalog's, because the format says it will not do it.

MEASURED against pylance 11.0.0, reclaiming ON the branch with `older_than=0, delete_unverified=True`:
`CleanupStats(old_versions=3, data_files_removed=1, transaction_files_removed=3)`, all of them the
branch's own — the parent's `data/` held 3 files before and 3 after with none removed, and from a COLD
interpreter the parent still read 9 rows and time-travelled to versions 1, 2 and 3. A branch reclaim
cannot reach what the gate protects.

COMPACTION IS A SEPARATE ANSWER AND STAYS REFUSED. A branch manifest DOES register the parent as a
base — measured on the same fixture, `manifest_base_paths` is `[]` on main and `['<parent>']` on the
branch, whose inherited fragments carry `base_id: 0` while only its own new fragment carries `None`.
So the flag-16 compaction gate is right about a branch for the reason it gives (compacting would
materialise the parent's bytes into `tree/<branch>/`), and it is a COST refusal recorded on
`DatasetResult.refused` while the pass continues. Reclamation is the step this row is about.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa

from maintenance.services.optimize import compact_one
from service_kit.lakehouse.base_refs import BaseRefs, containment_of


def _parent_with_branch(tmp_path: Path) -> tuple[str, str]:
    """A parent someone else references, plus a branch of it that has its own version to reclaim."""
    src = str(tmp_path / "parent.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2, 3], pa.int64())}), src, mode="create")
    lance.write_dataset(pa.table({"id": pa.array([4, 5, 6], pa.int64())}), src, mode="append")

    lance.dataset(src).create_branch("work")
    branch = lance.dataset(src).checkout_version(("work", None))
    lance.write_dataset(pa.table({"id": pa.array([99], pa.int64())}), branch, mode="append")
    return src, f"{src}/tree/work"


def test_a_branch_of_a_protected_root_is_NOT_refused(tmp_path: Path) -> None:
    """The refusal ladder's own diagnosis says `branch`; the pass must then run, not decline.

    Half of everything the live sweep refused was this relation — 105 `branch` against 94 `is` over a
    10-minute window on 2026-09-17 — so a refusal here is not an edge case, it is most of the disk the
    estate cannot reclaim.
    """
    src, branch_uri = _parent_with_branch(tmp_path)
    protected = BaseRefs(protected={src.lstrip("/")})
    assert containment_of(branch_uri, src) == "branch", "the fixture stopped being the case under test"

    result = compact_one(branch_uri, {}, timedelta(seconds=0), protected=protected)

    assert result.old_versions_removed > 0, "the branch's own versions were not reclaimed"
    assert result.bytes_removed > 0


def test_the_parent_keeps_its_data_and_its_history(tmp_path: Path) -> None:
    """The permit is only sound if reclaiming the child cannot reach the parent.

    Asserted on BOTH halves, because a data-file count alone would pass a pass that deleted the
    parent's manifests: the parent's `data/` is byte-identical in membership, and every version it had
    still opens and still answers the row count it answered before.
    """
    src, branch_uri = _parent_with_branch(tmp_path)
    data_dir = Path(src) / "data"
    before = sorted(p.name for p in data_dir.iterdir())
    history = {v["version"]: lance.dataset(src, version=v["version"]).count_rows() for v in lance.dataset(src).versions()}

    compact_one(branch_uri, {}, timedelta(seconds=0), protected=BaseRefs(protected={src.lstrip("/")}))

    assert sorted(p.name for p in data_dir.iterdir()) == before, "reclaiming the branch removed the PARENT's data files"
    assert {v["version"]: lance.dataset(src, version=v["version"]).count_rows() for v in lance.dataset(src).versions()} == history


def test_the_root_ITSELF_is_still_refused(tmp_path: Path) -> None:
    """The half that must not move. `file_format.md:3187` says the format will garbage-collect a
    source out from under its clone, so equality is the relation the estate-wide pre-pass exists for.
    """
    src, _branch = _parent_with_branch(tmp_path)

    result = compact_one(src, {}, timedelta(seconds=0), protected=BaseRefs(protected={src.lstrip("/")}))

    assert result.refused is not None
    assert "another dataset resolves its files through" in result.refused
    assert result.old_versions_removed == 0, "the referenced root was reclaimed anyway"
    assert result.bytes_removed == 0
