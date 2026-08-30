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


def _hand_built_events() -> list[tuple[str, int, set[str]]]:
    """Every lineage event built as a DICT LITERAL rather than through `build_run_event`.

    `_emit_sites()` walks `build_run_event(` calls only, so a hand-built event is invisible to both
    targeting gates above — and the non-vacuity guard still passes, because the same MODULE has other
    emits that do use the helper. That is how the one site failing the contract stayed unseen.
    """
    out: list[tuple[str, int, set[str]]] = []
    for py in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "eventType" not in keys:
                continue
            inner = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
            out.append((py.relative_to(SRC).as_posix(), node.lineno, inner))
    return out


def test_every_HAND_BUILT_lineage_event_names_an_author() -> None:
    """Trap 2, at the one site the `build_run_event` scan cannot reach.

    `notifiable()` returns `None` on an event with no verified author BEFORE `originator_subject()` is
    read, so an event without an `author` facet is undeliverable no matter what else it carries. The
    train watcher hand-built one with `facets = {lance, errorMessage}` and threaded `originator` and
    `project` all the way from `/train` into an event the consumer is designed to drop — then the plane
    SUCCESS-acked it, so nothing reported the loss.

    Hand-building is LEGITIMATE here and this gate does not forbid it: that site's runId must stay
    `run_id_for(f"train-{token}")`, byte-identical to the job's own, or the watcher's FAIL forks a second
    run instead of merging onto it. `build_run_event` derives its own id and cannot express that. So the
    contract is checked directly rather than by forcing every emit through one helper.
    """
    events = _hand_built_events()
    assert events, "no hand-built lineage events found — the scan root moved and this gate is vacuous"

    authorless = sorted(f"{f}:{line}" for f, line, inner in events if "author" not in inner)
    assert not authorless, (
        f"these hand-built lineage events carry no `author` facet: {authorless}. `notifiable()` returns "
        "None on them, so the plane acks and tells nobody — and because they go out on the BUS through the "
        "outbox, neither `enforce_author` nor the HTTP door's checks run to supply one."
    )


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
    response: Any = await produce_route.produce(
        dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), idempotency_key="idem-test", originator=None
    )

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
    response: Any = await produce_route.produce(
        dapr=cast(DaprClient, _UNUSED), settings=cast(MedallionSettings, _UNUSED), idempotency_key="idem-test", originator=None
    )

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


def _emit_fail_run_calls() -> list[tuple[int, set[str]]]:
    """Every `_emit_fail_run(` call in the mover, with the kwargs it stamps.

    The mover's four stage-outcome FAIL emits (project-unresolvable, media-underivable, stage-failed,
    promotion-held) route through one `_emit_fail_run` helper (MED-005), so `_emit_sites()` — which
    walks `build_run_event(` calls — no longer sees them as four sites; it sees the helper's single
    call. The per-outcome REACH guarantee therefore moves to the call sites, checked here.
    """
    transform = SRC / "services" / "transform.py"
    out: list[tuple[int, set[str]]] = []
    for node in ast.walk(ast.parse(transform.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"_emit_fail_run", "_emit_stage_failure"}:
            out.append((node.lineno, {k.arg for k in node.keywords if k.arg is not None}))
    return out


def test_the_targeting_scan_sees_every_hop_of_the_cascade() -> None:
    """Non-vacuity, and specifically about REACH rather than count.

    Both gates above are exemption-list shaped, and an exemption list is only as honest as the scan
    that feeds it: a walk that stopped resolving files would report zero unstamped sites and read as a
    fully-targeted estate. So this pins that the scan still reaches the three modules the cascade
    actually flows through — the head, the movers and the workflow — rather than a bare total.
    """
    sites = _emit_sites()
    files = {site.file for site in sites}

    assert len(sites) >= 6, f"only {len(sites)} emit sites found — the cascade has more hops than that"
    for expected in ("services/produce.py", "services/transform.py", "workflow.py"):
        assert expected in files, f"the scan no longer reaches {expected} — it is a cascade hop with emits"
    # The mover carries the COMPLETE emit plus the shared FAIL emit (`_emit_fail_run`'s own
    # `build_run_event`); its four stage-outcome FAILs are checked at their call sites below.
    assert sum(1 for s in sites if s.file == "services/transform.py") >= 2, (
        "the mover module should carry the COMPLETE emit and the shared FAIL emit; the scan is seeing too few"
    )


def test_every_stage_outcome_reaches_the_person_through_the_fail_helper() -> None:
    """The per-outcome REACH guarantee, at the seam MED-005 moved it to.

    Consolidating the four inline FAIL blocks into `_emit_fail_run` means `_emit_sites()` sees one
    `build_run_event` call, not four — so `test_every_published_lineage_emit_stamps_originator`
    (which walks that helper) proves the emit CAN name the person, but not that every stage outcome
    passes the person down to it. That is what these call sites carry: drop the person at any one of
    them and that stage's failed run reaches an inbox actor named after a chart role, exactly the
    silent loss the sibling gate was written for. The mover has four failure exits (project
    unresolvable, media underivable, stage failed, promotion held), so four call sites.

    MED-004 added ONE more level between them: the four sites now call `_emit_stage_failure`, which
    pairs `best_effort` with `_emit_fail_run` instead of repeating fourteen keyword lines each time.
    So the scan accepts either name, and the guarantee splits in two — every call site must hand down
    the tenant and the trigger the person is read off, and the one `_emit_fail_run` call must stamp
    both onto the event. Neither half is sufficient alone.
    """
    calls = _emit_fail_run_calls()
    assert len(calls) >= 5, f"the mover should route all four stage-outcome FAILs through the shared FAIL emit; found {len(calls)}"

    forwarding = [(line, stamped) for line, stamped in calls if "originator" not in stamped]
    assert len(forwarding) >= 4, f"expected the four stage-outcome call sites to forward through `_emit_stage_failure`; found {len(forwarding)}"
    for line, stamped in forwarding:
        assert "project" in stamped, f"the FAIL emit at transform.py:{line} drops `project` — the watcher fan-out is skipped for this outcome"

    stamping = [(line, stamped) for line, stamped in calls if "originator" in stamped]
    assert stamping, "nothing stamps `originator` onto the FAIL event any more"
    for line, stamped in stamping:
        assert "project" in stamped, f"the FAIL emit at transform.py:{line} drops `project` — the watcher fan-out is skipped for this outcome"

    # The person is read off the TRIGGER, which is what every call site forwards. A helper that
    # defaulted the originator would satisfy the two halves above and still reach nobody.
    source = (SRC / "services" / "transform.py").read_text()
    _, _, helper = source.partition("async def _emit_stage_failure(")
    assert "originator=trigger.originator" in helper.partition("\nasync def ")[0]
