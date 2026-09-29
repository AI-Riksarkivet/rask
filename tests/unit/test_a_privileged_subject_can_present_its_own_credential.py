"""No subject is named privileged unless its service can present its own credential.

F2-3. The door and the caller are two halves of one control, and the failure mode is asymmetric:
naming a subject privileged makes the catalog DEMAND `service-token-<identity>` from it, and a
service still sending the shared `APP_API_TOKEN` is refused outright. Measured on the live estate
2026-08-26 — rendering the server-side expectation alone 401'd every catalog call and stopped the
cascade (`POST /v1/table/acme-silver$features/describe -> 401`, repeating) until it was reverted.

THERE IS NO SAFE ORDERING FOR A SUBJECT BEING ADDED, which is why this gate asserts the pair rather
than either half. The client half alone is inert: nothing yet demands it, so it changes nothing. The
server half alone is an outage. They land in one change or not at all. (The reverse — a subject that
is ALREADY privileged, like the trainer — can take its client half first, because the door already
expects the dedicated token. That asymmetry is real and cost a live 401 window when it was applied
in the wrong direction.)

THERE ARE THREE HALVES, NOT TWO, and the third is the one that nearly shipped a 401. A privileged
subject also needs its `service-token-<identity>` SEEDED: without it the resolver reads the store,
finds nothing for that identity, correctly answers `None`, and the caller falls back to the shared
bearer — which the door then refuses because the name is privileged. Client half present, server half
present, and every call still 401s. `openbao.yaml` derives the seeded set from its OWN list, which is
not the list the catalog renders, so the two can disagree silently. This file asserts all three.

THE CLIENT HALF IS DISCOVERED, NOT LISTED. A hand-maintained list of "services that are ready" is the
same defect one layer up: the next service is the one nobody adds, and the symptom is a 401 storm
rather than a red test. This greps the service sources for a `dedicated_token_for` definition and
maps each privileged subject to the values key that names it.
"""

from __future__ import annotations

import pathlib
import sys


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


REPO = pathlib.Path(__file__).resolve().parents[2]
ON = "auth.dedicatedServiceCredentials=true"


def _services_with_a_client_half() -> set[str]:
    """Services whose source defines `dedicated_token_for` — read off the code, not a list."""
    found: set[str] = set()
    for path in (REPO / "services").rglob("*.py"):
        if "/tests/" in str(path):
            continue
        if "def dedicated_token_for" in path.read_text(encoding="utf-8"):
            found.add(path.relative_to(REPO / "services").parts[0])
    return found


def _privileged(env_name: str) -> set[str]:
    for doc in _rendered_docs(ON):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env["name"] == env_name:
                    return {s for s in str(env.get("value") or "").split(",") if s}
    return set()


def test_ingest_is_privileged_at_BOTH_doors_now_that_it_can_present_one() -> None:
    """Ingest claims ONE subject at TWO doors, and `service_principal` refuses the shared token from a
    privileged name AND a dedicated one from an ordinary name. So a subject privileged at one door and
    ordinary at the other cannot satisfy both: whichever token it sends, one door refuses it. The two
    lists must therefore agree about it, which is why this asserts both rather than either."""
    assert "ingest" in _services_with_a_client_half(), "ingest lost its client half"
    assert "service-ingest" in _privileged("LANCE_PRIVILEGED_SUBJECTS"), "ingest is ordinary at the catalog"
    assert "service-ingest" in _privileged("LINEAGE_PRIVILEGED_SUBJECTS"), (
        "ingest is privileged at the catalog and ordinary at the graph — whichever token it sends, one door refuses it"
    )
