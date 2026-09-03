"""The maintenance plane finds datasets by URI and the credential door needs an identifier.

Only the flat layout answers, and the cases that must NOT answer matter more than the ones that do: a
guessed identifier vends a credential for a different table, and the failure then surfaces as a 403 on
the table being maintained — naming nothing about the guess.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.table_locations import table_id_from_location


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("s3://acme-bucket/4c49d010_acme-bronze$vendproof4", "acme-bronze$vendproof4"),
        ("s3://acme-bucket/4750a5b9_acme-bronze$events", "acme-bronze$events"),
        # No uuid prefix — the catalog is not the only writer of this layout.
        ("s3://bucket/silver$features", "silver$features"),
        # A namespace carrying its own underscore must survive: `transcripts` is not hex, so the
        # prefix-strip must decline. Getting this wrong yields `v2$t1`, which names no table.
        ("s3://bucket/transcripts_v2$t1", "transcripts_v2$t1"),
        ("s3://bucket/aa3bed10_transcripts_v2$t1", "transcripts_v2$t1"),
        ("s3://bucket/4750a5b9_acme-bronze$events.lance", "acme-bronze$events"),
    ],
)
def test_the_flat_layout_yields_its_identifier(uri: str, expected: str) -> None:
    assert table_id_from_location(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        # Nested layouts: the leaf is a table and its namespace is a DIRECTORY, which the catalog may
        # render differently. Answering here would vend for the wrong table.
        "s3://bucket/acme-bronze/events",
        "s3://bucket/medallion/bronze",
        "s3://bucket/medallion/bronze-media/pages",
        # Not a dataset location at all.
        "s3://bucket",
        "",
        # A leaf with a delimiter but nothing on one side of it.
        "s3://bucket/aa3bed10_$events",
        "s3://bucket/aa3bed10_ns$",
    ],
)
def test_an_unanswerable_location_answers_none_rather_than_guessing(uri: str) -> None:
    assert table_id_from_location(uri) is None
