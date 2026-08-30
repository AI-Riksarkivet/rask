"""The settings model must accept what the CHART actually renders (FLEET-ENV-SCATTER).

A settings class is only a fix if the deployed environment still binds to it. The reads it replaced
were `os.environ.get(...)` with string comparisons, which accept anything; pydantic VALIDATES, so a
value the chart renders and the model refuses would turn a correct configuration into a crash-loop —
the class of failure a settings refactor most easily introduces and least easily notices.

The values are the ones `helm template` produces for `rask-config` at chart defaults, transcribed
here rather than rendered: a test that shells out to helm cannot run in CI's sandbox, and the point
is the model's acceptance of these exact strings.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gateway.config import GatewaySettings


#: `helm template rask chart/ --set image.localImages=true …` → ConfigMap/rask-config, 2026-08-30.
_RENDERED = {
    "RASK_API_PREFIX": "/api",
    "RASK_DOCS": "false",
    "RASK_DAPR_ENABLED": "true",
    "RASK_COMPUTE_URL": "http://rask-compute:8804",
    "RASK_CONTROLPLANE_URL": "http://rask-controlplane:8820",
    "RASK_INGEST_URL": "http://rask-ingest:8830",
    "RASK_FLOWS_URL": "http://rask-flows:8840",
    "RASK_NOTIFICATIONS_URL": "http://rask-notifications:8850",
    "RASK_CATALOG_API_URL": "http://rask-catalog:2333",
    "RASK_LINEAGE_API_URL": "http://rask-lineage:8000",
    "RASK_LINEAGE_SIDECAR_ONLY_ROUTES": "lineage-events,lineage-reconcile-cron",
    "RASK_MEDALLION_API_URL": "http://rask-medallion-producer:8000",
}


def test_the_rendered_configmap_binds() -> None:
    settings = GatewaySettings.model_validate(_RENDERED)

    assert settings.api_prefix == "/api"
    assert settings.docs_enabled is False
    assert settings.dapr_enabled is True
    assert settings.compute_url == "http://rask-compute:8804"
    assert settings.medallion_url == "http://rask-medallion-producer:8000"
    assert settings.sidecar_only_routes == ("lineage-events", "lineage-reconcile-cron")


def test_the_route_table_built_from_the_rendered_values_points_in_cluster() -> None:
    """Not just that it binds — that the rows the proxy dispatches on carry the cluster addresses."""
    import gateway

    rows = {row.app_id: row.fallback_url for row in gateway._routes(GatewaySettings.model_validate(_RENDERED))}
    assert rows["compute"] == "http://rask-compute:8804"
    assert rows["controlplane"] == "http://rask-controlplane:8820"
    assert rows["catalog"] == "http://rask-catalog:2333"
    assert rows["medallion-producer"] == "http://rask-medallion-producer:8000"


def test_a_trailing_slash_on_the_prefix_does_not_double_up() -> None:
    assert GatewaySettings.model_validate({"RASK_API_PREFIX": "/api/"}).api_prefix == "/api"


def test_an_unparseable_flag_is_refused_at_startup_not_silently_false() -> None:
    """The reads this replaced answered False for `RASK_DOCS=treu`. A typo in a security-relevant
    flag that silently means "off" is luck, not a design — and the same read shape would silently
    mean "off" for a flag whose safe default is ON."""
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate({"RASK_DOCS": "treu"})
