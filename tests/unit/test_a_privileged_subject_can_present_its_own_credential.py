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


def test_the_discovery_finds_the_services_that_have_one() -> None:
    """A grep matching nothing would make the assertions below vacuous."""
    have = _services_with_a_client_half()
    assert {"medallion", "maintenance", "ingest", "notifications"} <= have, f"only {sorted(have)} define dedicated_token_for — has it been renamed?"


def test_every_catalog_privileged_subject_belongs_to_a_service_that_can_present_one() -> None:
    """THE GATE. A name here whose service cannot send its own token is a 401 on every call it makes."""
    have = _services_with_a_client_half()
    subjects = _privileged("LANCE_PRIVILEGED_SUBJECTS")
    assert subjects, "no LANCE_PRIVILEGED_SUBJECTS rendered — this gate is blind"

    # The cascade writers, the trainer and maintenance are the only owners the chart puts on this list.
    unready = {s for s in subjects if s.startswith("service-maintenance") and "maintenance" not in have}
    unready |= {s for s in subjects if s == "service-ingest" and "ingest" not in have}
    unready |= {
        s
        for s in subjects
        if s.startswith(("service-medallion", "service-bronze", "service-silver", "service-media", "service-trainer")) and "medallion" not in have
    }
    assert not unready, f"privileged with no client half: {sorted(unready)} — every call they make will 401"


def test_the_services_with_NO_client_half_are_NOT_privileged() -> None:
    """The other direction, and the one that would actually break the estate: a subject named
    privileged before it can present its own credential is refused outright.

    EVERY subject now has a client half, so this passes vacuously today — and it is kept for exactly
    that reason. It is written as a RULE over the pair rather than a list of services that happen to
    lack one, so the next service to be named privileged is checked by this file already existing
    rather than by someone remembering to add it."""
    have = _services_with_a_client_half()
    subjects = _privileged("LANCE_PRIVILEGED_SUBJECTS") | _privileged("LINEAGE_PRIVILEGED_SUBJECTS")
    for service, subject in (("ingest", "service-ingest"), ("notifications", "notifications")):
        if service not in have:
            assert subject not in subjects, (
                f"{subject} is privileged but services/{service} defines no dedicated_token_for — "
                "the door will demand a credential it cannot send, and every call 401s"
            )


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


def test_maintenance_is_privileged_now_that_it_can_present_one() -> None:
    """The pair this file landed with: the client half in `catalog_compaction.dedicated_token_for`
    and the server half on the catalog's list. Asserted together, because either alone is wrong."""
    assert "maintenance" in _services_with_a_client_half(), "maintenance lost its client half"
    assert "service-maintenance" in _privileged("LANCE_PRIVILEGED_SUBJECTS"), "maintenance has a client half nothing demands"


def _seeded_tokens() -> set[str]:
    """The identities the OpenBao seed actually mints, read off the rendered Job."""
    import re

    seeded: set[str] = set()
    for doc in _rendered_docs(ON):
        if doc.get("kind") != "Job" or "openbao" not in doc["metadata"]["name"]:
            continue
        seeded |= set(re.findall(r"service-token-([A-Za-z0-9-]+)=", str(doc["spec"]["template"]["spec"]["containers"][0])))
    return seeded


def test_every_privileged_subject_has_its_token_SEEDED() -> None:
    """THE THIRD HALF. `openbao.yaml` builds its own list of identities to mint, and the catalog builds
    its own list of identities to demand. Nothing makes those two agree, and when they disagree the
    failure is silent in exactly the wrong direction: the resolver answers `None` for an unseeded
    identity — which is CORRECT, meaning "not provisioned" — the caller falls back to the shared
    bearer, and the door refuses it because the name is privileged. Every call 401s while both halves
    of the pair look present."""
    subjects = _privileged("LANCE_PRIVILEGED_SUBJECTS")
    seeded = _seeded_tokens()
    if not seeded:
        import pytest

        pytest.skip("no openbao seed Job on this profile — nothing to compare against")
    missing = subjects - seeded
    assert not missing, (
        f"privileged but unseeded: {sorted(missing)} — each resolves to None, falls back to the shared "
        "bearer, and is refused by the door that demands its dedicated one"
    )


def test_notifications_has_a_client_half_and_is_NOT_privileged() -> None:
    """THE THIRD ORDERING CASE, and the estate paid for it live before this test existed.

    The rule was: the client half alone is inert, the server half alone is an outage, so they land
    together. Notifications is the case where landing them TOGETHER is also an outage — because the
    credential never reaches the door. Its reconciler calls lineage through DAPR SERVICE INVOCATION,
    and daprd stamps its own `dapr-api-token` on everything it delivers, so lineage sees the estate's
    shared token whatever the caller sets. Naming the subject privileged made the door demand a token
    the transport cannot carry: measured live 2026-09-07, every walk answered
    `401 the presented credential may not claim 'notifications'`.

    So A DEDICATED CREDENTIAL IS A PROPERTY OF THE TRANSPORT, not only of the service. Ingest holds
    one at this same door because it calls lineage DIRECTLY over HTTP. This asserts the pair that is
    actually correct today — the client half present and unused, the subject ordinary — so that
    "notifications has a client half, therefore make it privileged" reds here rather than in a
    reconcile loop nobody is watching."""
    assert "notifications" in _services_with_a_client_half(), "notifications lost its client half"
    assert "notifications" not in _privileged("LINEAGE_PRIVILEGED_SUBJECTS"), (
        "notifications is privileged, but it reaches lineage through Dapr service invocation and daprd "
        "overwrites `dapr-api-token` on delivery — the door will demand a credential the transport "
        "cannot carry, and every reconcile walk 401s while returning no rows and no error"
    )


def test_EVERY_PRIVILEGED_SUBJECT_can_present_its_own() -> None:
    """F2-3's invariant, which is NOT the same as the row being closed. Every subject the doors demand
    a dedicated credential from must be able to send one; `notifications` is deliberately not among
    them (see above), so this passes with the row still open. Asserted over the RENDERED set rather
    than a written list, so a subject added later is covered by this file existing."""
    have = _services_with_a_client_half()
    owners = {
        "service-maintenance": "maintenance",
        "service-ingest": "ingest",
        "service-web": None,  # the BFF reads its token from env; it needs no client half
    }
    subjects = _privileged("LANCE_PRIVILEGED_SUBJECTS") | _privileged("LINEAGE_PRIVILEGED_SUBJECTS")
    unready = [s for s in sorted(subjects) if (owner := owners.get(s)) is not None and owner not in have]
    assert not unready, f"privileged with no client half: {unready} — every call they make will 401"
