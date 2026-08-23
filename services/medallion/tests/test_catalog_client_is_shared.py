"""The catalog client is built once in the lifespan, not per call.

`fastapi` -> `production-patterns.md` § Lifespan: build everything once, dispose everything once, and
inject it from `app.state`. `catalog_register` opened an `httpx.Client` on EVERY call — three sites —
so each stage transition paid a fresh connection setup to the catalog, and the gate probe added on the
held path made it two.

The accommodation is deliberate: the client is OPTIONAL. Passing one uses it; omitting one falls back
to the per-call client, which is what keeps every existing caller — the tests here, `scripts/`, any
direct use — working without a wired app. The rule is about the hot path having a shared client
available, not about making the function unusable without one.
"""

from __future__ import annotations

import inspect

from medallion.api import dependencies
from medallion.services import catalog_register


def test_the_publish_helper_accepts_a_shared_client() -> None:
    assert "client" in inspect.signature(catalog_register.publish_stage_output).parameters


def test_the_ensure_helper_accepts_a_shared_client() -> None:
    assert "client" in inspect.signature(catalog_register.ensure_stage_output).parameters


def test_a_shared_client_is_optional() -> None:
    """Omitting it must keep working — every existing caller does, and none of them has an app."""
    assert inspect.signature(catalog_register.publish_stage_output).parameters["client"].default is None


def test_there_is_a_dependency_for_it() -> None:
    """Injected from app.state the way the sibling clients are, not reached for as a global."""
    assert hasattr(dependencies, "CatalogHttpDep"), "no dependency to hand the route a shared client"
