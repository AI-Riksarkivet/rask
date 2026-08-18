"""The publish gate must scan the version it is tagging, and it scans whatever is latest instead.

`publication.publish` pins the candidate and says so in a comment — "Gate the version being published,
not `latest`: they differ the moment another writer commits while this gate runs, and publishing a
version nobody checked is the whole failure this prevents." It then hands `assert_quality` only
`candidate.uri`, and `assert_quality` re-opens the dataset with no version at all. The pin is
discarded at the string boundary, so the comment describes an intent the code does not carry out.

Both directions are wrong, and the second is the dangerous one:

  * a CLEAN version is refused because a later dirty one exists — noisy, safe;
  * a DIRTY version is published because a later clean one exists — silent, and it moves the
    `published` tag onto data nothing checked, which is precisely what the gate exists to stop.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from service_kit.lakehouse.quality import assert_quality, passed


@pytest.fixture
def clean_then_dirty(tmp_path: Path) -> str:
    """v1 has no nulls; v2 does. Latest is dirty."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), uri)
    lance.write_dataset(pa.table({"id": [4, None, 6]}), uri, mode="append")
    return uri


@pytest.fixture
def dirty_then_clean(tmp_path: Path) -> str:
    """v1 has nulls; v2 does not. Latest is clean — the version that hides a bad one."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": [1, None, 3]}), uri)
    lance.write_dataset(pa.table({"id": [4, 5, 6]}), uri, mode="overwrite")
    return uri


class TestItScansTheVersionItIsAsked_About:
    def test_a_clean_version_passes_even_when_LATEST_is_dirty(self, clean_then_dirty: str) -> None:
        assert passed(assert_quality(clean_then_dirty, {}, key_column="id", version=1))

    def test_a_DIRTY_version_is_refused_even_when_LATEST_is_clean(self, dirty_then_clean: str) -> None:
        """The silent one. Without the pin the gate scans the good latest version and moves the
        `published` tag onto data that has a null key."""
        assertions = assert_quality(dirty_then_clean, {}, key_column="id", version=1)

        assert not passed(assertions)
        assert [a.assertion for a in assertions if not a.success] == ["not_null"]

    def test_row_counts_come_from_the_named_version_too(self, tmp_path: Path) -> None:
        """`row_count_positive` is the other assertion a wrong version silently answers for."""
        uri = str(tmp_path / "e.lance")
        lance.write_dataset(pa.table({"id": pa.array([], type=pa.int64())}), uri)
        lance.write_dataset(pa.table({"id": [1, 2]}), uri, mode="append")

        assert not passed(assert_quality(uri, {}, key_column="id", version=1))
        assert passed(assert_quality(uri, {}, key_column="id", version=2))


class TestTheDefaultIsUnchanged:
    def test_no_version_still_means_latest(self, clean_then_dirty: str) -> None:
        """The mover calls this right after writing, where latest IS its write. Changing that default
        would move a caller this fix is not about."""
        assert not passed(assert_quality(clean_then_dirty, {}, key_column="id"))

    def test_an_unknown_version_raises_rather_than_scanning_something_else(self, clean_then_dirty: str) -> None:
        """Silently falling back to latest is how the original defect reads from the outside."""
        with pytest.raises(ValueError, match=r"99\.manifest"):
            assert_quality(clean_then_dirty, {}, key_column="id", version=99)
