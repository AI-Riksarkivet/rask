"""The Lance-native metrics bridge: it activates against the installed pylance, and its failure never reaches startup."""

from __future__ import annotations

import pytest

from lakehouse import lance_metrics
from lakehouse.lance_metrics import register_lance_metrics


def test_activates_against_the_really_installed_pylance() -> None:
    """The bridge is live, not merely importable: asserted against the real dependency, not a fake, since a mocked
    ``lance.otel`` would pass just as happily against a build that shipped without the ``otel`` extra."""
    assert register_lance_metrics() is True

    import lance.otel

    assert callable(lance.otel.instrument_lance_metrics)


def test_bridge_failure_never_raises_into_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("recorder exploded")

    monkeypatch.setattr(lance_metrics, "instrument_lance_metrics", boom)
    assert register_lance_metrics() is False  # logged, swallowed — startup untouched
