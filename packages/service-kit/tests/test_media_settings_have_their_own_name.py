"""SK-10 — one distribution, two classes called `Settings` and two `get_settings` with different DI shapes.

`service_kit.config.Settings` is the fleet's `RASK_*` config; `service_kit.media.config.Settings` was
a second, unrelated class on `MEDIA_*` aliases. They share almost no field and no inheritance, so a
`settings: Settings` annotation, a stack frame or a `SettingsDep` meant one or the other depending on
which module the reader had open.

The accessors were worse than the classes, because they had INCOMPATIBLE SIGNATURES under one name:
`service_kit.dependencies.get_settings(request)` returns whatever the app's lifespan bound, while
`service_kit.media.config.get_settings()` returned an `@lru_cache`d singleton. `AppState` defaulted
its `settings` field to the latter, so every `AppState` in a process was bound to ONE mutable object:
two apps in one process could not be configured differently, and a mutation through one silently
reconfigured the other.

The definitions are now `MediaSettings` / `get_media_settings`; `Settings` / `get_settings` remain as
explicit aliases, because three services import them under those names.
"""

from __future__ import annotations

from service_kit.config import Settings as FleetSettings
from service_kit.media import config as media_config
from service_kit.media.state import AppState


def test_the_media_settings_are_named_for_the_plane_they_configure() -> None:
    assert media_config.MediaSettings.__name__ == "MediaSettings"
    assert media_config.MediaSettings is not FleetSettings
    assert not issubclass(media_config.MediaSettings, FleetSettings)


def test_the_old_names_are_aliases_not_second_definitions() -> None:
    assert media_config.Settings is media_config.MediaSettings
    assert media_config.get_settings is media_config.get_media_settings


def test_the_accessor_is_named_apart_from_the_request_scoped_one() -> None:
    """Same name, different signature, different lifetime — the pair that made the AppState bug."""
    import inspect

    from service_kit.dependencies import get_settings as request_scoped

    assert list(inspect.signature(request_scoped).parameters) == ["request"]
    assert list(inspect.signature(media_config.get_media_settings).parameters) == []


def test_each_app_state_carries_its_own_settings() -> None:
    first, second = AppState(), AppState()
    assert first.settings is not second.settings, "two apps in one process share a mutable settings object"
    first.settings.host = "10.0.0.1"
    assert second.settings.host != "10.0.0.1", "a mutation through one app reconfigured the other"


def test_the_process_wide_singleton_is_still_available_when_asked_for_explicitly() -> None:
    assert media_config.get_media_settings() is media_config.get_media_settings()
