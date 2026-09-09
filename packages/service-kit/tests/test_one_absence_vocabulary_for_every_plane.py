"""ONE answer to "is this Lance target ABSENT, or did we merely fail to look?", for every plane.

Four seams ask this question and each carried its own marker list, which is the estate's recurring
shape: a convention one path holds and another does not. Measured on pylance 11.0.0 against a stub S3
endpoint returning each error verbatim, the four disagreed on the SAME failure — a 404 from the object
store:

    catalog  `dataplane._ABSENCE_MARKERS`  -> ABSENT  (matched "not found" AND "does not exist")
    ingest   `_reads_as_absent`            -> ABSENT  (matched "not found")
    lineage  `read_table_version`          -> unreadable, correctly, and says why in its own comment
    catalog  `plan_compaction`             -> ABSENT, and it does not use its own module's markers

`"not found"` is the trap: it matches the HTTP STATUS LINE (`404 Not Found`) that pylance embeds in
every non-2xx object-store error, so it proves nothing about the dataset. `"does not exist"` matches
S3's `NoSuchBucket` body verbatim. Both make "the bucket is gone" indistinguishable from "this table
was never written".
"""

from __future__ import annotations

import pytest

from service_kit.lancekit.absence import reads_as_absent


#: Verbatim pylance 11.0.0 wording, captured from a stub S3 endpoint answering each status. The
#: bodies are S3's own; the `LanceError(IO): Generic S3 error` wrapper and the trailing Rust source
#: location are pylance's.
_S3 = (
    "LanceError(IO): Generic S3 error: Error performing list request: Error performing GET "
    "http://s/b?list-type=2&prefix=live-table%2F_versions%2F in 787.404µs - Server returned non-2xx "
    'status code: {status}: <?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
    "<Message>{message}</Message></Error>, /rust/lance-io/src/object_store.rs:1018:92"
)

ABSENT = [
    pytest.param(
        "Dataset at path b/t was not found: Not found: b/t/_versions, /rust/lance-table/src/io/commit.rs:660:2",
        id="the dataset was never written",
    ),
    pytest.param(
        "Dataset at path /tmp/x.lance version 999 was not found: manifest was not found",
        id="the version is absent (pre-transaction-file history, or GC'd)",
    ),
    pytest.param("No such file or directory (os error 2)", id="a local path that is not there"),
    pytest.param(
        "Append mode must already exist unless create_dataset is true",
        id="an append whose base was never committed",
    ),
]

UNREADABLE = [
    pytest.param(
        _S3.format(status="404 Not Found", code="NoSuchBucket", message="The specified bucket does not exist"),
        id="404 NoSuchBucket — the warehouse bucket is gone, the table is NOT proven absent",
    ),
    pytest.param(
        _S3.format(status="403 Forbidden", code="AccessDenied", message="Access Denied"),
        id="403 — the credential cannot look",
    ),
    pytest.param(
        _S3.format(status="500 Internal Server Error", code="InternalError", message="We encountered an internal error"),
        id="500 — the store is broken",
    ),
    pytest.param('Generic Config error: failed to parse "x" as Duration', id="a malformed storage option"),
    pytest.param("some wording no marker anticipated", id="an unrecognized failure fails CLOSED"),
]


@pytest.mark.parametrize("message", ABSENT)
def test_a_message_that_PROVES_the_target_is_gone_reads_as_absent(message: str) -> None:
    assert reads_as_absent(OSError(message)), f"{message!r} proves absence and was not recognized"


@pytest.mark.parametrize("message", UNREADABLE)
def test_a_message_that_proves_only_that_we_could_not_LOOK_does_not(message: str) -> None:
    assert not reads_as_absent(ValueError(message)), f"{message!r} does not prove absence and was read as absent"


def test_FileNotFoundError_is_definitive_whatever_it_says() -> None:
    """The one typed signal pylance passes through. Message-independent, so it needs no vocabulary."""
    assert reads_as_absent(FileNotFoundError("anything at all"))


def test_the_status_line_alone_never_proves_absence() -> None:
    """THE TRAP, pinned on its own: every non-2xx object-store error carries `404 Not Found` verbatim.

    A marker of `"not found"` matches that status line, so it answers ABSENT for a bucket that was
    deleted, an endpoint pointed at the wrong host, or a proxy 404 — none of which say anything about
    whether the table was written. `_already_committed` acts on this answer to decide whether a run's
    rows are already in the table, and its own contract is that a guard which cannot PROVE the run has
    not committed must not assume it has not.
    """
    assert not reads_as_absent(ValueError("Server returned non-2xx status code: 404 Not Found"))
