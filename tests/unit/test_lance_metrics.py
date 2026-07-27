"""The Lance-native metrics bridge — all three paths, now that pylance 9 makes it real."""

from __future__ import annotations

import sys
import types

import pytest
from common.lance_metrics import instrument_lance_if_available


def test_activates_against_the_really_installed_pylance() -> None:
    """The bridge is live, not merely importable — asserted against the real dependency, not a fake.

    This test replaces one that asserted the opposite. Under the old pylance 8.0.0 pin ``lance.otel`` did
    not exist, so the honest assertion then was that the guard no-ops; the module and its five lifespan
    hooks were written ahead of the release and lay dormant. The 9.0 bump (which carries the ``otel`` extra)
    is what turns them on, and the thing worth pinning now is that it genuinely did turn on: a mocked
    ``lance.otel`` — which the test below still uses for the failure path — would pass just as happily
    against a build that shipped without the extra.
    """
    assert instrument_lance_if_available() is True

    import lance.otel

    assert callable(lance.otel.instrument_lance_metrics)


def test_noop_when_the_otel_extra_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build installed as plain ``pylance`` rather than ``pylance[otel]`` must still start.

    Metrics are an enhancement; a missing optional dependency may never touch service startup, and five
    lifespans call this. ``sys.modules[name] = None`` is the documented way to make an import raise
    ``ImportError`` without uninstalling anything.
    """
    monkeypatch.setitem(sys.modules, "lance.otel", None)
    assert instrument_lance_if_available() is False


def test_activates_when_the_bridge_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    fake = types.ModuleType("lance.otel")
    setattr(fake, "instrument_lance_metrics", lambda: calls.append(True))  # noqa: B010
    monkeypatch.setitem(sys.modules, "lance.otel", fake)
    assert instrument_lance_if_available() is True
    assert calls == [True]


def test_bridge_failure_never_raises_into_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("recorder exploded")

    fake = types.ModuleType("lance.otel")
    setattr(fake, "instrument_lance_metrics", boom)  # noqa: B010
    monkeypatch.setitem(sys.modules, "lance.otel", fake)
    assert instrument_lance_if_available() is False  # logged, swallowed — startup untouched
