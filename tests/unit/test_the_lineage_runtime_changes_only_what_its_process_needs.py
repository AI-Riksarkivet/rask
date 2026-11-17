"""`services.lineage.runtime` swaps the lineage container's process and nothing the pod is given (ADR 0239).

The Rust binary starts from the environment the chart renders for the Python service, as the same ServiceAccount, with
the same mounts, probes, ports, securityContext and NetworkPolicy (ADR 0238). So a `rust` render differs from a `python`
one in the lineage container's image, its command, its preStop (the image has no shell, so the kubelet sleeps), its
resource tier and the variables only the Python process reads, and in no other byte of any manifest. The render is the
production overlay, so the NetworkPolicy, the HPA and the disruption budget are in what is compared.
"""

from __future__ import annotations

import copy
import subprocess

import pytest

from tests.unit.chart_render import OBSERVABILITY_ARGS, PROD_ARGS, env_of, render, render_text


_PIN = ("--set-string", "image.digests.lance-lineage-rust=sha256:" + "a" * 64)
_RUST = ("--set", "services.lineage.runtime=rust")

#: What only the Python process reads: pyarrow's pool, the Python OTel distro's credential entry point, its logging and
#: FastAPI instrumentations, and the semantic-convention switch of its instrumentations.
_PYTHON_ONLY = {
    "ARROW_DEFAULT_MEMORY_POOL",
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
    "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED",
    "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
    "OTEL_SEMCONV_STABILITY_OPT_IN",
}


def _lineage(docs: tuple[dict, ...]) -> tuple[list[dict], dict]:
    """Every document but the lineage Deployment, and that Deployment."""
    others = [doc for doc in docs if not (doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "rask-lineage")]
    (deployment,) = [doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "rask-lineage"]
    return others, deployment


def test_rust_changes_the_lineage_containers_process_and_nothing_its_pod_is_given() -> None:
    python_others, python = _lineage(render(*PROD_ARGS, *OBSERVABILITY_ARGS, *_PIN))
    rust_others, rust = _lineage(render(*PROD_ARGS, *OBSERVABILITY_ARGS, *_PIN, *_RUST))

    assert rust_others == python_others
    (py,) = python["spec"]["template"]["spec"]["containers"]
    (rs,) = rust["spec"]["template"]["spec"]["containers"]
    assert rs["image"] == "ghcr.io/example/rask/lance-lineage-rust@sha256:" + "a" * 64
    assert py["image"] == "ghcr.io/example/rask/lance-rest-catalog:1.2.3"
    assert py["args"][:3] == ["opentelemetry-instrument", "uvicorn", "lineage.main:app"]
    assert rs["args"] == ["serve", *py["args"][3:]]
    assert rs["lifecycle"] == {"preStop": {"sleep": {"seconds": 5}}}
    assert rs["resources"] == {"requests": {"cpu": "250m", "memory": "64Mi"}, "limits": {"cpu": "1", "memory": "256Mi"}}
    assert set(env_of(py)) - set(env_of(rs)) == _PYTHON_ONLY
    assert env_of(rs) == {name: value for name, value in env_of(py).items() if name not in _PYTHON_ONLY}

    unchanged = copy.deepcopy(rust)
    unchanged["spec"]["template"]["spec"]["containers"] = [{**rs, **{key: py[key] for key in ("image", "args", "lifecycle", "resources", "env")}}]
    assert unchanged == python


def test_rust_without_a_pinned_build_is_refused_naming_the_pin() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*PROD_ARGS, *_RUST)

    assert "image.tags.lance-lineage-rust or image.digests.lance-lineage-rust" in refused.value.stderr
