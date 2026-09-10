"""A subject named PRIVILEGED must also be one the service door ADMITS.

the lakehouse register, row H14 (drained 2026-09-10; in git history). `dapr_auth.service_principal` asks two questions in order, and the
allowlist is the first: *"may this SUBJECT use the door at all?"*, then *"may THIS CALLER be that
subject?"*. So a name that appears only in the privileged list is refused before its credential is ever
examined — a grant that cannot be exercised, and one that reports the wrong reason while it lasts.

MEASURED LIVE 2026-09-08, asking the deployed door from inside the Ray head:

    service-trainer             -> 201
    service-bronze-to-silver    -> 403  "service identity not allowed: service-bronze-to-silver"

while `LINEAGE_PRIVILEGED_SUBJECTS` carried `service-bronze-to-silver`, `service-media-to-silver` and
`service-silver-to-gold` and `LINEAGE_SERVICE_SUBJECTS` carried none of them.

ADMITTING THEM GRANTS NOTHING, which is why the fix is safe to make without a credential decision: a
privileged subject must still present its OWN dedicated token, and these have none provisioned, so the
refusal moves from `403 not allowed` to `401 the presented credential may not claim ...`. Neither
answer lets them in. What changes is that the configuration stops contradicting itself, and this gate
holds it there.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
CHART = REPO / "chart"


def _render(*extra: str) -> str:
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        "rask",
        str(CHART),
        "--set",
        "image.localImages=true",
        "--set-string",
        "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
        *extra,
    ]
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout  # noqa: S603


def _subjects(rendered: str, key: str) -> set[str]:
    return {s for value in re.findall(rf'name: {key}, value: "([^"]*)"', rendered) for s in value.split(",") if s}


def test_every_privileged_subject_is_also_an_allowed_one() -> None:
    """The containment the door's own order requires. Rendered WITH dedicated credentials, since that
    is the flag under which the privileged list exists at all."""
    rendered = _render("--set", "auth.dedicatedServiceCredentials=true")
    allowed = _subjects(rendered, "LINEAGE_SERVICE_SUBJECTS")
    privileged = _subjects(rendered, "LINEAGE_PRIVILEGED_SUBJECTS")

    assert privileged, "the privileged list rendered empty — the flag or the key moved, and this gate went blind"
    assert not privileged - allowed, (
        f"named privileged but never admitted, so the door refuses them before reading their credential: {sorted(privileged - allowed)}"
    )


def test_the_stage_runners_are_admitted() -> None:
    """Named explicitly, because they are the three the live door refused and a subset check alone
    would also pass if BOTH lists lost them."""
    allowed = _subjects(_render("--set", "auth.dedicatedServiceCredentials=true"), "LINEAGE_SERVICE_SUBJECTS")

    assert {"service-bronze-to-silver", "service-media-to-silver", "service-silver-to-gold"} <= allowed
