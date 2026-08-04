"""Ray job entrypoint for the DUMMY silver hop (A11).

A script baked into the ray-lance image, exactly like ray_stage_job.py — jobs are artefacts, never
code shipped at submit time, so a transform change is an image rebuild and reproducible by
construction.

The transform itself lives in the SEALED `runners/dummy` project rather than here, so the job body
stays a thin parameterised shim and the mechanics it exercises are unit-tested outside the cluster.
That split is the point: this file is how the cluster invokes it; the runner is what is tested.

Env: FROM_URI TO_URI [BASE_VERSION] [RUN_ID] [LINEAGE_JSON]
"""

from __future__ import annotations

import sys

from dummy_runner.job import main


if __name__ == "__main__":
    sys.exit(main())
