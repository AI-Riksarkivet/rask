"""The catalog's own ``Settings``, built for real with test values.

A door reads its settings through ``SettingsDep`` and an authorization context carries them, so a test hands over the
class itself rather than an object holding the few attributes one path reads today: a stand-in cannot see a field
renamed or a validator added, and the real class can. A field a test depends on is passed by name: the class still
reads the environment for the rest, as it does in the pod.
"""

from __future__ import annotations

from typing import Any, Final

from catalog.core.config import Settings


__all__ = ["catalog_settings", "governed_settings"]

#: A key pair the local-directory backend ignores and the class requires.
_STORE_KEYS: Final[frozendict[str, Any]] = frozendict({"s3_access_key_id": "k", "s3_secret_access_key": "s"})

#: What FGA on demands besides itself: a verified subject, from an issuer whose people are named.
_GOVERNED: Final[frozendict[str, Any]] = frozendict(
    {
        "oidc_enabled": True,
        "oidc_issuer": "https://idp.example",
        "oidc_audience": "lance",
        "oidc_idp_id": "dex",
        "fga_enabled": True,
        "fga_api_url": "http://openfga:8080",
    }
)


def catalog_settings(**fields: Any) -> Settings:  # noqa: ANN401 — any field of Settings, validated by it
    """The catalog's settings with authorization off and ``fields`` on top."""
    return Settings.model_validate({**_STORE_KEYS, **fields})


def governed_settings(**fields: Any) -> Settings:  # noqa: ANN401 — any field of Settings, validated by it
    """The catalog's settings with OIDC and FGA on and ``fields`` on top."""
    return Settings.model_validate({**_STORE_KEYS, **_GOVERNED, **fields})
