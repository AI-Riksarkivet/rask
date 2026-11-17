"""A warehouse runs without the seal only where git declares it, and the catalog reads that declaration (ADR 0242).

Every file a commit adds is sealed, and the catalog identity's store policy refuses a put over a sealed key. A warehouse
whose store does not enforce that registers, and commits without a tag where the store refuses one, only when
`catalog.declared.warehouses[].seal` (or `catalog.declared.estateWarehouseSeal` for the estate's own) says
`detection_only`: the chart renders those buckets into the catalog's `LANCE_SEAL_DETECTION_ONLY_BUCKETS`, empty by
default, and a value other than `required` or `detection_only` fails the render rather than reading as either.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, REPO, render, render_chart_text


def _detection_only(*extra: str) -> str:
    catalog = next(doc for doc in render(*DEFAULT_ARGS, *extra) if doc.get("kind") == "Deployment" and doc["metadata"]["name"].endswith("-catalog"))
    env = [entry for container in catalog["spec"]["template"]["spec"]["containers"] for entry in container.get("env", [])]
    return next(entry["value"] for entry in env if entry["name"] == "LANCE_SEAL_DETECTION_ONLY_BUCKETS")


@pytest.mark.parametrize(
    ("extra", "buckets"),
    [
        pytest.param((), "", id="nothing-declared-every-warehouse-requires-the-seal"),
        pytest.param(
            (
                "--set-json",
                'catalog.declared.warehouses=[{"id":"wh-a","project":"default","seal":"detection_only"},'
                '{"id":"wh-b","project":"default","bucket":"bucket-b","seal":"detection_only"},{"id":"wh-c","project":"default"}]',
                "--set",
                "catalog.declared.estateWarehouseSeal=detection_only",
            ),
            "wh-a,bucket-b,lance-catalog",
            id="declared-warehouses-and-the-estates-own",
        ),
    ],
)
def test_the_catalog_reads_which_warehouses_git_declares_detection_only(extra: tuple[str, ...], buckets: str) -> None:
    assert _detection_only(*extra) == buckets


def test_a_seal_that_is_neither_required_nor_detection_only_fails_the_render() -> None:
    declared = 'catalog.declared.warehouses=[{"id":"wh-a","project":"default","seal":"detection-only"}]'
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_chart_text(REPO / "chart", *DEFAULT_ARGS, "--set-json", declared)
    assert "required` or `detection_only`" in refused.value.stderr
