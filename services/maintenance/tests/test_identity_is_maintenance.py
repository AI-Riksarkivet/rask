"""The compaction→maintenance rename is FINISHED — no surface still claims the old identity.

open_python-audit `MAINT-15`: the rename left the old name on wire-visible and reader-visible
surfaces — the OTel meter scope, the OpenLineage job NAMESPACE default (persisted into AGE on every
emitted event), and three module docstrings introducing the code as "the compaction service". The
first fix closed one of the four cited sites and was flipped FIXED; this pins all of them.

Deliberately NOT swept away: "compaction" as the name of the OPERATION the sweep performs
(``operation=compaction``, the ``COMPACTION`` facet marker, the job-name prefix, the
``compaction.*`` counters). The service is `maintenance`; compaction is a thing it does.
"""

from __future__ import annotations

import inspect
from pathlib import Path


def test_the_job_namespace_default_names_the_service_that_emits() -> None:
    from maintenance.core.config import MaintenanceSettings

    assert MaintenanceSettings().lineage_job_namespace == "maintenance", (
        "every emitted RunEvent's job namespace is persisted into AGE forever — it must name the maintenance service, not the pre-rename one"
    )


def test_the_otel_meter_scope_names_the_service() -> None:
    from maintenance.core import metrics

    source = Path(inspect.getsourcefile(metrics) or "").read_text()
    assert 'get_meter("lance.maintenance")' in source, "the meter's instrumentation scope still carries the pre-rename identity"
    assert "lance.compaction" not in source


def test_no_module_still_introduces_itself_as_the_compaction_service() -> None:
    """Swept over the whole package source rather than an import list — the two package-__init__
    docstrings survived the first fix precisely because no list named them."""
    import maintenance

    src_root = Path(inspect.getsourcefile(maintenance) or "").parent
    offenders = sorted(str(path.relative_to(src_root)) for path in src_root.rglob("*.py") if "compaction service" in path.read_text())
    assert not offenders, f"modules still claiming the pre-rename identity: {offenders}"
