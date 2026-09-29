"""The assist producer registry — `backend_for` routing (the pluggability seam).

A new model backend must be a CONFIG entry, not a code change: producers route to their backend by
LONGEST prefix, the default `assist_url` catches the rest, and nothing configured means the honest
in-repo mock. The longest-prefix rule is load-bearing — `"sam"` covers `sam-click` while a more
specific `"sam-hq"` entry still wins over it.
"""

from __future__ import annotations

from types import SimpleNamespace

from annotator.api.v1.endpoints.assist import backend_for


def _settings(backends: dict[str, object] | None = None, default: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(assist_backends=backends or {}, assist_url=default)


def test_longest_prefix_wins() -> None:
    s = _settings({"sam": "http://sam:1", "sam-hq": "http://samhq:1", "insid3": "http://insid3:1"})
    assert backend_for(s, "sam-click") == "http://sam:1"
    assert backend_for(s, "sam-hq-tiny") == "http://samhq:1"
    assert backend_for(s, "insid3") == "http://insid3:1"


def test_a_structured_entry_declares_its_own_contract() -> None:
    """The registry entry IS the whole of adding a model now. A bare URL used to be all an entry
    could say, so a registered `vlm` rendered "returns unknown" forever (the family map in code
    only knows the built-ins) and task compatibility could never compute."""
    from annotator.api.v1.endpoints.assist import producer_listing

    s = _settings(
        {
            "vlm": {"url": "http://vllm:8000", "returns": ["bbox"], "inputs": ["prompt"]},
            "sam": "http://sam:9000",  # bare-URL back-compat, side by side
        }
    )
    by_name = {p.name: p for p in producer_listing(s, allowed={"bbox"}).producers}

    assert by_name["vlm"].configured is True
    assert by_name["vlm"].returns == ["bbox"]
    assert by_name["vlm"].inputs == ["prompt"]
    assert by_name["vlm"].compatible is True  # the eternal unknown, ended
    assert by_name["sam"].returns == ["polygon"]  # family fallback still answers for built-ins
    assert backend_for(s, "vlm-qwen") == "http://vllm:8000"
    assert backend_for(s, "sam-click") == "http://sam:9000"
