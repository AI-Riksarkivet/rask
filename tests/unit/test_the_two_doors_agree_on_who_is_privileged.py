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
    blocks = [b for b in rendered.split("---") if "kind: Deployment" in b and re.search(rf"^  name: (?:[a-z0-9-]+-)?{re.escape(component)}$", b, re.MULTILINE)]
    assert blocks, f"no {component} Deployment in the render"
    return dict(re.findall(r"\{\s*name:\s*([A-Z0-9_]+),\s*value:\s*\"?([^\"}\n]*)\"?\s*\}", blocks[0]))


def _subjects(value: str) -> set[str]:
    return {s.strip() for s in value.split(",") if s.strip()}


@pytest.fixture(scope="module")
def rendered() -> str:
    return _helm_template("auth.dedicatedServiceCredentials=true", "medallion.enabled=true")


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
