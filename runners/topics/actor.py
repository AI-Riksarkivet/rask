"""There is deliberately no ``compute_factory`` here — topics is corpus-global.

Toponymy fits the WHOLE corpus at once (it clusters the full 2-D atlas map and
names the resulting layers), so there is no honest per-batch compute:
``map_batches`` streams disjoint batches through parallel actors, and a
whole-corpus fit needs every row before it can emit its first output. A
``Stage(runner="topics")`` is therefore a modelling error, and this module makes
it fail AT RESOLUTION with this explanation instead of a bare import error.

The topics runner's two real drivable forms:

* **batch job** — ``runners/topics/worker.py`` via ``ratch.core.jobs.run_runner``
  (a Ray Job when ``RATCH_RAY_ENABLED=1``, in-process otherwise); ``make topics``
  is the sealed-env local convenience.
* **online** — ``runners/topics/deployment.py`` (Ray Serve).

kg is job-only for the same whole-graph reason; it ships no actor module at all
(``resolve_runner_actor`` points job-only runners at ``ratch.core.jobs``).
"""

from __future__ import annotations

from ratch.errors import RatchError


raise RatchError(
    "topics is corpus-global (Toponymy fits the whole atlas map at once) — it cannot "
    "run as a per-batch pipeline stage. Drive it as a job: `ratch feature topics` "
    "(ratch.core.jobs) or `make topics` (sealed env); online: runners/topics/deployment.py."
)
