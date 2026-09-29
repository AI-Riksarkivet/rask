"""Every Ray job the fleet can SUBMIT must exist in the image that runs it (R27 audit, 2026-07-28).

The medallion submits jobs through the Ray Jobs REST API with an ``entrypoint`` string that names an
absolute path inside the Ray image — ``python /home/ray/jobs/<job>.py``. Nothing validates that path at
submit time: a job the settings name but the image does not carry fails only on the cluster, as a job whose
logs say "No such file or directory", after the stage runner has already committed to the Dapr redelivery cycle.

That is not hypothetical — it is how the P7a IIIF head's Ray branch was dead on arrival:
An entrypoint setting defaulted to a job script the image did not carry, while
``.docker/ray-lance.dockerfile``'s COPY listed only the lance/stage/train jobs. These tests close the loop
in the unit tier, where it costs nothing.

They also pin the Ray image's Lance stack to the FLEET's, because this image reads and writes the same
blob-v2 datasets the services write: at the previously-pinned pylance 8.0.0 a blob column written by
pylance 9.0.0 could not be read row-aligned at ALL (``blob_handling="all_binary"`` raised, and the blob
descriptor's ``is_valid()`` lied), so a version split here is a correctness bug rather than a currency
preference. See docs/architecture/lance-blob-v2-findings.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_REPO = Path(__file__).parents[2]
_VALUES = _REPO / "chart" / "values.yaml"


def _chart_ray_tasks() -> list[dict[str, Any]]:
    """`medallion.rayTasks` as the chart declares it — the estate's statement of what Ray can run."""
    import yaml

    values = yaml.safe_load(_VALUES.read_text(encoding="utf-8"))
    tasks = values["medallion"]["rayTasks"]
    assert isinstance(tasks, list) and tasks, "chart/values.yaml declares no medallion.rayTasks"
    return tasks


# ── The CLUSTER image, which is the one KubeRay actually runs ──────────────────────────────────────
#
# Everything above gates `.docker/ray-lance.dockerfile` — the DEMO image behind `make ray-demo` and
# `deploy/ray-lance-demo.yaml`, which the chart does not deploy. The image the chart's KubeRay cluster
# runs is `.docker/ray-cluster.dockerfile`, and its baked job entrypoints were gated by nothing.
#
# That is the expensive direction. CLAUDE.md states the failure exactly: "The Ray lane submits
# `python /home/ray/jobs/<job>.py` — those scripts are baked by `.docker/ray-cluster.dockerfile` ... a
# job whose entrypoint the image lacks dies `exit 2` and the stage reports FAILED with nothing naming
# the image." The dockerfile's own comment records the same incident, quoting the runtime error:
# `python: can't open file '/home/ray/jobs/ray_stage_job.py': No such file or directory`.
#
# The gate is NOT the demo file's deletion — the finding's own note says so. It is that the SUBMITTED
# set and the BAKED set agree, derived from both sides so neither can be restated by hand.


def test_the_chart_declaration_parses_into_the_setting_that_reads_it() -> None:
    """The chart renders these rows as ONE JSON string; the producer parses that string back.

    A field the model forbids, or a row missing one it requires, renders perfectly and then fails at
    the producer's own construction — which is a CrashLoopBackOff at the cascade head, discovered on
    a cluster. `extra="forbid"` is what makes a typo'd key a failure at all, and this is where that
    failure is cheap.
    """
    from medallion.core.config import TaskDeclaration

    for task in _chart_ray_tasks():
        TaskDeclaration.model_validate(task)
