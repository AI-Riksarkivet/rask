"""A problem body may carry EXTENSION MEMBERS, and none of them may redefine a member the body's builder owns.

RFC 9457 §3.2 lets a problem detail object carry members beyond the ones it standardises, and tells consumers to ignore
ones they do not recognise. A partly applied operation needs them: its 5xx `detail` is redacted, so what landed of the
caller's own objects can only travel as extensions. The members the builder owns, RFC 9457's, the spec's `code` and
`error`, and the `instance` a span puts there, are what every client parses, and an extension that could overwrite one
would turn a convenience into a way to lie about the status or un-redact the detail.
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lakehouse.problems.envelope import PROBLEM_JSON
from lakehouse.problems.ns_errors import PartiallyAppliedError, install_problem_handlers
from lakehouse.process.correlation import FRAMEWORK_TELEMETRY_OFF


def test_a_lance_error_extension_cannot_name_a_reserved_member() -> None:
    """A partly applied operation's extension members reach the wire, and none of them may redefine a member the spec
    or RFC 9457 reserves, `instance` included while no span is active to put one there."""
    app = FastAPI(telemetry=FRAMEWORK_TELEMETRY_OFF)
    install_problem_handlers(app, logging.getLogger("test"))

    @app.get("/partial")
    async def partial() -> None:
        raise PartiallyAppliedError("half done at s3://b/k", extensions={"instance": "forged", "code": 0, "error": "leaked", "status": 200, "table": "a"})

    response = TestClient(app).get("/partial")

    assert (response.status_code, response.headers["content-type"]) == (503, PROBLEM_JSON)
    assert response.json() == {
        "type": "https://lance.org/problems/partiallyappliederror",
        "title": "PartiallyAppliedError",
        "status": 503,
        "detail": "Internal Server Error",
        "code": 17,
        "error": "Internal Server Error",
        "table": "a",
    }
