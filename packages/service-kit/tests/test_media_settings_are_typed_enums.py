"""SK-18 — constrained settings were `str` fields policed by hand-rolled `field_validator`s.

`read_backend` / `write_backend` (`direct|catalog`) and `lineage_sink` (`log|none`) had their allowed
values spelled inside validator bodies. The field type stayed `str`, so the constraint was invisible
to `ty` (a comparison against a misspelt literal type-checks fine), invisible to the generated
schema, and re-spelled as bare strings at every consumer. A `StrEnum` states the set once, in the
type — and because its members compare equal to their own strings, every existing `== "catalog"`
comparison keeps working.
"""

from __future__ import annotations

from service_kit.media.config import LineageSink, Settings


def _settings(**env: object) -> Settings:
    return Settings.model_validate(env)


def test_the_derived_properties_keep_their_meaning() -> None:
    live = _settings(MEDIA_READ_BACKEND="catalog", MEDIA_WRITE_BACKEND="catalog", MEDIA_CATALOG_URI="http://catalog:2333")
    assert live.rest_catalog_mode is True
    assert live.effective_lineage_sink is LineageSink.none
    assert _settings().rest_catalog_mode is False
    assert _settings().effective_lineage_sink is LineageSink.log
