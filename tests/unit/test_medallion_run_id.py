"""``build_run_event``'s run id must be INJECTIVE — it is the MERGE key for the ``(:Run)`` node in AGE.

THE DEFECT. The project-qualified seed was ``f"{project}-{operation}-{token}"``, and `-` is a character
BOTH caller-supplied fields may contain: ``PROJECT_PATTERN``
(``service_kit.lakehouse.warehouse_registry``) is ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}`` and
``SAFE_TOKEN_PATTERN`` (``medallion.services.trigger_guards``) admits ``-`` as well. A separator
the fields can themselves contain does not separate anything — it can be forged out of them — and the
consequence here is not cosmetic: the lineage service MERGEs on this id, so two tenants that render the
same seed land on ONE ``(:Run)`` node and their lineage cross-wires.

WHY THE PAIR BELOW IS THE HONEST ONE. The obvious collision needs two different ``operation`` values,
which is not reachable: ``operation`` is per-stage runner env config (``MEDALLION_OPERATION``,
``medallion.core.config``), fixed for a deployment. The pair used here holds ``operation`` CONSTANT and
varies only ``project`` and ``token``, the two fields a caller supplies — so it is reachable by a
caller, which is what makes this a defect rather than a curiosity.

This is the third site of the same shape; ``services/flows/tests/test_run_id.py`` and the ingest plane's
equivalent pin it for theirs, each keeping a copy of the OLD derivation so the premise stays checkable
rather than becoming folklore.
"""

from __future__ import annotations

import re
import uuid

import pytest

from medallion.schemas.events import build_run_event
from medallion.services.trigger_guards import SAFE_TOKEN_PATTERN
from service_kit.lakehouse.warehouse_registry import PROJECT_PATTERN


#: The pair that collided under the `-` join, with ONE operation — see the module docstring. Both
#: rendered ``acme-embed_features-evil-embed_features-tok1``.
COLLIDING_PAIR: tuple[tuple[str, str, str], tuple[str, str, str]] = (
    ("acme", "embed_features", "evil-embed_features-tok1"),
    ("acme-embed_features-evil", "embed_features", "tok1"),
)


def _run_id(project: str | None, operation: str, token: str) -> str:
    """The id as the real builder derives it — through ``build_run_event``, not a re-implementation of
    it, so a future change to the derivation cannot pass this suite by leaving a copy behind."""
    event = build_run_event(
        operation=operation,
        author=None,
        job_namespace="ns",
        inputs=[],
        output_namespace="silver",
        output_name="silver$features",
        token=token,
        project=project,
    )
    return str(event["run"]["runId"])


def test_two_projects_never_share_a_run_id() -> None:
    """The regression itself, in the exact shape a caller could reach."""
    first, second = COLLIDING_PAIR

    assert _run_id(*first) != _run_id(*second)


def test_the_project_still_scopes_the_token() -> None:
    """The plain case the qualification exists for: same token, two tenants, two runs."""
    assert _run_id("acme", "embed_features", "tok1") != _run_id("globex", "embed_features", "tok1")


@pytest.mark.parametrize(
    ("label", "pattern", "candidate"),
    [
        ("project", PROJECT_PATTERN, "ac\x00me"),
        ("token", SAFE_TOKEN_PATTERN, "to\x00k"),
    ],
)
def test_no_validated_field_may_contain_the_separator(label: str, pattern: str, candidate: str) -> None:
    """THE PREMISE THE FIX RESTS ON, pinned against the validators themselves rather than against a
    comment about them.

    NUL is a safe separator for exactly one reason: no field joined by it can contain it. That is a
    property of ``PROJECT_PATTERN`` and ``SAFE_TOKEN_PATTERN``, not of this module — so a future
    widening of either (to accept a raw byte-ish id, say) would silently restore the forgeable join
    with every test above still green. This is the test that would go red instead.
    """
    assert re.fullmatch(pattern, candidate) is None, f"{label} now admits NUL — the run-id join is forgeable again"


@pytest.mark.parametrize("project", ["acme"])
def test_the_id_is_still_a_bare_uuid_string(project: str | None) -> None:
    """OpenLineage requires ``runId`` to be a UUID. The NUL rides the SEED, never the wire — a
    control character reaching the emitted event would be a spec violation, not a fix."""
    run_id = _run_id(project, "embed_features", "tok1")

    assert str(uuid.UUID(run_id)) == run_id
