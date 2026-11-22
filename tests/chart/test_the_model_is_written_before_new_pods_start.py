"""The authorization model is written before an upgrade's new pods start ([[LH-201]]).

A service resolves the model its image carries at boot. Written after the upgrade, the model a new image
carries does not exist when that image's pods start, so they wait and then fail closed. `pre-upgrade`
runs the hook against the release's new image before any of its resources change; `post-install` stays
for a first install, where OpenFGA itself is one of the resources and there is nothing to write into yet. The hook logs
at the level the release names, as every lakehouse process does.
"""

from __future__ import annotations

from tests.chart.chart_render import DEFAULT_ARGS, env_of, render


def test_the_model_hook_runs_before_an_upgrade_and_after_a_first_install_at_the_release_log_level() -> None:
    jobs = [
        doc
        for doc in render(*DEFAULT_ARGS, "--set-string", "observability.logLevel=ERROR")
        if doc.get("kind") == "Job" and doc["metadata"]["name"].endswith("-openfga-model")
    ]
    assert len(jobs) == 1, f"the render holds {len(jobs)} openfga-model Jobs"

    hooks = {hook.strip() for hook in jobs[0]["metadata"]["annotations"]["helm.sh/hook"].split(",")}
    [container] = jobs[0]["spec"]["template"]["spec"]["containers"]

    assert hooks == {"post-install", "pre-upgrade"}
    assert env_of(container).get("RASK_LOG_LEVEL") == "ERROR"
