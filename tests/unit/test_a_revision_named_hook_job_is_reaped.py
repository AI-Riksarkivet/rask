"""A hook Job whose name carries the release revision must reap itself.

[[XC-053]]. Helm's `hook-delete-policy: before-hook-creation` removes the PREVIOUS hook of the same
NAME. Every bootstrap Job here is named `<fullname>-<what>-<bootstrapRev>`, so each revision mints a
new name and the delete policy never matches its predecessor — the husk stays, holding its completed
pod, one per `helm upgrade` forever.

THE ESTATE HAS PAID FOR THIS ONCE AND WROTE IT DOWN. `openfga-migrate.yaml:17-19` carries the lesson in
its own comment — "Each revision's job carries a -r<revision> name, so Helm's before-hook-creation
delete policy never matches the previous one — without a TTL every `helm upgrade` left another
Completed husk behind (14 were sitting in `kubectl get pods`)". Five of the six revision-named Jobs
learned from it; `minio-scoped-users` did not, and 17 of its husks were counted live on 2026-09-16
(r146..r163), each retaining its pod.

WHY A GATE AND NOT AN EDIT. The cost is legibility, not compute, and legibility is what this estate
keeps losing: four orphaned durables ([[LH-127]]) and a split image stem ([[LH-169]]) were both hard to
see for the same reason — a `kubectl get` full of things nobody reaps. A one-line fix to one template
leaves the next revision-named Job to relearn it.

Read off the SOURCE rather than a render: the rule is about how a template is written, and a render
under one set of values cannot show that a conditional Job was skipped rather than compliant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


CHART = Path(__file__).resolve().parents[2] / "chart" / "templates"

#: A Job whose metadata.name interpolates the bootstrap revision — the shape `before-hook-creation`
#: cannot reap, because the name differs every release.
_REVISION_NAMED = re.compile(r"^\s*name:.*bootstrapRev", re.MULTILINE)


def _revision_named_job_templates() -> list[Path]:
    return sorted(p for p in CHART.glob("*.yaml") if "kind: Job" in p.read_text(encoding="utf-8") and _REVISION_NAMED.search(p.read_text(encoding="utf-8")))


def test_the_chart_still_has_revision_named_jobs() -> None:
    """Without this the parametrized assertion below would pass by iterating nothing."""
    found = _revision_named_job_templates()

    assert len(found) >= 5, f"expected the bootstrap Job family, found {[p.name for p in found]}"


@pytest.mark.parametrize("template", _revision_named_job_templates(), ids=lambda p: p.name)
def test_a_revision_named_job_carries_a_ttl(template: Path) -> None:
    """`hook-delete-policy` is not a substitute: it matches by name, and the name is what changes."""
    body = template.read_text(encoding="utf-8")

    assert "ttlSecondsAfterFinished" in body, (
        f"{template.name} names its Job with the release revision, so Helm's before-hook-creation policy "
        "never matches the previous one — without ttlSecondsAfterFinished its husks accumulate one per "
        "upgrade (17 were counted live for minio-scoped-users on 2026-09-16)"
    )
