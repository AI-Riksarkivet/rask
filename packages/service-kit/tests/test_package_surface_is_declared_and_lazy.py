"""SK-17 — `service_kit/__init__.py` WAS the app factory, so every import paid for it.

Importing anything under this library executed `make_service_app`'s whole graph first: FastAPI,
python-dotenv, the OTel wiring, the middleware stack, the probe router. `from service_kit.config
import Settings` imported a web framework. A Ray job reaching for one Arrow helper in
`service_kit.lakehouse` did too. And with no `__all__` anywhere in ~80 modules there was nothing
saying which names were the library's surface and which were internals.

The factory moved to `service_kit.app`; the package root re-exports its names lazily (PEP 562), so
the public spelling is unchanged and the cost is not paid until somebody actually builds an app.
"""

from __future__ import annotations


def test_an_undeclared_name_raises_attribute_error_not_import_error() -> None:
    import pytest

    import service_kit

    with pytest.raises(AttributeError, match="no attribute 'not_a_thing'"):
        _ = service_kit.not_a_thing
