"""The platform's test seam, a pytest plugin a client's suite gets with `service-kit` ([[XC-190]]).

A client pins `service-kit` by lakehouse tag and runs its suite in its own repository, so what that suite needs to stay
hermetic against the platform's process-global state travels with the package, under the `pytest11` entry point
`service_kit`, and loads in any environment that installs the package. Each fixture here guards state `service-kit` or
its `dapr` dependency keeps across a process; this repository's suites get the same seam the same way.

- every test gets its own cache of secret-store reads (`service_kit.governed.secret_cache.STORE_READS`);
- every test gets the token files a pod's kubelet projects, one per lakehouse door;
- Dapr's sidecar handshake is bounded and its process-global actor-proxy factory starts empty;
- respx's bare `@respx.mock` is strict, and `respx_allows_unused_routes` opts a test out.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Final

import pytest
from dapr.actor.client.proxy import ActorProxy
from dapr.conf import settings as dapr_settings

from service_kit.governed import secret_cache


try:
    import respx
except ImportError:  # a client whose suite mocks no HTTP does not install respx
    respx = None

#: How long any test may let Dapr's sidecar handshake run. The SDK's own default is 60 s, and where there is no sidecar
#: nothing is cached, so every unstubbed `ActorProxy.create` pays it again: CI run 35726435185 spent 8m17s in
#: `wait_for_sidecar` before `--timeout=300` took the offline suite and four e2e lanes with it.
DAPR_HANDSHAKE_BUDGET_SECONDS: Final = 1.0

#: The doors whose callers read a projected token file, `RASK_<DOOR>_IDENTITY_TOKEN_FILE`.
PROJECTED_TOKEN_DOORS: Final = ("catalog", "lineage")


# respx's own default is strict (`MockRouter.__init__` takes assert_all_called=True), and that holds for the
# `respx_mock` fixture and the called decorator form, but the bare `@respx.mock` is a module-level router built with
# `_assert_all_called = False`, so a route registered for a call the code no longer makes passes silently. Measured
# when first switched on in this repository: 17 of 227 tests across 16 respx files went red, thirteen of them mocking a
# namespace-create the cascade never makes and the real catalog answers 400. Flipping the one instance covers every
# bare use; `@respx.mock(assert_all_called=...)` would build a new router, and a module-level `respx.post(...)` in the
# test body would then register on the global one. To assert a call is not made, register no route for it
# (`assert_all_mocked` already raises on an unregistered call) or assert over `respx.calls`.
if respx is not None:
    respx.mock._assert_all_called = True


@pytest.fixture
def respx_allows_unused_routes() -> Iterator[None]:
    """Opt out of the strict default, for a test whose uncalled route is the point.

    A dead route (the code stopped making the call) is what the default catches. A negative route is registered so the
    test can prove it was not called, or which subset fired, and deleting it would delete the proof: taking this
    fixture declares a test of the second kind. A fixture rather than a decorator argument, because
    `@respx.mock(assert_all_called=False)` builds a new router and module-level `respx.get(...)` in the body would
    register on the global one.
    """
    if respx is None:
        pytest.fail("respx_allows_unused_routes needs respx installed")
    respx.mock._assert_all_called = False
    try:
        yield
    finally:
        respx.mock._assert_all_called = True


@pytest.fixture(autouse=True)
def _a_fresh_secret_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test its own process-global cache of secret-store reads.

    It holds what a store double answered for up to ten minutes, so one test's answer would otherwise be the next
    test's, and a test asserting what the store is asked would pass or fail by collection order. A test that drives the
    TTL replaces it again with a cache on its own clock.
    """
    monkeypatch.setattr(secret_cache, "STORE_READS", secret_cache.SecretCache())


@pytest.fixture(autouse=True)
def _projected_identity_tokens(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test the token files the kubelet projects into a pod, one per door ([[LH-220]]).

    A service authenticates to the catalog's and lineage's doors with the token projected for that audience, read
    afresh on every call, and a call whose file is absent is not sent. So a test that drives such a call reads a file
    the way the pod does, at the path the settings name. The contents are opaque: the door under test is a double, or a
    real verifier minted its own token.
    """
    for door in PROJECTED_TOKEN_DOORS:
        path = tmp_path / f"projected-rask-{door}-token"
        path.write_text(f"projected-for-rask-{door}")
        monkeypatch.setenv(f"RASK_{door.upper()}_IDENTITY_TOKEN_FILE", str(path))


@pytest.fixture(autouse=True)
def _bounded_dapr_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound the sidecar handshake for each test without stubbing the handshake itself.

    A stub of `DaprHealth.wait_for_sidecar` would swap the handshake production runs for one it never runs; lowering
    the budget leaves it polling, sleeping and raising as it does, and only stops it costing a minute per call. A test
    that needs another duration sets its own inside a `MonkeyPatch.context`.
    """
    monkeypatch.setattr(dapr_settings, "DAPR_HEALTH_TIMEOUT", DAPR_HANDSHAKE_BUDGET_SECONDS, raising=False)


@pytest.fixture(autouse=True)
def _no_dapr_proxy_factory_carryover() -> None:
    """Start each test with Dapr's process-global actor-proxy factory empty, so one test cannot decide another's outcome.

    `ActorProxy` caches its default factory on the class, assigned only after the constructor returns. A test that
    builds one leaves it built, so a later test expecting construction to raise (no sidecar) fails `DID NOT RAISE`
    whenever something warmed it first; and where construction raises nothing is cached, so each later call pays the
    whole handshake again. Clearing the one attribute leaves the mechanism as production runs it.
    """
    ActorProxy._default_proxy_factory = None
