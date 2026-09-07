"""`base_id = 0` is a REAL external base, and a truthy test reads it as none at all.

`open_lakehouse_diff_left.md` § C1. `vending.has_external_bases` decides whether a table may be
DIRECT-vended, and its own docstring states the stake: *"Such a table cannot be safely direct-vended:
the STS session policy is scoped to the primary root bucket only, so a data-base fragment would be
denied at the object store."*

It asked ``any(getattr(df, "base_id", None) for ...)`` — a TRUTHY test. Measured on pylance 10.0.0::

    plain dataset, its own files      base_id = None    falsy
    SHALLOW CLONE, files in the base  base_id = 0       falsy   <-- the case the check exists for
    branch, files in the parent root  base_id = 0       falsy

`None` means "this file lives under my own root"; `0` means "`base_paths[0]`" — a genuinely external
base. A truthy test cannot tell them apart, so the guard returned False for the canonical multi-base
shape and the table was direct-vended with a session policy that cannot reach where its bytes are.

That is this estate's recurring failure with an off-by-falsy twist: the control's NAME is present, its
enforcement is not, and nothing is red because the vend SUCCEEDS — the denial happens later, at the
object store, to whoever used the credential.

Driven against real datasets rather than a stub, because the subject is what pylance puts in
`base_id` — a double would only assert that this file and the fix agree with each other.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from catalog.core.vending import has_external_bases


lance = pytest.importorskip("lance")


def _rows() -> pa.Table:
    return pa.table({"id": pa.array(range(50), pa.int64())})


def test_a_shallow_clone_is_reported_as_having_external_bases(tmp_path: Path) -> None:
    """The headline: the canonical multi-base shape must not be direct-vendable."""
    source = lance.write_dataset(_rows(), str(tmp_path / "src.lance"))
    source.shallow_clone(str(tmp_path / "clone.lance"), reference=source.version)

    assert has_external_bases(str(tmp_path / "clone.lance"), {}) is True, (
        "a shallow clone was reported as having no external bases, so it would be DIRECT-vended with a "
        "session policy scoped to its own root — the fragments live in the source and would be denied"
    )


def test_a_plain_dataset_is_still_direct_vendable(tmp_path: Path) -> None:
    """The other direction, and the one that keeps the fix from being a blanket refusal.

    A dataset whose files are its own carries `base_id = None`. Reporting it as multi-base would refuse
    direct vending for every ordinary table in the estate — a far worse outcome than the bug.
    """
    lance.write_dataset(_rows(), str(tmp_path / "plain.lance"))

    assert has_external_bases(str(tmp_path / "plain.lance"), {}) is False, (
        "an ordinary dataset was reported as multi-base — direct vending would be refused estate-wide"
    )


def test_an_unopenable_location_is_not_reported_as_multi_base(tmp_path: Path) -> None:
    """The existing contract, pinned so the fix cannot change it by accident: an open that fails
    answers False rather than raising, and the caller's own existence checks report the real problem."""
    assert has_external_bases(str(tmp_path / "does-not-exist.lance"), {}) is False
