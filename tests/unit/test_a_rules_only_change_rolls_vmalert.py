"""A change to the alert rules rolls vmalert, or the estate keeps evaluating the rules it started with.

vmalert reads its `-rule` file when it starts, and the chart passes no `-configCheckInterval`, so a rules ConfigMap
that changes under a running pod changes nothing vmalert evaluates. Only a new pod loads it, and a Deployment rolls only
when its pod template changes. Measured on the estate 2026-10-04: the running vmalert started 2026-10-02 12:40, and two
rules changes were deployed after it, LH-064's `LineageSignatureRefusedForAListedSigner` among them.

The rules file reaches the chart through `.Files.Get`, which no `--set` can change, so the chart is copied and the copy
rendered with a line appended to `alerting/rules.yml`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from tests.unit.chart_render import DEFAULT_ARGS, REPO, render, render_chart_text
from tests.unit.chart_yaml import FAST_LOADER


ALERTING_ON = (*DEFAULT_ARGS, "--set", "observability.alerting.enabled=true")


def _vmalert_pod_template(docs: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "rask-vmalert":
            return doc["spec"]["template"]
    raise AssertionError("vmalert did not render with alerting on")


def test_a_rules_only_change_rolls_vmalerts_pod(tmp_path: Path) -> None:
    changed = tmp_path / "chart"
    shutil.copytree(REPO / "chart", changed)
    with (changed / "alerting" / "rules.yml").open("a") as rules:
        rules.write("# a rules-only change\n")

    deployed = _vmalert_pod_template(render(*ALERTING_ON))
    rendered = tuple(doc for doc in yaml.load_all(render_chart_text(changed, *ALERTING_ON), Loader=FAST_LOADER) if isinstance(doc, dict))

    assert _vmalert_pod_template(rendered) != deployed, "the rules changed and vmalert's pod template did not, so no rollout loads the new rules"
