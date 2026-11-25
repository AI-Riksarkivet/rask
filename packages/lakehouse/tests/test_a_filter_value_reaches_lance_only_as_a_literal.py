"""A value reaches a Lance filter only as a literal (`lakehouse.lancekit.filters.lance_filter`): the template's text is the
filter, and each value it interpolates is read by pylance itself as a literal that matches only that value."""

from pathlib import Path
from string.templatelib import Template

import lance
import pyarrow as pa
import pytest

from lakehouse.lancekit.filters import lance_filter


KEYS = ["plain", "O'Brien", "ends\\"]


@pytest.fixture
def table(tmp_path: Path) -> lance.LanceDataset:
    return lance.write_dataset(pa.table({"k": KEYS, "n": list(range(len(KEYS)))}), tmp_path / "t")


@pytest.mark.parametrize(
    ("template", "matched"),
    [
        pytest.param(t"k = {KEYS[1]}", ["O'Brien"], id="a-quote-is-doubled"),
        pytest.param(t"k = {KEYS[2]}", ["ends\\"], id="a-backslash-escapes-nothing"),
        pytest.param(t"n = {2}", ["ends\\"], id="an-int-is-its-digits"),
        pytest.param(t"k IN ({[KEYS[1], KEYS[0]]})", ["plain", "O'Brien"], id="a-list-is-an-in-list"),
    ],
)
def test_a_filter_value_matches_only_itself(table: lance.LanceDataset, template: Template, matched: list[str]) -> None:
    assert table.to_table(filter=lance_filter(template)).column("k").to_pylist() == matched


@pytest.mark.parametrize(
    ("template", "refusal", "match"),
    [
        pytest.param(t"flag = {True}", TypeError, "not a bool", id="a-bool"),
        pytest.param(t"n = {1.5}", TypeError, "not float", id="a-value-of-another-type"),
        pytest.param(t"k = {KEYS[0]!r}", ValueError, "no conversion", id="a-conversion"),
        pytest.param(t"k = {KEYS[0]:>9}", ValueError, "format spec", id="a-format-spec"),
        pytest.param(t"k IN ({[]})", ValueError, "at least one value", id="an-empty-list"),
        pytest.param(t"k IN ({[KEYS[0], 1]})", TypeError, "strings only", id="a-list-holding-a-non-string"),
    ],
)
def test_lance_filter_refuses_a_value_it_cannot_spell_as_a_literal(template: Template, refusal: type[Exception], match: str) -> None:
    with pytest.raises(refusal, match=match):
        lance_filter(template)
