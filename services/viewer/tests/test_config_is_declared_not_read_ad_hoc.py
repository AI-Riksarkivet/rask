"""Settings are declared in `viewer.core.config`, and a module's privates stay private (VS-24).

open_python-audit VS-24, two halves of one habit:

* `objects.py` read `os.getenv("RASK_SECRET_STORE", "lance-secrets")` inline — the ONLY
  `os.getenv` in either of these two services, in an endpoint module, for a value every other
  setting declares on `ViewerSettings`. An undeclared setting has no default anyone can find, no
  type, and no place a reader looks for it.
* `voice.py` reached into `voice_service._MAX_UPLOAD_BYTES`. A leading underscore is the module
  saying "this is mine"; a second module importing it is a contract with no declaration, and the
  sibling constant `MAX_N` was already made public for exactly this reason ("Public because the
  ROUTE declares it now: one number, named once").

The env NAME is unchanged (`RASK_SECRET_STORE`), so no deployment moves — only where the read lives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from viewer.core.config import ViewerSettings
from viewer.services import voice_service


VIEWER_SRC = Path(voice_service.__file__).resolve().parents[1]


def test_the_secret_store_name_is_a_declared_setting() -> None:
    assert "secret_store" in ViewerSettings.model_fields, (
        "the secret-store name is not declared on ViewerSettings, so nothing documents its default or its type"
    )
    assert ViewerSettings().secret_store == "lance-secrets"


def test_the_env_name_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving a read must not silently retire the variable a deployment already sets."""
    monkeypatch.setenv("RASK_SECRET_STORE", "other-secrets")
    assert ViewerSettings().secret_store == "other-secrets"


def test_no_module_in_the_service_reads_the_environment_directly() -> None:
    offenders = [str(p.relative_to(VIEWER_SRC)) for p in VIEWER_SRC.rglob("*.py") if "os.getenv(" in p.read_text() or "os.environ" in p.read_text()]
    assert not offenders, f"{offenders} read the process environment directly instead of declaring the value on ViewerSettings"


def test_the_upload_cap_is_public_where_it_is_used() -> None:
    assert hasattr(voice_service, "MAX_UPLOAD_BYTES"), "the upload cap the route enforces is still a private name in another module"
    route_src = (VIEWER_SRC / "api" / "v1" / "endpoints" / "voice.py").read_text()
    assert "_MAX_UPLOAD_BYTES" not in route_src, "voice.py still reaches into voice_service's private constant"
    assert "MAX_UPLOAD_BYTES" in route_src
