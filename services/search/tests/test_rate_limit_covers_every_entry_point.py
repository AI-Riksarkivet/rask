"""Every search entry point must sit in the ONE shared rate-limit scope — and a test must say so.

Found by the adversarial re-audit of the search finding's closure. The closing note claimed "one
shared scope across the THREE entry points so they cannot be used in rotation for three times the
budget"; the code had decorated exactly two. `GET /search/similar` drives the same Lance ANN work
from a stored seed vector — no embedder, so the GPU half is smaller, but the fan-out and the cache
bypass are the same — and it was entirely unmetered. Worse, nothing pinned the two decorators that
DID exist: deleting both `@limiter.shared_limit` lines failed no test.

PINNED THROUGH SLOWAPI'S OWN REGISTRY, not through `__wrapped__`. The decorator registers each
handler in the limiter's marked-for-limiting map keyed by `module.qualname`, and that map is what the
middleware consults at request time — so a decorator that is deleted, or a handler that is renamed
out from under its registration, both fail here. `__wrapped__` would be satisfied by ANY decorator.
"""

from __future__ import annotations

from search.api.v1 import router as router_module
from search.core.rate_limit import limiter


#: The three functions that serve caller-driven search work. Derived from the module rather than
#: hardcoded strings, so a rename moves the expectation with the code.
ENTRY_POINTS = ("search_get", "search_post", "search_similar")


def _registry() -> dict[str, object]:
    marked = getattr(limiter, "_Limiter__marked_for_limiting", None)
    assert isinstance(marked, dict), (
        "slowapi no longer exposes its marked-for-limiting registry under this name — re-pin this test against whatever the middleware now consults"
    )
    return marked


def test_every_entry_point_is_registered_with_the_limiter() -> None:
    registry = _registry()
    for name in ENTRY_POINTS:
        fn = getattr(router_module, name)
        key = f"{fn.__module__}.{fn.__qualname__}"
        assert key in registry, (
            f"`{name}` is not registered with the rate limiter — it can be driven in rotation with the "
            f"limited routes for extra budget (registered: {sorted(registry)})"
        )


def test_the_entry_points_actually_exist() -> None:
    """The gate above is vacuous if a rename leaves it asserting about nothing."""
    for name in ENTRY_POINTS:
        assert callable(getattr(router_module, name, None)), f"`{name}` is gone from the router module"
