"""Two producer-side contracts the plane cannot repair downstream, and neither had a test.

`.claude/skills/rask-notifications` states the shape: "a state change that names nobody is not
under-delivered, it is UNDELIVERABLE", and `notifiable()` answers an event it cannot target with a
SUCCESS ack — so a producer that drops a field fails silently and is reported by nothing.

1. TRAP 3 — `lance.project`. `fanout.py:88` skips the watcher loop entirely when `project` is None,
   and the run is still delivered to its AUTHOR, so the event looks completely healthy and simply
   reaches fewer people. Measured 2026-08-22: deleting the mover's stamp left 4,853 tests passing.
2. The `/produce` 503 tail. The route's own docstring makes it load-bearing — the bronze-write emit is
   the cascade head, so "a publish failure surfaces as 503 (not the 202 that would hide it)". Line
   coverage reported the whole branch missing: the `publish_failed` check, the problem+json body and
   the `Retry-After` header never executed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, NamedTuple, cast

import pytest
from dapr.aio.clients import DaprClient
from medallion.api import produce as produce_route
from medallion.core.config import MedallionSettings


SRC = Path(__file__).resolve().parents[1] / "src" / "medallion"

#: Stands in for a resolved dependency the code under test never touches.
_UNUSED = object()

#: Emit sites that legitimately carry no `project=`, by the reason they are exempt. An entry here is a
#: claim someone has to justify — which is the whole difference between this and a count.
_PROJECTLESS_EMITS = {
    "services/media_produce.py": (
        "the media head writes to a CONFIGURED platform target (`settings.media_bronze_namespace`), "
        "not a tenant's warehouse. `ingest_media` therefore takes no project, and rask-notifications "
        "records why the door has no `?project=`: 'the media head's target is configured and "
        "authorization scope must equal write scope'. Adding one to reach WATCH would break that "
        "invariant, so this lane reaches its author and no watchers BY CONSTRUCTION."
    ),
}


class EmitSite(NamedTuple):
    """One `build_run_event(` call and which targeting fields it stamps."""

    file: str
    line: int
    project: bool
    originator: bool


#: Emit sites that legitimately carry no `originator=`, by the reason they are exempt — same contract
#: as `_PROJECTLESS_EMITS`: an entry is a claim someone has to justify.
_ORIGINATORLESS_EMITS = {
    "services/promotion.py": (
        "`promotion_lineage` does not PUBLISH this event. It builds one only to project it into the "
        "`LineageDoc` written beside the dataset, so the event never reaches `notifiable()` and has no "
        "audience to target. A provenance document answers 'what produced this dataset', which is a "
        "different question from 'who should hear about it' — see rask-notifications' closing line: "
        "'a fact that names a principal and touches no data is never lineage', and its converse here."
    ),
}


def _emit_sites() -> list[EmitSite]:
    """Every `build_run_event(` call under medallion, with the targeting fields it carries."""
    out: list[EmitSite] = []
    for py in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "build_run_event":
                stamped = {k.arg for k in node.keywords}
                out.append(
                    EmitSite(
                        file=py.relative_to(SRC).as_posix(),
                        line=node.lineno,
                        project="project" in stamped,
                        originator="originator" in stamped,
                    )
                )
    return out


def test_every_lineage_emit_stamps_lance_project() -> None:
    """Trap 3, at the producer — the only place it can be fixed."""
    sites = _emit_sites()
    assert sites, "no build_run_event call sites found — the scan root moved and this gate is vacuous"

    unstamped = sorted({site.file for site in sites if not site.project})
    assert unstamped == sorted(_PROJECTLESS_EMITS), (
        f"these medallion emit sites do not stamp `lance.project`: {unstamped}. fanout.py skips the "
        "watcher loop entirely when it is None, so the run still reaches its author and silently "
        "reaches NO watcher. Stamp it, or justify the omission in _PROJECTLESS_EMITS."
    )


@pytest.mark.asyncio
async def test_a_failed_publish_answers_503_and_not_the_202_that_would_hide_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cascade head's failure must be visible to the caller, not swallowed into a 202."""

    async def _publish_failed(*_a: object, **_k: object) -> dict[str, str]:
        return {"status": "publish_failed", "token": "tok"}

    monkeypatch.setattr(produce_route, "run_produce", _publish_failed)
    # Neither dependency is touched on this path — `run_produce` is patched — so a cast is the
    # honest way to satisfy the resolved-dependency signature without loosening it.
    response: Any = await produce_route.produce(dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), originator=None)

    assert response.status_code == 503, "a dropped cascade head must not answer 202 — the run silently never happens"
    assert response.headers["Retry-After"] == "5"
    assert response.media_type == "application/problem+json"


