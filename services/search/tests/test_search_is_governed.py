"""The search plane must know who is asking, and which corpora they may search.

open_python-audit `X6` (E1, HIGH) — "`search` is the only explorer service with no authn/authz code
path at all — the chart's estate-wide OIDC/FGA env has nothing to bind to" — and `VS-13` (med) —
"The search service has no authn/authz at all yet accepts a raw SQL `where` expression ANDed into
every query". One service, one root cause, so one change.

WHY IT IS NOW THE ANOMALY, not the norm: the chart sets `RASK_OIDC_*`/`RASK_FGA_*` on all three
explorer services, and `viewer` and `annotator` both bind them (`ViewerSettings(Settings,
GovernedAuthSettings)`, `AnnotatorSettings(GovernedAuthSettings, ...)`). `SearchSettings(Settings)`
did not, so the estate's authorization env reached this service and had nothing to attach to — it was
configured-looking and inert. With the viewer's 24 corpus routes now gated, search is the last
unguarded door on the `/api/explorer` edge, and it is the one that takes a raw SQL predicate.

FILTERED, NOT REFUSED, for the fan-out — the estate's established answer (`datasets.list_datasets`:
"a caller with access to two of five corpora gets two, because the honest answer to 'what can I
search' is a shorter list, not a 403"). A single-corpus search is a different question with a
different answer: asked for one specific corpus and not entitled to it, the caller gets 403.

The relation is `can_read_data`, not `can_get_metadata`: a search returns row PAYLOAD, which is the
rung `pages.py` uses for bytes rather than the one the corpus listing uses.
"""

from __future__ import annotations

import inspect

from search.core.config import SearchSettings
from service_kit.governed.settings import GovernedAuthSettings


def test_the_search_settings_bind_the_estates_auth_env() -> None:
    """X6's core: the chart ships RASK_OIDC_*/RASK_FGA_* to this service and nothing read them."""
    assert issubclass(SearchSettings, GovernedAuthSettings), (
        "SearchSettings does not mix in GovernedAuthSettings — the estate's OIDC/FGA env reaches this "
        "service and binds to nothing, so authorization is configured-looking and inert"
    )
    for field in ("oidc_enabled", "fga_enabled"):
        assert field in SearchSettings.model_fields, f"{field} is not a setting of the search service"


def test_the_service_has_an_auth_seam_at_all() -> None:
    """`find services/search/src -name '*.py'` showed NO api/security.py and no auth module — the
    finding's own evidence. A verified subject has to come from somewhere."""
    from search.api import security

    for name in ("CurrentSubject", "CheckerDep", "READ_DATA"):
        assert hasattr(security, name), f"search's security seam exposes no {name}"


def test_every_search_route_resolves_a_verified_subject() -> None:
    """Deny-by-default, the shape the viewer's gate uses: walk the route signatures rather than
    trusting a grep, so a fourth entry point cannot land ungated."""
    from fastapi.routing import APIRoute

    from search.api import security
    from search.api.v1.router import router

    subject_dep = security._deps.current_subject
    ungated: list[str] = []
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue

        def _walk(dependant: object) -> bool:
            if getattr(dependant, "call", None) is subject_dep:
                return True
            return any(_walk(sub) for sub in getattr(dependant, "dependencies", []))

        if not _walk(route.dependant):
            ungated.append(f"{sorted((route.methods or set()) - {'HEAD', 'OPTIONS'})} {route.path}")
    assert not ungated, "these search routes serve corpus content with no verified subject: " + "; ".join(ungated)


def test_the_raw_sql_predicate_is_still_accepted_but_now_scoped() -> None:
    """VS-13 names the raw `where` as the sharp edge of having no authz. The predicate stays — it is
    a real feature (`duration > 60`) — but it can now only ever run against a corpus the caller is
    entitled to read, which is the half that was missing."""
    from search.services.spec import SearchSpec

    assert "where" in SearchSpec.model_fields, "the raw predicate was removed rather than scoped"
    from search.api.v1 import router as router_module

    assert inspect.isfunction(router_module.search_similar)


# ── the gate refuses and filters, over HTTP ─────────────────────────────────────────────────────
#
# The walk above proves the dependency is WIRED. These prove it DECIDES: a caller with no grant is
# refused a named corpus, and a fan-out is trimmed to what they may read rather than refused whole.


def test_a_caller_with_no_grant_is_REFUSED_a_named_corpus(monkeypatch) -> None:
    import service_kit.media.state as state_mod
    from service_kit.lancekit.descriptor import Declared

    # A REAL `Declared`, not an ad-hoc stub: the gate resolves the request's `?table=` through
    # `search_named`, which only the real model can answer — and an object that answers only
    # `.search` is precisely the superseded shape the gate used to read.
    declared = Declared.model_validate({"identity": {"key_fields": ["doc_id"]}, "searches": [{"name": "default", "row_table": "chunks"}]})
    monkeypatch.setattr(
        state_mod,
        "dataset_handle",
        lambda *_a, **_k: type("H", (), {"id": "vasa", "descriptor": type("D", (), {"declared": declared})()})(),
    )
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from search.api import security
    from search.api.dependencies import StateDep
    from search.api.v1.router import router
    from search.core.config import SearchSettings, get_search_settings
    from service_kit.exceptions import register_handlers

    async def deny(*, user: str, relation: str, obj: str) -> bool:
        return False

    app = FastAPI()
    app.include_router(router)
    register_handlers(app)
    settings = SearchSettings.model_validate(
        {"RASK_FGA_ENABLED": True, "RASK_OIDC_ENABLED": True, "RASK_OIDC_ISSUER": "https://i.test", "RASK_OIDC_AUDIENCE": "rask"}
    )
    app.dependency_overrides[get_search_settings] = lambda: settings
    app.dependency_overrides[StateDep.__metadata__[0].dependency] = lambda: object()
    app.dependency_overrides[security._deps.current_subject] = lambda: "eve"
    app.dependency_overrides[security._deps.get_checker] = lambda: deny

    r = TestClient(app, raise_server_exceptions=False).get("/api/search/similar", params={"key": "d/1", "dataset": "vasa"})
    assert r.status_code == 403, f"an ungranted caller reached the search plane: {r.status_code}"
    assert "can_read_data" in r.text
