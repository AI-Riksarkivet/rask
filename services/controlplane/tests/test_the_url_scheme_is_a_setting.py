"""`RASK_PROJECT_URL_SCHEME` is a declared, validated setting — not a per-request `os.environ` read.

`routes.list_projects` called `os.environ.get("RASK_PROJECT_URL_SCHEME", "http")` on every request
(FLEET-ENV-SCATTER), while the service already owns a `ControlplaneSettings` model injected as
`ControlplaneSettingsDep`. Two things that read as style and are not:

* **The value is unvalidated and it lands in a LINK.** It is interpolated straight into
  `f"{scheme}://{host}/overview"`, which the home zone's gallery renders as each project's entry
  point. Anything at all was accepted, so a typo'd or hostile value silently shipped a scheme no
  browser would follow (or would follow differently) to every tenant in the estate. It is one of
  exactly two values.
* **It was read per request**, so the entry URLs the gallery shows could change under a running pod
  without a restart, and nothing recorded which answer a given response had used.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError


@pytest.fixture
def client() -> Iterator[TestClient]:
    from controlplane import app

    with TestClient(app) as c:
        yield c


def _reader():
    class _Reader:
        def list_projects(self) -> list[dict[str, Any]]:
            return [{"metadata": {"name": "demo", "creationTimestamp": "2026-01-01T00:00:00Z"}, "spec": {"team": "t"}, "status": {"namespace": "project-demo"}}]

        def ingress_hosts(self) -> dict[str, str]:
            return {"project-demo": "demo.rask.test"}

    return _Reader()


def test_the_scheme_is_a_declared_field() -> None:
    from controlplane.config import ControlplaneSettings

    assert ControlplaneSettings().project_url_scheme == "http"
    assert ControlplaneSettings.model_validate({"RASK_PROJECT_URL_SCHEME": "https"}).project_url_scheme == "https"


def test_a_scheme_that_is_not_http_or_https_is_refused() -> None:
    """The value is interpolated into a link the gallery renders — it is not free text."""
    from controlplane.config import ControlplaneSettings

    with pytest.raises(ValidationError):
        ControlplaneSettings.model_validate({"RASK_PROJECT_URL_SCHEME": "javascript"})


def test_the_route_spends_the_injected_setting(client: TestClient) -> None:
    """Overriding the settings dependency changes the entry URL — no environment involved."""
    from controlplane import app
    from controlplane.config import ControlplaneSettings
    from controlplane.dependencies import get_controlplane_settings
    from controlplane.routes import get_reader

    app.dependency_overrides[get_reader] = _reader
    app.dependency_overrides[get_controlplane_settings] = lambda: ControlplaneSettings.model_validate({"RASK_PROJECT_URL_SCHEME": "https"})
    try:
        response = client.get("/api/projects/")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["projects"][0]["url"] == "https://demo.rask.test/overview"
