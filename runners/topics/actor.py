"""There is deliberately no ``compute_factory`` here — topics is corpus-global.

Toponymy fits the WHOLE corpus at once (it clusters the full 2-D atlas map and
names the resulting layers), so there is no honest per-batch compute:
``map_batches`` streams disjoint batches through parallel actors, and a
whole-corpus fit needs every row before it can emit its first output. A
``Stage(runner="topics")`` is therefore a modelling error, and this module makes
it fail AT RESOLUTION with this explanation instead of a bare import error.

The topics runner's two real drivable forms:

* **batch job** — ``runners/topics/worker.py``, run in this runner's sealed env
  (``uv run --project runners/topics python -m runners.topics.worker --db <db>``),
  or the same module submitted as a Ray Job.
* **online** — ``runners/topics/deployment.py`` (Ray Serve).

kg is job-only for the same whole-graph reason; it ships no actor module at all.
"""

from __future__ import annotations

from .errors import TopicsError


raise TopicsError(
    "topics is corpus-global (Toponymy fits the whole atlas map at once) — it cannot "
    "run as a per-batch pipeline stage. Drive it as a batch job: runners/topics/worker.py "
    "(`python -m runners.topics.worker --db <db>` in this runner's sealed env, or the same "
    "module as a Ray Job); online: runners/topics/deployment.py."
)
