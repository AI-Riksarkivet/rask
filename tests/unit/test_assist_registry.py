"""The assist producer registry — `backend_for` routing (the pluggability seam).

A new model backend must be a CONFIG entry, not a code change: producers route to their backend by
LONGEST prefix, the default `assist_url` catches the rest, and nothing configured means the honest
in-repo mock. The longest-prefix rule is load-bearing — `"sam"` covers `sam-click` while a more
specific `"sam-hq"` entry still wins over it.
"""

from __future__ import annotations

from types import SimpleNamespace

from annotator.api.v1.endpoints.assist import backend_for


def _settings(backends: dict[str, str] | None = None, default: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(assist_backends=backends or {}, assist_url=default)


def test_longest_prefix_wins() -> None:
    s = _settings({"sam": "http://sam:1", "sam-hq": "http://samhq:1", "insid3": "http://insid3:1"})
    assert backend_for(s, "sam-click") == "http://sam:1"
    assert backend_for(s, "sam-hq-tiny") == "http://samhq:1"
    assert backend_for(s, "insid3") == "http://insid3:1"


def test_unmapped_producer_falls_back_to_the_default_url() -> None:
    s = _settings({"insid3": "http://insid3:1"}, default="http://runner:1")
    assert backend_for(s, "grounding-dino") == "http://runner:1"


def test_nothing_configured_is_none_which_means_the_honest_mock() -> None:
    assert backend_for(_settings(), "grounding-dino") is None


def test_a_prefix_never_matches_mid_name() -> None:
    """`insid3` must not catch `not-insid3` — prefix means STARTSWITH, not substring."""
    s = _settings({"insid3": "http://insid3:1"})
    assert backend_for(s, "not-insid3") is None
