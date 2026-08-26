"""Every problem+json body this estate emits must carry the spec's numeric `code`.

open_fastapi-audit — "Seven hand-built problem+json bodies omit the numeric `code` the lance plane's
own error model calls required".

`ns_errors.problem_detail` emits SIX keys — `type,title,status,detail,code,error` — and the last two
are not decoration. `code` is a REQUIRED, no-default field on the generated Lance-Namespace client's
`ErrorResponse` model, so a client validating one of these bodies RAISES; it does not quietly get a
`None`. Seven places built the envelope by hand with four keys.

THE TWO HALVES ARE NOT THE SAME DEFECT, and the audit is right to separate them:

  * The CATALOG sites answer on `/v1/...` routes that generated clients parse. That is the real one.
  * The MEDALLION sites are root-mounted non-spec doors (`/produce`, `/ingest-media`, `/train`) reached
    by the frontend and operators, never by a generated client — envelope inconsistency only. Worth
    fixing anyway, not least because `produce.py` claims "parity with catalog/lineage errors" in a
    comment while emitting four keys.

WHY THE SITES KEEP BUILDING A RESPONSE INSTEAD OF RAISING, which is where this deviates from the
audit's suggested fix. Two of them are pure-ASGI middleware that sit outside `ExceptionMiddleware` and
must answer before the body is buffered — they genuinely cannot raise, and the audit says so. But the
others cannot simply raise either: EVERY one of these sites sets `Retry-After` (5s on the medallion
doors, 60s on catalog maintenance), and `install_problem_handlers`' handler builds a bare
`JSONResponse(status_code=..., content=..., media_type=...)` with no headers at all. Raising would
render the body correctly and silently drop the header — trading a missing `code` for a missing
`Retry-After`, which the reference calls out by name ("clients can't self-throttle, retry storms
hammer the API back into 429"). A generic handler also cannot know that maintenance means 60 and a
draining pod means 5.

So the SHAPE moves to one place — `ns_errors.problem_body` — and each site keeps its own status and
headers. That satisfies the rule the finding actually cites (the envelope must come from one place)
without regressing a documented HTTP behaviour.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import ErrorCode

from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.lakehouse.ns_errors import problem_body


REQUIRED_KEYS = {"type", "title", "status", "detail", "code", "error"}


def test_the_shared_builder_emits_the_full_envelope() -> None:
    body = problem_body(ErrorCode.THROTTLING, status=429, title="TooManyRequests", detail="slow down")
    assert set(body) >= REQUIRED_KEYS, f"problem_body omits {REQUIRED_KEYS - set(body)}"
    assert body["code"] == int(ErrorCode.THROTTLING)
    assert body["error"] == "slow down"


def test_the_body_cap_refusal_carries_a_code() -> None:
    """Pure-ASGI middleware: it must answer before the body is buffered, so it cannot raise."""
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=16)

    @app.post("/write")
    async def write() -> dict[str, str]:
        return {"ok": "yes"}

    response = TestClient(app).post("/write", content=b"x" * 64)
    assert response.status_code == 413
    assert set(response.json()) >= REQUIRED_KEYS, f"413 body omits {REQUIRED_KEYS - set(response.json())}"


def test_the_maintenance_refusal_carries_a_code_and_keeps_retry_after() -> None:
    """Both halves, because either alone would be satisfied by a worse implementation: adding `code`
    by switching to a raise would drop the header the window depends on."""
    from catalog.api.maintenance_mode import maintenance_response

    response = maintenance_response()
    import json

    body = json.loads(bytes(response.body))
    assert set(body) >= REQUIRED_KEYS, f"maintenance body omits {REQUIRED_KEYS - set(body)}"
    assert response.headers["Retry-After"] == "60"
    # Unrenamed: its type has always ended in `/maintenance` while its title is `MaintenanceMode`.
    assert body["type"] == "https://lance.org/problems/maintenance"
    assert body["title"] == "MaintenanceMode"


@pytest.mark.parametrize(
    ("module", "status", "title", "detail"),
    [
        ("medallion.api.train", 503, "ServiceUnavailable", "training trigger publish failed; retry"),
        ("medallion.api.train", 409, "Conflict", "train head not configured"),
    ],
)
def test_the_medallion_doors_carry_a_code(module: str, status: int, title: str, detail: str) -> None:
    """Non-spec doors, so this is envelope consistency rather than a client break — but `produce.py`
    claims parity with the catalog errors in a comment, and this is what that claim costs."""
    import importlib
    import json

    mod = importlib.import_module(module)
    response = mod._problem(status, title, detail)  # noqa: SLF001
    body = json.loads(bytes(response.body))
    assert set(body) >= REQUIRED_KEYS, f"{module} {status} body omits {REQUIRED_KEYS - set(body)}"
    if status == 503:
        assert response.headers["Retry-After"] == "5", "the draining contract's header was dropped"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/v1/validate", 422), ("/v1/boom", 500)],
)
def test_the_installed_handlers_also_carry_a_code(path: str, expected: int) -> None:
    """The finding names seven hand-built sites; the two handlers in `ns_errors` had the same hole.

    They are the bodies a generated client is MOST likely to meet — a 422 on a malformed request and a
    500 on a fault — and both are served on the `/v1` routes those clients call, so `code` being absent
    made them raise exactly as the hand-built four-key bodies did. Fixing the seven and leaving these
    would have left the contract test that pins them passing over a surface that still breaks clients.
    """
    import logging

    from pydantic import BaseModel

    from service_kit.lakehouse.ns_errors import install_problem_handlers

    class _Body(BaseModel):
        n: int

    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))

    @app.post("/v1/validate")
    async def validate(body: _Body) -> dict[str, int]:
        return {"n": body.n}

    @app.get("/v1/boom")
    async def boom() -> None:
        raise RuntimeError("fault")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/v1/validate", json={"n": "not-an-int"}) if expected == 422 else client.get("/v1/boom")

    assert response.status_code == expected
    body = response.json()
    assert set(body) >= REQUIRED_KEYS, f"{path} body omits {REQUIRED_KEYS - set(body)}"
    if expected == 422:
        assert body["errors"], "the field list must survive alongside the envelope"


def test_adding_a_key_did_not_RENAME_the_existing_bodies() -> None:
    """A missing key is the defect; the `type` URI and `title` are a contract clients already parse.

    Routing these bodies through a shared builder that derives `type` from `title.lower()` silently
    rewrote three of them — `/validation` became `/validationerror`, `/payload-too-large` became
    `/payloadtoolarge`, `/throttling` became `/toomanyrequests` — and the first was caught only
    because an integration test happened to assert the title. That is a wire change dressed up as a
    fix, so the builder takes an explicit `slug` and these are pinned.
    """
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=16)

    @app.post("/write")
    async def write() -> dict[str, str]:
        return {"ok": "yes"}

    body = TestClient(app).post("/write", content=b"x" * 64).json()
    assert body["type"] == "https://lance.org/problems/payload-too-large"
    assert body["title"] == "Payload Too Large"
