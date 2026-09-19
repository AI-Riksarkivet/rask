"""A project id the catalog would never MINT cannot be CONSUMED by the planes downstream of it.

[[LH-069]]. Two rules described the same identifier and disagreed: the catalog mints against
``[a-z0-9][a-z0-9-]{1,61}[a-z0-9]`` (lowercase, DNS-safe, 3-63, no trailing hyphen), while
``warehouse_registry.PROJECT_PATTERN`` admitted ``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`` — any case,
underscores, one character, a trailing hyphen. The gap was not theoretical: ``is_safe_project`` is
what the catalog's lineage emit, the medallion's ingest trigger and transform, and ingest's name
composition all ask before putting a project into an S3 key prefix, a Lance dataset URI and a lineage
namespace qualifier. Anything the loose rule admitted and the strict one refuses is an id those planes
would accept and the control plane could never have issued — an id with no project behind it.

MEASURED BEFORE TIGHTENING, because a shape rule is only safe to narrow against the ids that exist:
all 93 projects registered on the deployed estate (2026-09-19, ``GET /v1/projects``) already satisfy
the mint rule, and none of them exercises the difference. The row was parked on an owner decision to
"adopt or revoke the pre-registry ghost ids"; the measurement is what dissolved it — there are none.

THE ANCHOR DIFFERS BETWEEN THE TWO USES AND THAT IS DELIBERATE, not drift. The compiled guard anchors
with ``\\Z`` and matches with ``fullmatch``; the pattern string ``PROJECT_PATTERN`` is published on the
wire as an OpenAPI ``pattern`` (``medallion.api.produce_auth.ProjectParam``), where the consumer is
pydantic's Rust regex and a JSON Schema reader, neither of which knows ``\\Z``. So the wire string is
``$``-anchored and the enforcement never relies on it.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.naming import CONTROL_ID_PATTERN
from service_kit.lakehouse.warehouse_registry import PROJECT_PATTERN, is_safe_project


#: Ids the loose rule admitted that the catalog could never have minted.
_UNMINTABLE = [
    pytest.param("ACME", id="uppercase"),
    pytest.param("acme_bronze", id="underscore"),
    pytest.param("a", id="one-char"),
    pytest.param("ab", id="two-chars-below-the-floor"),
    pytest.param("acme-", id="trailing-hyphen"),
]


@pytest.mark.parametrize("value", _UNMINTABLE)
def test_an_id_the_catalog_could_not_mint_is_not_consumed(value: str) -> None:
    assert is_safe_project(value) is False


@pytest.mark.parametrize("value", ["acme", "a72fcc0f", "acme-bronze", "a" * 63])
def test_a_real_registered_id_still_passes(value: str) -> None:
    """The control. Without it, a rule that refused everything would pass above."""
    assert is_safe_project(value) is True


def test_the_wire_pattern_is_the_mint_pattern() -> None:
    """ONE constant, not two strings that happen to agree today — the whole failure mode of this row."""
    assert rf"^{CONTROL_ID_PATTERN}$" == PROJECT_PATTERN


def test_the_wire_pattern_carries_no_python_only_anchor() -> None:
    """``\\Z`` on the wire is unparseable to pydantic's Rust regex and to any JSON Schema reader."""
    assert "\\Z" not in PROJECT_PATTERN


def test_the_guard_does_not_lean_on_the_wire_anchor() -> None:
    """``$`` matches before a trailing newline in Python, and that newline becomes a registry filename."""
    assert is_safe_project("acme\n") is False
