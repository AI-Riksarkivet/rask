"""A dataset the orphan method does not APPLY to is not a half-finished scan.

the lakehouse register, row H10 (drained 2026-09-10; in git history).

MEASURED LIVE 2026-09-08: of the estate's 490 incomplete notes, 419 were refusals by SHAPE (unsupported manifest reader
feature flag 16, base paths) — a permanent property of the manifest, so no later try clears them. A scan therefore
tells two refusals apart:

  * **STRUCTURAL** — an unsupported feature flag, a shallow clone, a branch, a committed overlay, a history past the
    version ceiling. The method does not apply, or not at this cost, and nothing was missed: the dataset is EXCLUDED.
  * **FAILED** — an unreadable manifest, a listing that raised. The method DOES apply and could not be run: a partial
    answer, which a later try may complete.

The per-refusal coverage — that every arm which refuses by shape sets `excluded` — lives beside those refusals in
`tests/unit/test_orphan_files.py`.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.fs as pafs
import pytest

from maintenance.services import orphans


def test_a_history_past_the_version_ceiling_is_an_exclusion_not_a_partial_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling bounds what one scan spends walking one dataset's versions, so a dataset past it is excluded by name
    before any version is read, never walked in part.

    Measured live 2026-10-20 on the previous estate (helm rev 19), two days after a rebuild:
    `s3://lance-catalog/18edda68_provenance$events: 826 versions exceeds the 500 ceiling`.
    """
    monkeypatch.setattr(orphans, "_MAX_VERSIONS", 2)
    uri = str(tmp_path / "busy.lance")
    lance.write_dataset(pa.table({"id": [1]}), uri)
    for _ in range(2):
        lance.write_dataset(pa.table({"id": [1]}), uri, mode="append")

    scan = orphans.scan_dataset(pafs.LocalFileSystem(), uri, prefix=uri)

    assert (scan.checked, scan.excluded, scan.orphans, scan.reason) == (
        False,
        True,
        [],
        f"{uri}: 3 versions exceed the 2-version ceiling, so its unreferenced files were not looked for",
    )
