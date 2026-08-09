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


def test_unmapped_producer_falls_back_to_the_default_url() -> None:
    s = _settings({"insid3": "http://insid3:1"}, default="http://runner:1")
    assert backend_for(s, "grounding-dino") == "http://runner:1"


def test_nothing_configured_is_none_which_means_the_honest_mock() -> None:
    assert backend_for(_settings(), "grounding-dino") is None


def test_a_prefix_never_matches_mid_name() -> None:
    """`insid3` must not catch `not-insid3` — prefix means STARTSWITH, not substring."""
    s = _settings({"insid3": "http://insid3:1"})
    assert backend_for(s, "not-insid3") is None


def test_the_listing_names_every_known_family_including_the_batch_ones() -> None:
    """One registry, not two. The frontend ships producer metadata for htr/insid3/vlm-judge/
    embed-propagate; while `_RETURNS` knew only the two interactive families, `/assist/producers`
    never listed the rest — their compatibility rendered "unknown" forever and the settings
    surface denied producers the jobs seam happily accepts."""
    from annotator.api.v1.endpoints.assist import producer_listing

    listing = producer_listing(_settings())
    by_name = {p.name: p for p in listing.producers}

    assert {"grounding-dino", "sam", "htr", "insid3", "vlm-judge", "embed-propagate"} <= set(by_name)
    assert by_name["htr"].returns == ["text"]
    assert by_name["insid3"].returns == ["mask"]
    # Verdict/field producers emit no drawable shape — an honest empty, not a guess.
    assert by_name["vlm-judge"].returns == []


def test_batch_only_families_say_so_and_interactive_ones_do_not() -> None:
    """The flag the assist BAR gates on: a mode button for a jobs-seam producer could only ever
    answer from the mock."""
    from annotator.api.v1.endpoints.assist import producer_listing

    by_name = {p.name: p for p in producer_listing(_settings()).producers}

    assert by_name["htr"].interactive is False
    assert by_name["vlm-judge"].interactive is False
    assert by_name["embed-propagate"].interactive is False
    assert by_name["grounding-dino"].interactive is True
    assert by_name["sam"].interactive is True
    assert by_name["insid3"].interactive is True


def test_compatibility_now_computes_for_batch_families_too() -> None:
    """An `htr` producer against a task allowing `text` is a YES, not an eternal unknown."""
    from annotator.api.v1.endpoints.assist import producer_listing

    by_name = {p.name: p for p in producer_listing(_settings(), allowed={"text"}).producers}

    assert by_name["htr"].compatible is True
    assert by_name["grounding-dino"].compatible is False


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


def test_a_declared_return_in_a_producer_dialect_is_canonicalised() -> None:
    """A backend declaring "rectangle" and a task allowing "bbox" must still meet — the same
    normalization a RESPONSE gets applies to the declaration."""
    from annotator.api.v1.endpoints.assist import producer_listing

    s = _settings({"legacy-detector": {"url": "http://d:1", "returns": ["rectangle"]}})
    by_name = {p.name: p for p in producer_listing(s, allowed={"bbox"}).producers}

    assert by_name["legacy-detector"].returns == ["bbox"]
    assert by_name["legacy-detector"].compatible is True


def test_a_declaration_OVERRIDES_the_family_map() -> None:
    """A `sam-hq` registered as emitting masks must not be presented with the family's polygon."""
    from annotator.api.v1.endpoints.assist import producer_listing

    s = _settings({"sam-hq": {"url": "http://hq:1", "returns": ["mask"]}})
    by_name = {p.name: p for p in producer_listing(s).producers}

    assert by_name["sam-hq"].returns == ["mask"]
