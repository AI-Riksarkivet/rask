"""The 24-code error contract stays COMPLETE — found incomplete by the 2026-08-04 skills audit.

`_STATUS` mapped 22 codes while the SDK's ``ErrorCode`` has 24: ``TABLE_BRANCH_NOT_FOUND`` (22) and
``TABLE_BRANCH_ALREADY_EXISTS`` (23) landed with the branch ops and were never added, so
``status_for`` fell to its ``default 500`` — a missing branch answered "Internal Server Error" on
endpoints rask actually ships (`catalog/api/v1/endpoints/branches.py`). No test failed, because no
test pinned the mapping against the ENUM; the skill even claimed "22 codes, all mapped", and both
were wrong together.

The completeness pin below is the real fix: the next code the spec adds fails a test here instead
of failing a client with a 500.
"""

from __future__ import annotations

from lance_namespace import ErrorCode

from service_kit.lakehouse.ns_errors import _STATUS, status_for


def test_every_sdk_error_code_is_mapped() -> None:
    """No ``ErrorCode`` may fall to the 500 default — that default exists for codes the SDK does
    not know, not for codes it does."""
    unmapped = [code.name for code in ErrorCode if code not in _STATUS]
    assert unmapped == [], f"ErrorCode members with no HTTP status (they answer 500): {unmapped}"


def test_branch_codes_carry_the_statuses_their_meaning_requires() -> None:
    """The two codes the audit found missing, pinned by VALUE so an enum rename cannot hide a
    remap: not-found is 404, already-exists is 409 — same as every other not-found/conflict pair
    in the table."""
    assert status_for(22) == 404  # TABLE_BRANCH_NOT_FOUND
    assert status_for(23) == 409  # TABLE_BRANCH_ALREADY_EXISTS


def test_an_unknown_code_still_defaults_to_500() -> None:
    """The default survives for genuinely unknown codes — completeness must not make the map
    brittle against a FUTURE spec running ahead of this SDK pin."""
    assert status_for(9999) == 500
