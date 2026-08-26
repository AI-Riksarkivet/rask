"""Interactive API docs must be OFF unless a deployment asks for them.

open_fastapi-audit — "/docs and /openapi.json are on in production for every served app, and the
public front door serves an unauthenticated aggregated Swagger of the whole estate at /api/docs".

TWO DEFECTS, and the second is the one that made the first survive.

**The apps.** Four entrypoints — gateway, annotator, viewer, search — construct `FastAPI(...)` with no
`docs_url`/`openapi_url` at all, so FastAPI's defaults stand and `/docs`, `/redoc` and `/openapi.json`
are served. `make_service_app` is worse than silent: it hard-codes all three onto `{api_prefix}`, so
every service composed through it publishes them with no way to say no.

**The default.** Catalog, lineage, medallion and maintenance DO carry a `docs_enabled` flag — and it
defaults to `True`, with no deployment path setting it: `grep -rn DOCS chart/ .docker/ scripts/`
matches nothing but an unrelated `_DOCS = _ROOT / "docs"`. A flag nobody sets is the default, and a
security default that every deployment must remember to turn OFF is one nobody turns off.

So the gate asserts the CLOSED default, not the presence of a knob. With no docs env var set — which
is precisely how the chart deploys today — every app must answer nothing at all.

What leaks is a route table, parameter names and request/response schemas rather than data: every
documented route stays individually auth-gated. That is why the audit grades this medium. The gateway
half is the sharp end, because `chart/templates/ingress.yaml` publishes `/api` and the gateway's
catch-all serves `{prefix}/openapi.json` by fanning out to every backend it fronts — ten sequential
upstream fetches at a 10 s timeout, to an unauthenticated caller, which is an amplification lever as
well as a disclosure.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: Every served entrypoint, as (import path, expression yielding the app).
#: All 14 of them — the audit's count — because a gate covering some of the apps is how three of these
#: stayed ungated while four siblings carried a flag.
ENTRYPOINTS: list[tuple[str, str]] = [
    ("gateway", "app"),
    ("annotator.main", "app"),
    ("viewer.main", "app"),
    ("search.main", "app"),
    ("catalog.main", "app"),
    ("lineage.main", "app"),
    ("medallion.producer", "app"),
    ("medallion.mover", "app"),
    ("maintenance.service", "app"),
    ("compute", "app"),
    ("controlplane", "app"),
    ("flows", "app"),
    ("notifications", "app"),
    ("ingest", "create_app()"),
]

_PROBE = """
import importlib, json, sys
mod = importlib.import_module({module!r})
app = eval({expr!r}, {{"m": mod, **vars(mod)}})
print(json.dumps({{
    "openapi_url": app.openapi_url,
    "docs_url": app.docs_url,
    "redoc_url": app.redoc_url,
}}))
"""


def _urls(module: str, expr: str) -> dict[str, str | None]:
    """Import one app in a CLEAN process and report its three doc URLs.

    A subprocess per app, deliberately. These modules build settings at import time from
    `os.environ`, several via `lru_cache`, so importing fourteen of them into one interpreter would
    let the first import's environment decide the rest — and this gate is precisely about what the
    environment does. Isolation is the assertion.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        # Strip every docs opt-in so the DEFAULT is what gets measured. A developer with docs turned
        # on in their shell must not be able to turn this gate green.
        if not (k.endswith("_DOCS") or k == "RASK_DOCS")
    }
    # Inert credentials, because some apps validate required settings at IMPORT time and would other-
    # wise never reach their FastAPI constructor. Without these the probe reports an unimportable app
    # rather than an ungated one — which is how the catalog's real state stayed unmeasured on the
    # first run of this gate. Nothing here connects to anything.
    env |= {
        "LANCE_S3_ACCESS_KEY_ID": "gate",
        "LANCE_S3_SECRET_ACCESS_KEY": "gate",
        "RASK_S3_ACCESS_KEY_ID": "gate",
        "RASK_S3_SECRET_ACCESS_KEY": "gate",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, expr=expr)],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"{module} could not be imported for the docs gate:\n{proc.stderr[-2000:]}")
    return dict(__import__("json").loads(proc.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(("module", "expr"), ENTRYPOINTS, ids=[m for m, _ in ENTRYPOINTS])
def test_the_app_serves_no_docs_by_default(module: str, expr: str) -> None:
    urls = _urls(module, expr)
    served = {name: url for name, url in urls.items() if url is not None}
    assert not served, (
        f"{module} publishes {served} with no docs flag set — which is how the chart deploys it. "
        f"Interactive docs and the OpenAPI schema must be opt-IN; pass "
        f"docs_url/redoc_url/openapi_url=None unless the service's docs_enabled setting is true"
    )
