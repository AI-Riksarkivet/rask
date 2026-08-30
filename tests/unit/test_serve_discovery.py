"""Producer discovery from Ray Serve — deploying a model IS registering it.

Model endpoints in this estate are Ray Serve deployments; the registry's primary source is
the Serve control plane (`GET /api/serve/applications/`, `ServeInstanceDetails`), with the
labeling contract declared in each deployment's hot-updatable ``user_config``. These tests
pin the walk (RUNNING-only, route_prefix → proxy URL, declared returns/inputs), the merge
precedence (env config is operator INTENT and wins over observation), the proxy-base
derivation from Serve's own http_options, and the fail-honest edges (unreachable dashboard ⇒
config + mock, never an error).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from annotator.api.v1.endpoints.assist import backend_for, producer_listing
from annotator.api.v1.endpoints.serve_discovery import (
    discovered_backends,
    parse_applications,
    reset_cache,
)


#: The v2 REST response, shaped exactly as `ray.serve.schema.ServeInstanceDetails` (2.56.1)
#: renders it — applications keyed by name, deployments keyed by name, user_config under
#: deployment_config, statuses from ApplicationStatus.
DETAILS: dict[str, Any] = {
    "http_options": {"host": "0.0.0.0", "port": 8000},
    "applications": {
        "sam2": {
            "name": "sam2",
            "route_prefix": "/sam2",
            "status": "RUNNING",
            "deployments": {
                "Segmenter": {
                    "name": "Segmenter",
                    "status": "HEALTHY",
                    "deployment_config": {
                        "user_config": {
                            "labeling": {
                                "producer": "sam",
                                "returns": ["polygon"],
                                "inputs": ["points", "region"],
                            }
                        }
                    },
                }
            },
        },
        "qwen-vl": {
            "name": "qwen-vl",
            "route_prefix": "/qwen-vl",
            "status": "RUNNING",
            "deployments": {
                "VLM": {
                    "name": "VLM",
                    "status": "HEALTHY",
                    "deployment_config": {"user_config": {"labeling": {"returns": ["bbox"], "inputs": ["prompt"]}}},
                }
            },
        },
        "transcribe": {
            # A Serve app with NO labeling declaration — the HTR/transcribe deployments
            # exist on the same cluster and must not be offered as interactive producers.
            "name": "transcribe",
            "route_prefix": "/transcribe",
            "status": "RUNNING",
            "deployments": {"TrOCR": {"deployment_config": {"user_config": None}}},
        },
        "deploying": {
            "name": "deploying",
            "route_prefix": "/almost",
            "status": "DEPLOYING",
            "deployments": {"X": {"deployment_config": {"user_config": {"labeling": {"returns": ["bbox"]}}}}},
        },
    },
}


def test_the_walk_offers_declared_RUNNING_deployments_at_the_proxy_route() -> None:
    backends = parse_applications(DETAILS, "http://serve:8000")

    assert set(backends) == {"sam", "vlm"}
    assert backends["sam"].url == "http://serve:8000/sam2"
    assert backends["sam"].returns == ["polygon"]
    assert backends["sam"].inputs == ["points", "region"]
    # No `producer` key ⇒ the deployment name, lowercased, is the producer.
    assert backends["vlm"].url == "http://serve:8000/qwen-vl"
    # The transcribe app (no labeling block) and the DEPLOYING app are absent — an app that
    # is not RUNNING is not an endpoint anyone can call.
    assert "trocr" not in backends


def test_env_config_is_operator_INTENT_and_wins_over_observation() -> None:
    discovered = parse_applications(DETAILS, "http://serve:8000")
    s = SimpleNamespace(assist_backends={"sam": "http://pinned:1"}, assist_url=None)

    assert backend_for(s, "sam-click", discovered) == "http://pinned:1"
    assert backend_for(s, "vlm-anything", discovered) == "http://serve:8000/qwen-vl"

    by_name = {p.name: p for p in producer_listing(s, discovered=discovered).producers}
    assert by_name["vlm"].configured is True
    assert by_name["vlm"].returns == ["bbox"]
    assert by_name["vlm"].inputs == ["prompt"]
    assert by_name["sam"].configured is True


def test_an_unreachable_dashboard_degrades_to_config_plus_mock() -> None:
    reset_cache()

    def _refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("cluster down")

    # A REAL client over a refusing transport: the refusal travels httpx's own send path out of
    # `.get`, so what the production `except (httpx.HTTPError, ValueError)` sees is what a real
    # unreachable dashboard would deliver — not a stand-in `.get` whose own body was the assertion.
    with httpx.Client(transport=httpx.MockTransport(_refuse)) as http:
        assert discovered_backends(http, "http://ray:8265", None) == {}


def test_discovery_is_TTL_cached_per_dashboard() -> None:
    reset_cache()
    calls: list[str] = []

    class _Http:
        def get(self, url: str, timeout: float) -> Any:
            calls.append(url)
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: DETAILS)

    http: Any = _Http()
    first = discovered_backends(http, "http://ray:8265", "http://serve:8000", now=100.0)
    second = discovered_backends(http, "http://ray:8265", "http://serve:8000", now=105.0)
    assert first == second and len(calls) == 1
    discovered_backends(http, "http://ray:8265", "http://serve:8000", now=200.0)
    assert len(calls) == 2
    assert calls[0] == "http://ray:8265/api/serve/applications/"


def test_the_proxy_base_derives_from_serves_own_http_options_when_unpinned() -> None:
    reset_cache()

    class _Http:
        def get(self, url: str, timeout: float) -> Any:
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: DETAILS)

    http: Any = _Http()
    backends = discovered_backends(http, "http://ray-head:8265", None, now=0.0)
    # Dashboard host + the port Serve reports (8000) — not the dashboard's own port.
    assert backends["sam"].url == "http://ray-head:8000/sam2"


@pytest.mark.parametrize("garbage", [None, [], {"applications": "nope"}, {"applications": {"a": 3}}])
def test_a_malformed_response_yields_nothing_rather_than_raising(garbage: Any) -> None:
    assert parse_applications(garbage, "http://serve:8000") == {}