@pytest.mark.asyncio
async def test_a_successful_produce_still_answers_the_202_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The twin, so the 503 assertion above cannot pass by rejecting everything."""

    async def _ok(*_a: object, **_k: object) -> dict[str, str]:
        return {"status": "produced", "token": "tok", "dataset": "bronze$events"}

    monkeypatch.setattr(produce_route, "run_produce", _ok)
    # Neither dependency is touched on this path — `run_produce` is patched — so a cast is the
    # honest way to satisfy the resolved-dependency signature without loosening it.
    response: Any = await produce_route.produce(dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), originator=None)

    assert response == {"status": "produced", "token": "tok", "dataset": "bronze$events"}


def test_every_published_lineage_emit_stamps_originator() -> None:
    """Trap 2, at the producer — and the mover is the case the trap was written for.

    `rask-notifications` states it plainly: a service token SUBSTITUTES the author, so
    `enforce_author` overwrites the facet with the service's own sub and "your human is gone".
    The medallion movers author with a chart ROLE LITERAL (`MEDALLION_AUTHOR` = `data_eng` /
    `analyst` / `ray`), so `author_subject()` addresses an inbox actor named `data_eng` — nobody. The
    literal is *correct* as the author; what makes a failed cascade reachable by the person who
    started it is `lance.originator` riding beside it, carried from `/produce`'s verified sub through
    `/bronze-arrival` and down every hop.

    So this is the only field that can reach that human, and until 2026-08-22 nothing held it:
    deleting one of the mover's stamps left **217 medallion tests passing**. That is the shape the
    skill warns about — the plane acks an event it cannot target with SUCCESS, so the loss is reported
    by nothing, at the producer or anywhere downstream.
    """
    sites = _emit_sites()
    assert sites, "no build_run_event call sites found — the scan root moved and this gate is vacuous"

    unstamped = sorted({site.file for site in sites if not site.originator})
    assert unstamped == sorted(_ORIGINATORLESS_EMITS), (
        f"these medallion emit sites do not stamp `lance.originator`: {unstamped}. The mover authors "
        "with a chart role literal, so without it a failed cascade run reaches an inbox actor named "
        "after a ROLE and never reaches the person who started it. Stamp it, or justify the omission "
        "in _ORIGINATORLESS_EMITS."
    )


def test_the_targeting_scan_sees_every_hop_of_the_cascade() -> None:
    """Non-vacuity, and specifically about REACH rather than count.

    Both gates above are exemption-list shaped, and an exemption list is only as honest as the scan
    that feeds it: a walk that stopped resolving files would report zero unstamped sites and read as a
    fully-targeted estate. So this pins that the scan still reaches the three modules the cascade
    actually flows through — the head, the movers and the workflow — rather than a bare total.
    """
    sites = _emit_sites()
    files = {site.file for site in sites}

    assert len(sites) >= 8, f"only {len(sites)} emit sites found — the cascade has more hops than that"
    for expected in ("services/produce.py", "services/transform.py", "workflow.py"):
        assert expected in files, f"the scan no longer reaches {expected} — it is a cascade hop with emits"
    assert sum(1 for s in sites if s.file == "services/transform.py") >= 4, (
        "the mover module should carry several emits (one per stage outcome); the scan is seeing too few"
    )
