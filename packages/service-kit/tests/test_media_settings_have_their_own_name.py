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

from service_kit.media.state import AppState


def test_each_app_state_carries_its_own_settings() -> None:
    first, second = AppState(), AppState()
    assert first.settings is not second.settings, "two apps in one process share a mutable settings object"
    first.settings.host = "10.0.0.1"
    assert second.settings.host != "10.0.0.1", "a mutation through one app reconfigured the other"
