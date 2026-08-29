"""`parse_range` answers with three TYPED outcomes, not a tuple/str-sentinel/None triple (VS-20).

open_python-audit VS-20 — the classifier returned `tuple[int, int] | str | None`: a tuple when
satisfiable, the module-level string sentinel `IGNORE_RANGE` when the header should be ignored,
and `None` when well-formed-but-unsatisfiable. The caller then decoded that by REBINDING the
header variable (`range_hdr = None  # fall through`) and testing `isinstance(rng, tuple)` — the
control flow lived in a flag mutation rather than in the return type.

Pinned here: the three outcomes are explicit types — a `ByteRange` model for a satisfiable
range and a `RangeVerdict` enum for ignore/416 — so no string can be confused with a verdict
and the caller matches on type instead of rebinding state.
"""

from __future__ import annotations

from viewer.api.v1.endpoints import media as media_ep


def test_a_satisfiable_range_is_a_typed_result_not_a_tuple() -> None:
    rng = media_ep.parse_range("bytes=0-10", 100)
    assert isinstance(rng, media_ep.ByteRange), f"expected a ByteRange, got {type(rng).__name__}"
    assert (rng.start, rng.end) == (0, 10)


def test_an_ignorable_header_is_a_verdict_not_a_string_sentinel() -> None:
    for header in ("pages=1-2", "bytes=0-10, 20-30", "bytes=-", "junk"):
        outcome = media_ep.parse_range(header, 100)
        assert not isinstance(outcome, str), f"{header!r} → a str sentinel again: {outcome!r}"
        assert outcome is media_ep.RangeVerdict.IGNORE, f"{header!r} must be ignored (RFC 9110 §14.2), got {outcome!r}"


def test_an_unsatisfiable_range_is_a_verdict_not_None() -> None:
    for header, total in (("bytes=200-", 100), ("bytes=5-2", 100), ("bytes=0-10", 0)):
        outcome = media_ep.parse_range(header, total)
        assert outcome is media_ep.RangeVerdict.UNSATISFIABLE, f"{header!r}/{total} → {outcome!r}"


def test_the_suffix_form_still_resolves() -> None:
    """`bytes=-N` (last N bytes) — the arithmetic must survive the retyping."""
    rng = media_ep.parse_range("bytes=-10", 100)
    assert isinstance(rng, media_ep.ByteRange)
    assert (rng.start, rng.end) == (90, 99)


def test_the_open_ended_form_still_clamps() -> None:
    rng = media_ep.parse_range("bytes=95-", 100)
    assert isinstance(rng, media_ep.ByteRange)
    assert (rng.start, rng.end) == (95, 99)


def test_the_string_sentinel_is_gone() -> None:
    """The sentinel WAS the finding; a typed verdict beside a surviving sentinel is no fix."""
    assert not hasattr(media_ep, "IGNORE_RANGE")
