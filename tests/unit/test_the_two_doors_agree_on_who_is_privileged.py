"""A client of two governed doors can present ONE token, so the doors must agree who is privileged.

`dapr_auth.service_principal` has two branches. A PRIVILEGED subject must present
`service-token-<identity>` from the secret store and the shared `APP_API_TOKEN` is refused; every other
allowlisted subject must present the shared token and a dedicated one is refused
(`CredentialRejected("invalid service token")`). Both are correct in isolation.

They are not correct in DISAGREEMENT. `catalog_register.credential` — the one credential builder every
medallion client uses — resolves the dedicated token when the store holds one and sends it to whatever
it is calling. So a subject listed as privileged at one door and merely allowlisted at another cannot
satisfy both: whichever token it sends, one door refuses it.

MEASURED live 2026-09-05 on the cascade-lag detector. `service-medallion-producer` is privileged at the
catalog (`LANCE_PRIVILEGED_SUBJECTS`, rendered) and only allowlisted at lineage
(`LINEAGE_SERVICE_SUBJECTS`, rendered; `LINEAGE_PRIVILEGED_SUBJECTS` rendered NOWHERE, though
`lineage/core/config.py:77` reads it). The catalog read succeeded and the lineage read answered
`401 invalid service token` on every edge — so the detector had a published version and no consumed
version for every lane, which `lag_for_edge` reports UNKNOWN and `record_edge_lag` publishes as
nothing. An empty series, from a detector whose entire job is to notice absence.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_invariants import _helm_template


def _env(rendered: str, component: str) -> dict[str, str]:
    """One component's Deployment env, matched on the RELEASE-SUFFIX rather than a literal name.

    `lance.fullname` prefixes with the release only when one is set, so `helm template` with no release
    renders the Deployment as `lineage` where the cluster holds `rask-lineage`. Matching the literal
    silently found nothing and every assertion here passed on an empty dict.
    """
    blocks = [
        b
        for b in rendered.split("---")
        if "kind: Deployment" in b and re.search(rf"^  name: (?:[a-z0-9-]+-)?{re.escape(component)}$", b, re.MULTILINE)
    ]
    assert blocks, f"no {component} Deployment in the render"
    return dict(re.findall(r"\{\s*name:\s*([A-Z0-9_]+),\s*value:\s*\"?([^\"}\n]*)\"?\s*\}", blocks[0]))


def _subjects(value: str) -> set[str]:
    return {s.strip() for s in value.split(",") if s.strip()}


@pytest.fixture(scope="module")
def rendered() -> str:
    return _helm_template("auth.dedicatedServiceCredentials=true", "medallion.enabled=true")


def test_lineage_renders_the_privileged_list_its_own_door_reads(rendered: str) -> None:
    """`lineage/core/config.py:77` declares `LINEAGE_PRIVILEGED_SUBJECTS` and the chart set it nowhere,
    so lineage's privileged branch was unreachable: every allowlisted subject fell to the shared-token
    comparison no matter what the estate had provisioned for it."""
    assert _env(rendered, "lineage").get("LINEAGE_PRIVILEGED_SUBJECTS"), (
        "lineage's door reads a privileged list the chart never renders — its privileged branch is dead code"
    )


def test_a_subject_privileged_at_the_catalog_is_privileged_at_lineage(rendered: str) -> None:
    """The invariant, stated as the client experiences it. Not "the lists are equal" — lineage admits
    subjects the catalog never sees (`notifications`) and the catalog admits read-only ones that need
    no dedicated credential. What must hold is that no subject is privileged at one and ordinary at the
    other, for every subject BOTH doors admit."""
    catalog = _env(rendered, "catalog")
    lineage = _env(rendered, "lineage")
    both = _subjects(catalog.get("LANCE_SERVICE_SUBJECTS", "")) & _subjects(lineage.get("LINEAGE_SERVICE_SUBJECTS", ""))
    assert both, "no subject reaches both doors — this gate is checking nothing"
    catalog_privileged = _subjects(catalog.get("LANCE_PRIVILEGED_SUBJECTS", "")) & both
    lineage_privileged = _subjects(lineage.get("LINEAGE_PRIVILEGED_SUBJECTS", "")) & both
    assert catalog_privileged == lineage_privileged, (
        f"a client of both doors cannot authenticate to both: privileged at the catalog only "
        f"{sorted(catalog_privileged - lineage_privileged)}, at lineage only {sorted(lineage_privileged - catalog_privileged)}"
    )


def test_the_lag_detectors_own_subject_is_the_one_that_must_agree(rendered: str) -> None:
    """Named explicitly because it is the caller that found this, and because the failure is silent:
    a detector that cannot read publishes nothing, and nothing looks exactly like a healthy cascade."""
    producer = "service-medallion-producer"
    lineage = _env(rendered, "lineage")
    assert producer in _subjects(lineage.get("LINEAGE_SERVICE_SUBJECTS", "")), "the detector is not admitted at all"
    assert producer in _subjects(lineage.get("LINEAGE_PRIVILEGED_SUBJECTS", "")), (
        "the detector is admitted but its dedicated token is refused — 401 on every consumed read"
    )


def test_dedicated_credentials_OFF_renders_neither_list(rendered: str) -> None:  # noqa: ARG001
    """The switch stays one switch. An estate that has not provisioned dedicated tokens must see the
    pre-existing shared-token behaviour at BOTH doors, or turning the feature off half-breaks it."""
    off = _helm_template("auth.dedicatedServiceCredentials=false", "medallion.enabled=true")
    assert not _env(off, "catalog").get("LANCE_PRIVILEGED_SUBJECTS")
    assert not _env(off, "lineage").get("LINEAGE_PRIVILEGED_SUBJECTS")
