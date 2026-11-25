"""The catalog is told whether a branch delete hides the branch by a chart value of its own ([[LH-604]], ADR 0229)."""

import pytest

from tests.chart.chart_render import containers, env_of, render


_BASE = ("--set", "image.localImages=true")


@pytest.mark.parametrize(("extra", "told"), [((), "true"), (("--set", "catalog.branchTrash.enabled=false"), "false")])
def test_the_render_tells_the_catalog_whether_a_branch_delete_hides(extra: tuple[str, ...], told: str) -> None:
    docs = render(*_BASE, *extra)

    (catalog,) = [container for name, process, container in containers(docs) if f"{name}/{process}" == "Deployment/rask-catalog/catalog"]

    assert env_of(catalog)["LANCE_BRANCH_TRASH_ENABLED"] == told
