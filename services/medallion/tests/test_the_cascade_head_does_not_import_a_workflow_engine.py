"""The cascade head must be importable without a workflow engine.

Condition 3 in the owner's words: "Dapr Workflow and Ray are things the lakehouse can be driven BY,
never things it depends ON." An import is a dependency, and measured 2026-09-13 `import
medallion.producer` pulled in `dapr.ext.workflow` and its whole durabletask stack — the client, the
deterministic runtime, the generated protobufs.

THE CHAIN WAS NOT WHERE ANYONE LOOKED. `producer.py` starts the workflow RUNTIME only
`if settings.quality_review_enabled or settings.ray_enabled`, and says so: "with neither feature on,
this app hosts no workflow and should run no engine". That gate is real, and it is about threads. The
engine still arrived through `api/promotions.py` — a router the producer mounts unconditionally —
which imported `WorkflowStatus` at module scope and `PromotionSpec, promotion_review` from
`medallion.workflow`, whose own body is `import dapr.ext.workflow as wf`. The STAGE RUNNER arrived by a
second route through the same adapter: `services/transform.py` imports `promotion_hold` at module
scope, and that module took `PromotionSpec` from `medallion.workflow` too.

A PRIOR MEASUREMENT CALLED CONDITION 3 SATISFIED by reading two call sites (`producer.py:121`,
`stage_runner.py:93`). Both are genuinely lazy. Neither is the import graph, which is why this test
imports the module in a SUBPROCESS and asks `sys.modules` rather than reading any source: by the time
a suite reaches this file, some earlier test has already imported the engine into THIS interpreter, so
an in-process check would pass no matter what the producer does.

`medallion.workflow` itself is the engine ADAPTER and is expected to import the engine — an adapter may
import the thing it adapts. What must not happen is a door on the cascade head reaching through it.
"""

from __future__ import annotations

import subprocess
import sys


def _imports_engine(module: str) -> bool:
    """Import ``module`` in a clean interpreter and report whether the workflow engine came with it."""
    probe = f"import sys;__import__({module!r});print(any(m.startswith('dapr.ext.workflow') for m in sys.modules))"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, f"probe failed for {module}: {result.stderr[-2000:]}"
    return result.stdout.strip().endswith("True")


def test_importing_the_cascade_head_does_not_pull_in_dapr_workflow() -> None:
    """THE GATE. `medallion.producer` is the cascade head — `/produce`, `/ingest-media`, `/train`."""
    assert not _imports_engine("medallion.producer"), (
        "importing the cascade head pulled in dapr.ext.workflow — the lakehouse now DEPENDS ON a workflow engine rather than being driven by one (condition 3)"
    )


def test_the_promotions_router_does_not_pull_in_dapr_workflow() -> None:
    """The specific door that carried it: mounted unconditionally, so its imports are the producer's."""
    assert not _imports_engine("medallion.api.promotions")


def test_importing_the_stage_runner_does_not_pull_in_dapr_workflow() -> None:
    """The cascade's OTHER entrypoint, and it arrived by a second route: `services/transform.py`
    imports `promotion_hold` at module scope, which took `PromotionSpec` from the engine adapter. Both
    medallion entrypoints measured as pulling the engine before this change."""
    assert not _imports_engine("medallion.stage_runner")


def test_the_transform_path_does_not_pull_in_dapr_workflow() -> None:
    """`transform.py` is where the cascade actually runs a stage; its own `medallion.workflow` import
    is already lazy, and this pins that nothing it imports at module scope undoes that."""
    assert not _imports_engine("medallion.services.transform")


def test_the_promotion_payload_carries_no_engine() -> None:
    """`PromotionSpec` is plain pydantic. It sat in the engine adapter, which is what made every
    consumer of the payload a consumer of the engine."""
    assert not _imports_engine("medallion.schemas.promotion")


def test_the_workflow_adapter_still_imports_its_engine() -> None:
    """The other half, so the fix cannot be 'lazy-import everything until the module means nothing':
    `medallion.workflow` IS the Dapr Workflow adapter and is supposed to import Dapr Workflow."""
    assert _imports_engine("medallion.workflow")
