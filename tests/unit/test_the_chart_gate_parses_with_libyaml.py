"""The chart gate degrades to pure Python rather than failing, and says so when it does.

`chart_yaml.FAST_LOADER` falls back to `yaml.SafeLoader` when PyYAML was built without libyaml, because
a missing `CSafeLoader` attribute would otherwise `AttributeError` at import and red every chart gate —
an unacceptable trade for a speed optimisation. But a silent fallback costs ~57 % of this directory's
wall clock (measured 2026-09-09: 1.51 s per parse against 0.14 s), and a suite that got three times
slower for no visible reason is exactly the kind of thing nobody diagnoses.

So the fallback keeps the gate GREEN and this test makes the loss VISIBLE. If it fails, the estate did
not break — it got slow, and the fix is a PyYAML built against libyaml.
"""

from __future__ import annotations

import yaml
from chart_yaml import FAST_LOADER, LIBYAML_AVAILABLE


def test_the_fast_loader_is_the_one_in_use_here() -> None:
    assert LIBYAML_AVAILABLE, (
        "PyYAML on this platform has no CSafeLoader, so every chart gate parses with the pure-Python "
        "loader — measured 10x slower, ~57% of tests/unit's wall clock. Nothing is broken; install a "
        "PyYAML built against libyaml to get it back."
    )


def test_the_fallback_is_a_SAFE_loader_and_not_a_lax_one() -> None:
    """Degrading must not widen what the gate will parse. Both loaders refuse Python-specific tags."""
    import pytest

    with pytest.raises(yaml.YAMLError):
        # noqa is the point of the test: S506 cannot see that FAST_LOADER is a SAFE loader, and
        # proving that it is — for both the C and the pure-Python branch — is why this exists.
        yaml.load("!!python/object/apply:os.system ['true']", Loader=FAST_LOADER)  # noqa: S506


def test_both_loaders_read_the_same_documents() -> None:
    """The swap is a parser change, not a semantic one — asserted rather than assumed."""
    text = "a: 1\n---\nb: [2, 3]\n---\nc: {d: null}\n"

    assert list(yaml.load_all(text, Loader=FAST_LOADER)) == list(yaml.safe_load_all(text))
