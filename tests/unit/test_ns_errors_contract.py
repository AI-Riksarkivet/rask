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

from lance_namespace import ErrorCode, ServiceUnavailableError, UnsupportedOperationError
from lance_namespace.errors import InternalError

from service_kit.lakehouse.ns_errors import _STATUS, problem_detail, status_for


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


def test_an_unsupported_answer_keeps_its_message_because_it_is_a_capability_answer() -> None:
    """An Unsupported message names an operation and leaks nothing — it is the one thing that tells the caller to
    stop asking. The blanket ``>= 500`` redaction replaced it with "Internal Server
    Error", so a user pressing the lakehouse's backfill button was told the server had broken (#101)."""
    status, body = problem_detail(UnsupportedOperationError("alter_table_backfill_columns not implemented"))
    assert (
        status == 406
    )  # 406 since Q3 (2026-09-02): the spec's UnsupportedOperationErrorResponse is 406, and Lance's own reference server maps ErrorCode::Unsupported to NOT_ACCEPTABLE.
    assert body["detail"] == "alter_table_backfill_columns not implemented"
    assert body["error"] == body["detail"]  # the spec-0.9 twin field carries the same text


def test_every_other_5xx_is_still_redacted() -> None:
    """The negative twin. Unsupported is 4xx since Q3, so its detail survives by the ordinary 4xx rule and
    needs no exemption at all — but a genuine 5xx fault's message carries paths, DSNs and driver text,
    and that redaction is what this pins."""
    for exc in (InternalError("psycopg: connection to 10.0.0.4:5432 failed"), ServiceUnavailableError("s3://rask-work unreachable")):
        _, body = problem_detail(exc)
        assert body["detail"] == "Internal Server Error", body
        assert body["error"] == "Internal Server Error", body
