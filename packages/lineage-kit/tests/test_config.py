"""Env-driven transport config: RASK_* first, official OpenLineage names as aliases."""

from __future__ import annotations

import pytest
from lineage_kit import ClientEmitter, LineageSettings, NoopEmitter, build_emitter


def test_defaults_are_noop_friendly() -> None:
    s = LineageSettings()
    assert s.endpoint is None
    assert s.api_key is None
    assert s.namespace == "rask"
    assert s.endpoint_path == "api/v1/lineage"
    assert s.transport == "auto"


def test_rask_env_vars_configure_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    monkeypatch.setenv("RASK_LINEAGE_API_KEY", "sekrit")
    monkeypatch.setenv("RASK_LINEAGE_NAMESPACE", "htr")
    monkeypatch.setenv("RASK_LINEAGE_TIMEOUT", "2.5")
    s = LineageSettings()
    assert s.endpoint == "http://marquez:5000"
    assert s.api_key == "sekrit"
    assert s.namespace == "htr"
    assert s.timeout == 2.5


def test_official_openlineage_names_are_accepted_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENLINEAGE_URL", "http://marquez:5000")
    monkeypatch.setenv("OPENLINEAGE_NAMESPACE", "htr")
    s = LineageSettings()
    assert s.endpoint == "http://marquez:5000"
    assert s.namespace == "htr"


def test_rask_name_wins_over_the_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://rask-wins:5000")
    monkeypatch.setenv("OPENLINEAGE_URL", "http://alias-loses:5000")
    assert LineageSettings().endpoint == "http://rask-wins:5000"


def test_auto_transport_without_endpoint_is_noop() -> None:
    assert isinstance(build_emitter(), NoopEmitter)


def test_auto_transport_with_endpoint_is_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    assert isinstance(build_emitter(), ClientEmitter)


def test_forced_noop_overrides_a_configured_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_ENDPOINT", "http://marquez:5000")
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "noop")
    assert isinstance(build_emitter(), NoopEmitter)


def test_console_transport_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "console")
    assert isinstance(build_emitter(), ClientEmitter)


def test_http_forced_without_endpoint_degrades_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_LINEAGE_TRANSPORT", "http")
    assert isinstance(build_emitter(), NoopEmitter)
