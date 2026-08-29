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

import subprocess
import sys


def _modules_after(statement: str) -> set[str]:
    """The module table of a FRESH interpreter after `statement` — this cannot be measured in-process,
    because pytest has already imported half the estate."""
    result = subprocess.run(
        [sys.executable, "-c", f"import sys; {statement}; print(chr(10).join(sorted(sys.modules)))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(result.stdout.split())


def test_importing_settings_no_longer_builds_the_app_factorys_graph() -> None:
    loaded = _modules_after("import service_kit.config")
    assert "service_kit.config" in loaded
    # `dotenv` is NOT on this list: pydantic-settings imports it itself, so it arrives with `Settings`
    # either way and asserting on it would test somebody else's dependency, not this change.
    for unwanted in ("fastapi", "starlette", "service_kit.app", "service_kit.middleware", "service_kit.probes"):
        assert unwanted not in loaded, f"importing Settings still pulls in {unwanted}"


def test_importing_the_error_types_does_not_either() -> None:
    loaded = _modules_after("import service_kit.exceptions")
    assert "service_kit.app" not in loaded
    assert "dotenv" not in loaded, "the error types still load the app factory's dotenv read"


def test_the_public_names_still_import_from_the_package_root() -> None:
    from service_kit import build_settings, default_lifespan, make_service_app, setup_logging, setup_otel
    from service_kit.app import make_service_app as direct

    assert make_service_app is direct
    assert all(callable(fn) for fn in (build_settings, default_lifespan, setup_logging, setup_otel))


def test_the_surface_is_declared_and_dir_shows_it() -> None:
    import service_kit

    assert service_kit.__all__ == sorted(service_kit.__all__)
    assert set(service_kit.__all__) == set(service_kit._LAZY), "the declared surface and the lazy map disagree"
    assert "make_service_app" in service_kit.__all__
    assert set(service_kit.__all__) <= set(dir(service_kit)), "a declared name that dir() hides is invisible to tooling"


def test_an_undeclared_name_raises_attribute_error_not_import_error() -> None:
    import pytest

    import service_kit

    with pytest.raises(AttributeError, match="no attribute 'not_a_thing'"):
        _ = service_kit.not_a_thing


def test_a_submodule_import_is_untouched_by_the_lazy_root() -> None:
    """`from service_kit import dapr_publish` is an import-system lookup, not an attribute one."""
    from service_kit import dapr_publish

    assert dapr_publish.__name__ == "service_kit.dapr_publish"
