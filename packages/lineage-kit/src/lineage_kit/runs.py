"""Deterministic run ids.

A producer that may deliver a run event more than once names the run with :func:`run_id_for`, so a
redelivery MERGEs onto the same run instead of duplicating it.
"""

from __future__ import annotations

import uuid


#: uuid5 namespace for deterministic run ids. ALIGNED to the lance-ns URL: ``service_kit.openlineage.run_id_for``
#: seeds from the same URL, and every deterministic run id already in the lineage graph derives from it, so a
#: different namespace here would break redelivery MERGE for every producer that moves between the two.
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Borg93/lance-ns")


def run_id_for(seed: str) -> str:
    """A spec-valid UUID run id that is STABLE for ``seed`` — so an at-least-once
    redelivery MERGEs onto the same run instead of duplicating it. Keep the
    human-readable seed as the job name for correlation, never as the id itself."""
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, seed))
