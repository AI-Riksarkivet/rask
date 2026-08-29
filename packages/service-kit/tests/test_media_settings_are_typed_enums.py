"""SK-18 — constrained settings were `str` fields policed by hand-rolled `field_validator`s.

`read_backend` / `write_backend` (`direct|catalog`) and `lineage_sink` (`log|none`) had their allowed
values spelled inside validator bodies. The field type stayed `str`, so the constraint was invisible
to `ty` (a comparison against a misspelt literal type-checks fine), invisible to the generated
schema, and re-spelled as bare strings at every consumer. A `StrEnum` states the set once, in the
type — and because its members compare equal to their own strings, every existing `== "catalog"`
comparison keeps working.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from service_kit.media.config import LineageSink, Settings, TableBackend


def _settings(**env: object) -> Settings:
    return Settings.model_validate(env)


def test_the_backend_fields_are_the_enum() -> None:
    settings = _settings(MEDIA_READ_BACKEND="catalog", MEDIA_WRITE_BACKEND="direct")
    assert settings.read_backend is TableBackend.catalog
    assert settings.write_backend is TableBackend.direct


def test_the_members_still_compare_equal_to_their_strings() -> None:
    """Every existing consumer compares against the literal — `open_reader`, `open_writer`, the tests."""
    assert _settings(MEDIA_READ_BACKEND="catalog").read_backend == "catalog"
    assert _settings().write_backend != "catalog"


def test_an_unknown_value_is_refused_at_load() -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(MEDIA_READ_BACKEND="catalouge")
    assert "direct" in str(caught.value) and "catalog" in str(caught.value), "the message must name the options"


def test_the_lineage_sink_is_the_enum_and_stdout_is_gone() -> None:
    assert _settings(MEDIA_LINEAGE_SINK="log").lineage_sink is LineageSink.log
    assert [member.value for member in LineageSink] == ["log", "none"]
    with pytest.raises(ValidationError):
        _settings(MEDIA_LINEAGE_SINK="stdout")


def test_the_derived_properties_keep_their_meaning() -> None:
    live = _settings(MEDIA_READ_BACKEND="catalog", MEDIA_WRITE_BACKEND="catalog", MEDIA_CATALOG_URI="http://catalog:2333")
    assert live.rest_catalog_mode is True
    assert live.effective_lineage_sink is LineageSink.none
    assert _settings().rest_catalog_mode is False
    assert _settings().effective_lineage_sink is LineageSink.log


def test_no_hand_rolled_membership_check_survives() -> None:
    import inspect

    from service_kit.media import config

    source = inspect.getsource(config)
    assert '{"direct", "catalog"}' not in source
    assert '{"stdout", "log", "none"}' not in source
