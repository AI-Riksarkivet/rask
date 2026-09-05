"""Interactive AI-assist — a model prediction for the annotator (prompt/draw → shapes).

The NARROW interactive exception (bulk auto-labeling belongs in batch derivers). A
prompt — text for GroundingDINO, a box/point for SAM — runs a model and returns shapes
as `status="prediction"`, `source="model:<name>"` rows the annotator renders optimistically
and the reviewer accepts/rejects like any prediction. So interactive assist and batch
auto-label share the SAME provenance + review path.

Routes to a model server (``MEDIA_ASSIST_URL``) when set; else a deterministic MOCK so
the round-trip is wired + testable in-repo (drop-in for a real server, exactly like the
catalog transport). Shapes are in IMAGE coordinates — the annotator's own space.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from annotator.api.security import CheckerDep, CurrentSubject
from annotator.api.v1.endpoints.serve_discovery import discovered_backends
from annotator.projects.generation_schema import generation_schema
from annotator.projects.machines import UnreadableOntology, refuse_unreadable_ontology
from annotator.projects.ontology import LabelOntology
from service_kit.exceptions import ForbiddenError, ServiceUnavailableError
from service_kit.governed.audit import FAILURE, audit
from service_kit.lancekit.keys import validate_doc_key
from service_kit.media.authz import corpus_object
from service_kit.media.config import AssistBackend
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import AppState, dataset_handle


logger = logging.getLogger(__name__)

#: An assist prediction is a PROPOSED WRITE: it comes back as `status="prediction"`,
#: `source="model:<name>"` rows the annotator renders and a reviewer accepts or rejects — the same
#: provenance path a batch deriver's output takes. So the rung is the write one. Gating on the read
#: rung would let anyone who may merely LOOK at a corpus spend model compute against it and queue
#: work for its reviewers.
WRITE_DATA = "can_write_data"


async def require_assist(request: Request, state: StateDep, subject: CurrentSubject, checker: CheckerDep) -> None:
    """The door for the whole assist plane.

    AT THE ROUTER, not on each handler — the reference's own rule for a group where every route needs
    the same check ("cheaper to read and harder to forget"). This module had no door at all: it did not
    import `annotator.api.security`, so its three routes took no verified subject, no checker and no
    forwarded bearer while every sibling router in this service had one. The POST reads a corpus unit
    and drives a model backend; the two GETs disclose the estate's model-backend topology and a task's
    ontology. None of that should answer a caller nobody identified.

    The dataset is read off the QUERY STRING rather than a typed parameter because the two GETs do not
    declare one — they are scoped by `task_id`, and resolving `None` here gives the default dataset,
    which is the same corpus their answers describe. That keeps one door for the group instead of a
    per-route rung, which is what made this router easy to add a fourth ungated route to.

    Blocking Lance/S3 IO goes to the threadpool: `dataset_handle` opens under a `threading.Lock`, and
    awaiting it inline would freeze the loop for every other request during a cold S3 open. The handler
    resolves the same handle again from cache.
    """
    handle = await run_in_threadpool(dataset_handle, state, request.query_params.get("dataset"))
    binding = handle.descriptor.declared.document
    if binding is None:
        # FAIL CLOSED. With no document binding there is no table to authorize against, and answering
        # anyway would make a malformed descriptor an authorization bypass.
        audit("annotator.assist", FAILURE, subject=subject, resource=handle.id, relation=WRITE_DATA)
        raise ForbiddenError(f"{subject} cannot be authorized for assist on {handle.id}: no documents table")
    obj = corpus_object(state.settings, handle.id, binding.table)
    if not await checker(user=subject, relation=WRITE_DATA, obj=obj):
        audit("annotator.assist", FAILURE, subject=subject, resource=handle.id, relation=WRITE_DATA)
        raise ForbiddenError(f"{subject} lacks {WRITE_DATA} on {obj}")


router = APIRouter(prefix="/api", tags=["assist"], dependencies=[Depends(require_assist)])


class Region(BaseModel):
    """The drawn box the assist runs within (image coords). Omitted ⇒ whole image."""

    x: float
    y: float
    width: float
    height: float


class Point(BaseModel):
    """One interactive prompt point (image coords). ``positive=False`` marks background —
    the SAM click convention: foreground clicks say "include this", background clicks say
    "not this". Points ACCUMULATE across a refinement session; each request carries the
    full set so the backend is stateless."""

    x: float
    y: float
    positive: bool = True


class AssistRequest(BaseModel):
    """What to run: the producer + its prompt (free text, a drawn region, and/or clicked
    points). A SAM-family backend takes any combination — box, points, box+points; a
    text-prompt backend reads ``prompt``. Everything is optional so one wire shape serves
    every producer family."""

    producer: str = "grounding-dino"
    prompt: str | None = None
    region: Region | None = None
    #: The interactive point-prompt session: every point clicked so far, in order. The
    #: request is the whole session state — re-running with one more point REFINES the
    #: same object rather than predicting a new one.
    points: list[Point] = Field(default_factory=list)
    #: The labeling task this assist is for, when there is one. The SERVER reads that task's
    #: captured ontology — the client does not send the rules it is judged by, same posture as
    #: `review_required` and the submit-time contract check.
    task_id: str | None = None


#: Producer tool-name -> the canonical vocabulary the draft, ontology and published table speak.
#: A model server names shapes however it likes; `"rectangle"` was this endpoint's own default and
#: is accepted by NEITHER the service (`bbox`) nor the canvas. Normalizing here means a new backend
#: is a config entry rather than a new dialect leaking into the annotations table.
_CANONICAL_SHAPE: dict[str, str] = {
    "rectangle": "bbox",
    "rect": "bbox",
    "box": "bbox",
    "bbox": "bbox",
    "polygon": "polygon",
    "mask": "mask",
    "point": "keypoint",
    "keypoint": "keypoint",
    "line": "polyline",
    "polyline": "polyline",
    "baseline": "polyline",
}


#: What each known producer FAMILY emits, longest-prefix like the backend registry itself.
#: Absent ⇒ unknown, and every surface must say "unknown" rather than guess — a wrong claim here
#: would present a producer as task-compatible when it is not, which is the failure this whole
#: endpoint exists to prevent.
_RETURNS: dict[str, tuple[str, ...]] = {
    "grounding-dino": ("bbox",),
    "sam": ("polygon",),
    # The BATCH families. Absent from this map, the service's registry and the frontend's diverged
    # into two truths: `/assist/producers` never listed htr/insid3/vlm-judge/embed-propagate, so
    # their compatibility rendered "unknown" forever and the settings surface denied producers the
    # jobs seam happily accepts. Listed ⇒ compatibility computes; batch-ness rides `interactive`.
    "htr": ("text",),
    "insid3": ("mask",),
    # The RECIPE family (open-bulk): an LLM/VLM answering an item-level question. Its answer is a
    # `tag` shape carrying `text` — the bulk grid's cell — so it is interactive (per-cell fills go
    # through this POST) and its compatibility computes against tag-tooled classes.
    "vlm": ("tag",),
    # vlm-judge/embed-propagate emit VERDICTS/FIELD writes, not shapes — an empty tuple is honest
    # ("emits no drawable shape"), and compatibility correctly stays a non-claim.
    "vlm-judge": (),
    "embed-propagate": (),
}

#: Families that only run through the JOBS seam (`/api/jobs/apply`) — the interactive assist POST
#: has no transport for them. The registry says so instead of letting the bar offer a mode that
#: could only ever answer from the mock.
_BATCH_ONLY: frozenset[str] = frozenset({"htr", "vlm-judge", "embed-propagate"})


def returns_for(producer: str) -> tuple[str, ...]:
    """The shape types `producer` is known to emit; empty tuple when nothing is known."""
    best = max((p for p in _RETURNS if producer.startswith(p)), key=len, default=None)
    return _RETURNS[best] if best is not None else ()


class ProducerInfo(BaseModel):
    """One row of the assist registry, as the SERVICE sees it.

    The zone used to derive this list by parsing `MEDIA_ASSIST_BACKENDS` out of the WEB pod's own
    env — a second copy of the registry that the config comment itself admitted could "drift from
    the service's". The service is the process that actually resolves and calls a backend, so it is
    the only source that cannot be wrong.
    """

    name: str
    #: False ⇒ this name answers from the in-repo mock (no endpoint resolves for it).
    configured: bool
    #: The shape types it emits; EMPTY means unknown, not "none". A registered backend's own
    #: declaration wins over the built-in family map — the entry, not code, is the contract.
    returns: list[str] = Field(default_factory=list)
    #: What the backend DECLARES it takes (e.g. ["prompt"], ["points", "region"]). Empty means
    #: undeclared — the panel falls back to its family knowledge and says so no stronger.
    inputs: list[str] = Field(default_factory=list)
    #: Whether those shapes satisfy the task named in `?task_id=`. None when there is no task, when
    #: its ontology constrains nothing, or when `returns` is unknown — three genuinely different
    #: reasons not to make a claim, all of which the UI renders as "unknown" rather than as a pass.
    compatible: bool | None = None
    #: False ⇒ this family runs only through the jobs seam; the interactive assist POST cannot
    #: reach it, and the bar must not offer it as a mode.
    interactive: bool = True


class ProducerListing(BaseModel):
    """The registry plus the fallback, so a surface can explain WHY a producer is mocked."""

    producers: list[ProducerInfo]
    #: True when `MEDIA_ASSIST_URL` is set: unregistered producer names reach a real backend.
    default_configured: bool
    #: Endpoints are never returned. Any authenticated annotator can call this, and an internal
    #: model-server URL is not theirs to have; presence is the whole of what a surface needs.
    urls_redacted: bool = True


class AssistShape(BaseModel):
    """One predicted shape in image coordinates (box or polygon) with its scores.

    ``confidence``/``uncertainty`` are the active-learning columns the review queue ranks by
    (predictions first, highest uncertainty first). They are part of the WIRE contract so a real
    backend must state them — the columns exist end-to-end (schema, sidebar, queue order) and a
    producer that omits them leaves its predictions unrankable, which reads as "the queue is
    alphabetical" with nowhere to see why. ``uncertainty`` is the model's OWN estimate, never
    derived here as ``1 - confidence`` — that derivation carries no information beyond confidence
    and is exactly the trap this field exists to avoid."""

    shape_type: str = "bbox"
    x: float
    y: float
    width: float
    height: float
    polygon: list[float] = Field(default_factory=list)
    label: str = ""
    #: The TEXTUAL answer — a transcription, or an LLM/VLM recipe's cell value (the bulk grid's
    #: item-level columns land as `tag` shapes whose `text` is the cell). Part of the wire so a
    #: text-family producer can answer at all: without it the assist plane could only ever carry
    #: geometry, and every text answer would need a second transport.
    text: str = ""
    confidence: float = 0.0
    #: None = the backend made no estimate; the row lands with a null and sorts last in the queue.
    uncertainty: float | None = None


class AssistResult(BaseModel):
    """Predicted shapes + the provenance stamp (source) the annotator writes."""

    shapes: list[AssistShape]
    source: str
    #: Predictions the TASK's own contract refuses, and why — reported rather than silently dropped.
    #:
    #: A producer that returns a polygon for a bbox-only task is a real mismatch, and the annotator
    #: is the wrong place to discover it: before this, such a shape landed in the review queue and
    #: was refused only at SUBMIT, after a human had already looked at it. Dropping it silently
    #: would be worse — the operator would never learn the backend disagrees with the task.
    dropped: list[str] = Field(default_factory=list)


@router.get("/assist/producers")
async def producers(state: StateDep, task_id: str | None = None) -> ProducerListing:
    """Which producers exist, whether each is real or mocked, and what it returns.

    The owner's question, verbatim: "why is there no settings menu for which endpoints or runner is
    configured.. and what they return". There was none, and worse, the one list that DID exist was
    the zone re-parsing the registry out of its own pod's env — so a service configured with a real
    backend could still be presented as mocked, and vice versa.

    With `?task_id=`, each row also carries whether its output can satisfy that task. That is the
    other half of "schemas must align from the ml backend": a producer emitting polygons for a
    bbox-only task is answerable BEFORE anyone runs it, and this is where you can see it.
    """
    return producer_listing(state.settings, await enforced_shape_types(task_id), await _discovered(state))


async def enforced_shape_types(task_id: str | None) -> set[str] | None:
    """The shape types a task will ACCEPT, canonicalised. `None` = no constraint to speak of.

    TWO ways to get `None`, and they are deliberately indistinguishable to callers: no task, or an
    ontology that constrains nothing. Both mean the same thing — there is no rule here that anyone
    may be judged against, so make no claim.

    There used to be a third, "a task that could not be read", and it was the fail-open branch
    ANN-05 removed: an ontology the current model cannot parse now RAISES out of this function (409)
    rather than being flattened into "no constraint". An unreadable rule is not the absence of a
    rule, and returning `None` for it silently un-enforced the taxonomy the publish path depends on.
    A transport failure reaching the task store is a different thing and still propagates as itself.
    """
    if not task_id:
        return None
    ontology = await _task_ontology(task_id)
    if ontology is None:
        return None
    # DERIVED from the taxonomy, never declared beside it: `LabelOntology.tools` is the union of
    # every class's tools, and it is empty when any class is unconstrained. That replaced a flat
    # `tools` list that could contradict the very classes it sat next to.
    return {_CANONICAL_SHAPE.get(t, t) for t in ontology.tools} or None


def producer_listing(
    settings: Any,
    allowed: set[str] | None = None,
    discovered: dict[str, AssistBackend] | None = None,
) -> ProducerListing:
    """Build the registry report. Takes `settings` structurally, like `backend_for` — the routing
    rules are the interesting part and they should be testable without standing up an `AppState`."""
    registry = merged_registry(settings, discovered)
    default_url = getattr(settings, "assist_url", None)

    rows: list[ProducerInfo] = []
    # The registry, plus the two families the in-repo mock answers for — otherwise an unconfigured
    # service reports an EMPTY settings surface, which reads as "assist is unavailable" when in fact
    # both interactive loops work against the mock.
    for name in sorted(set(registry) | set(_RETURNS)):
        declared = registry.get(name)
        # The backend's OWN declaration wins; the built-in family map is the fallback for the
        # families the mock answers for. Canonicalised like a response would be, so a backend
        # declaring "rectangle" and a task allowing "bbox" still meet.
        emits = tuple(_CANONICAL_SHAPE.get(r, r) for r in declared.returns) if declared and declared.returns else returns_for(name)
        rows.append(
            ProducerInfo(
                name=name,
                configured=backend_for(settings, name, discovered) is not None,
                returns=list(emits),
                inputs=list(declared.inputs) if declared else [],
                # No task, no enforcement, or nothing known about what it emits ⇒ NO CLAIM. Only the
                # case where both sides are actually known produces a true/false.
                compatible=bool(set(emits) & allowed) if (allowed and emits) else None,
                interactive=name not in _BATCH_ONLY,
            )
        )
    return ProducerListing(producers=rows, default_configured=bool(default_url))


class GenerationContract(BaseModel):
    """The task's ontology as a JSON Schema for structured decoding — what a vLLM-style
    backend passes to ``guided_json`` (Outlines/xgrammar) so an off-contract annotation
    cannot be generated. ``null`` when the task constrains nothing: a schema fabricated from
    no contract would constrain to nothing while reading as if it enforced something."""

    output_schema: dict[str, Any] | None


@router.get("/assist/generation-schema")
async def generation_contract(task_id: str) -> GenerationContract:
    """The decode-time contract for `task_id` — also useful standalone: a batch deriver or an
    external labeling script can fetch it and constrain its own generation the same way."""
    return GenerationContract(output_schema=generation_schema(await _task_ontology(task_id)))


@router.post("/assist/{doc_id}/{speech_id}/{chunk_id}")
async def assist(
    state: StateDep,
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    body: AssistRequest,
    dataset: DatasetParam = None,
) -> AssistResult:
    """Run an interactive producer over one media unit and return predicted shapes."""
    # Off the loop: dataset resolution is blocking Lance/S3 under a threading.Lock — inline it
    # froze the whole loop for the duration of a cold S3 open (docs/DECISIONS.md "The Python estate audit" ANN-01); the
    # sibling `def` routes get the threadpool for free.
    handle = await run_in_threadpool(dataset_handle, state, dataset)
    doc_id = validate_doc_key(handle.descriptor.declared, doc_id)
    source = f"model:{body.producer}"
    url = backend_for(state.settings, body.producer, await _discovered(state))
    # ONE task read serves both halves of the contract: the schema a constrained decoder
    # enforces at GENERATION time, and the filter that checks whatever came back.
    ontology = await _task_ontology(body.task_id) if body.task_id else None
    # The producer call uses a SYNC httpx client, so it rides the threadpool rather than blocking
    # the event loop — the endpoint is async only because reading the task's template is.
    shapes = await run_in_threadpool(_remote, state, url, (doc_id, speech_id, chunk_id), body, generation_schema(ontology)) if url else _mock(body)
    for shape in shapes:
        shape.shape_type = _CANONICAL_SHAPE.get(shape.shape_type, shape.shape_type)
    shapes, dropped = _within_contract(shapes, ontology)
    # Prompt CONTENT is user free-text — never logged (PII/leak surface); length only.
    logger.info(
        "assist %s (prompt %d chars) → %d shape(s), %d dropped",
        body.producer,
        len(body.prompt or ""),
        len(shapes),
        len(dropped),
    )
    return AssistResult(shapes=shapes, source=source, dropped=dropped)


def _within_contract(shapes: list[AssistShape], ontology: LabelOntology | None) -> tuple[list[AssistShape], list[str]]:
    """Keep the predictions the task's template permits; report the rest.

    A producer is not obliged to know the task's rules — the whole point of the registry is that a
    backend is a config entry. So the mismatch is resolved HERE, once, rather than by every model
    server or (as before) by a human at submit time. PURE: the route reads the ontology once and
    both contract halves (the generation schema and this filter) derive from that one read.

    A task-less assist (the ad-hoc canvas) has no contract, so nothing is dropped. A read whose
    TRANSPORT failed arrives as ``None`` and is treated the same way: the assist still returns its
    shapes, because refusing a prediction because we could not FETCH a rule would be a worse failure
    than the mismatch it guards against. A stored ontology that will not parse never reaches here —
    `_task_ontology` refuses it by name, so this filter is never asked to stand in for a contract
    that exists and cannot be read.
    """
    if ontology is None:
        return shapes, []
    allowed = {_CANONICAL_SHAPE.get(t, t) for t in ontology.tools}
    if not allowed:
        return shapes, []
    kept, dropped = [], []
    for shape in shapes:
        if shape.shape_type in allowed:
            kept.append(shape)
        else:
            dropped.append(f"{shape.shape_type} refused — task {ontology.kind or 'ontology'} allows {sorted(allowed)}")
    return kept, dropped


async def _task_ontology(task_id: str) -> LabelOntology | None:
    """The ontology CAPTURED onto a task, read server-side. `None` when there is no rule to apply.

    Read here rather than accepted from the caller on purpose: the client would otherwise be
    supplying the rules it is judged by. `None` means "no claim" everywhere it is used — nothing
    filters, nothing is reported compatible. An ontology that constrains NOTHING reads as `None`,
    which is the same statement: there is no rule to judge anyone against.

    **A TRANSPORT failure and an UNREADABLE ontology are not the same answer**, though they arrive
    at this call site identically. A sidecar that did not answer says nothing about the rules, so it
    degrades to unconstrained rather than to a wrong answer — losing a real prediction because a
    rule could not be fetched is the worse failure. A stored ontology that will not PARSE says the
    opposite: the rules exist and this build cannot read them, so returning `None` would hand the
    annotator unfiltered suggestions that look exactly like filtered ones. That one is refused, by
    name, and is the only exception this function lets past.
    """
    try:
        from annotator.api.v1.endpoints.tasks import _proxy  # noqa: PLC0415 - import cycle

        task = await _proxy(task_id).get()
    except UnreadableOntology:
        # The task actor's `_load` already refused this document by name — and it crosses the
        # sidecar as itself (`projects.proxies._translating`). Swallowing it here as a transport
        # failure would put the ONE case that must fail closed back on the fail-open path.
        raise
    except Exception:  # noqa: BLE001 - a rule we could not FETCH must not lose a prediction; see above
        logger.warning("assist could not read task %s; proceeding without its contract", task_id)
        return None
    try:
        ontology = LabelOntology.model_validate((task or {}).get("ontology") or {})
    except ValidationError as exc:
        # Reachable only across a ROLLING UPGRADE that changes `LabelOntology`: within one deployed
        # version the actor's `_load` refuses first, so this parse cannot fail. During an upgrade the
        # read can be served by an actor pod still on the old code, whose `model_dump` this newer
        # model rejects — and the named refusal is what makes that window diagnosable.
        refuse_unreadable_ontology(exc, "task", task_id, path="")
    return ontology if ontology.constrains else None


async def _discovered(state: AppState) -> dict[str, AssistBackend]:
    """What Ray Serve is serving right now, TTL-cached — fetched off the event loop (sync
    httpx), and empty when discovery is unconfigured or the control plane is unreachable."""
    settings = state.settings
    if not getattr(settings, "serve_discovery_url", None):
        return {}
    return await run_in_threadpool(
        discovered_backends,
        state.http,
        settings.serve_discovery_url,
        getattr(settings, "serve_proxy_url", None),
    )


def merged_registry(settings: Any, discovered: dict[str, AssistBackend] | None) -> dict[str, AssistBackend]:
    """Discovery under config: what Ray Serve is OBSERVED to serve, overlaid by what the
    operator DECLARED — an env entry with the same producer name wins, because config is
    intent and discovery is observation."""
    return {**(discovered or {}), **registry_of(settings)}


def registry_of(settings: Any) -> dict[str, AssistBackend]:
    """The registry, NORMALIZED: config may carry structured `AssistBackend` entries or bare URL
    strings (back-compat), and structural test doubles pass plain dicts — every consumer reads
    through this one coercion instead of re-deciding what an entry is."""
    raw = getattr(settings, "assist_backends", None) or {}
    return {name: AssistBackend.model_validate(entry) for name, entry in raw.items()}


def backend_for(settings: Any, producer: str, discovered: dict[str, AssistBackend] | None = None) -> str | None:
    """Resolve the producer's backend: LONGEST matching prefix in the merged registry wins (so
    `"sam"` covers `sam-click` while `"sam-hq"` can still override it), else the default
    `assist_url`, else None (→ the honest in-repo mock). With Serve discovery on, DEPLOYING a
    model is what registers it — the env registry stays the operator override."""
    backends = merged_registry(settings, discovered)
    best = max((p for p in backends if producer.startswith(p)), key=len, default=None)
    if best is not None:
        return backends[best].url
    return settings.assist_url


_SAM_CLICK_PATCH = 120.0  # default box side for a bare click (a zero-size region)


def _mock(body: AssistRequest) -> list[AssistShape]:
    """Deterministic stand-in for a model server, so both interactive loops round-trip
    in-repo. GroundingDINO → a box at the drawn region (or a default), labeled with the
    prompt. SAM → a polygon 'mask' around the region/click (a click is a zero box, so a
    default patch is grown around the point). Both render + review like real predictions.

    Scores are stated (not derived from each other) for the same reason the mock exists at all:
    the wire contract must round-trip in-repo, and the review queue's uncertainty ordering is only
    exercisable when the two producers rank DIFFERENTLY — the segmenter reports lower uncertainty
    than the detector, so a mixed queue has a visible, deterministic order."""
    r = body.region
    if body.producer.startswith("sam"):
        if body.points:
            # A refinement session: the mask follows the POSITIVE points (their bounding
            # box, padded), and each added point visibly improves the scores — so the
            # client's replace-on-refine loop is exercisable end-to-end: same session,
            # different geometry, monotonically better confidence.
            x, y, w, h = _points_box(body.points)
            n = len(body.points)
            confidence = min(0.95, 0.7 + 0.05 * n)
            uncertainty = max(0.05, 0.3 - 0.04 * n)
        else:
            x, y, w, h = _region_box(r)
            confidence, uncertainty = 0.85, 0.3
        return [
            AssistShape(
                shape_type="polygon",
                x=x,
                y=y,
                width=w,
                height=h,
                polygon=_diamond(x, y, w, h),
                label=(body.prompt or "object").strip(),
                confidence=confidence,
                uncertainty=uncertainty,
            )
        ]
    if body.producer.startswith("vlm"):
        # The recipe family: an item-level ANSWER, not geometry — a `tag` shape whose `text` is
        # the bulk grid's cell value. Deterministic echo of the question so the loop (fill →
        # correct → validate) is exercisable in-repo, honest about being a mock.
        answer = f"[{body.producer}] {(body.prompt or '').strip()}"[:80]
        return [AssistShape(shape_type="tag", x=0.0, y=0.0, width=0.0, height=0.0, text=answer, confidence=0.8, uncertainty=0.2)]
    label = (body.prompt or "region").strip()
    if r is not None:
        return [
            AssistShape(
                shape_type="rectangle",
                x=r.x,
                y=r.y,
                width=r.width,
                height=r.height,
                label=label,
                confidence=0.7,
                uncertainty=0.45,
            )
        ]
    return [AssistShape(shape_type="rectangle", x=100.0, y=100.0, width=200.0, height=80.0, label=label, confidence=0.7, uncertainty=0.45)]


def _points_box(points: list[Point]) -> tuple[float, float, float, float]:
    """The patch a point session selects: the positive points' bounding box, padded by
    half the click patch on every side. Background (negative) points steer a real model
    but select nothing themselves — they only anchor the box when NO positive point
    exists (a session of pure background clicks still needs an answer somewhere)."""
    anchors = [p for p in points if p.positive] or points
    xs = [p.x for p in anchors]
    ys = [p.y for p in anchors]
    pad = _SAM_CLICK_PATCH / 2
    return (min(xs) - pad, min(ys) - pad, max(xs) - min(xs) + 2 * pad, max(ys) - min(ys) + 2 * pad)


def _region_box(r: Region | None) -> tuple[float, float, float, float]:
    """The region as (x, y, w, h); a click (near-zero size) becomes a patch centered on
    the point."""
    if r is None:
        return (100.0, 100.0, 200.0, 200.0)
    w = r.width if r.width > 1 else _SAM_CLICK_PATCH
    h = r.height if r.height > 1 else _SAM_CLICK_PATCH
    x = r.x if r.width > 1 else r.x - w / 2
    y = r.y if r.height > 1 else r.y - h / 2
    return (x, y, w, h)


def _diamond(x: float, y: float, w: float, h: float) -> list[float]:
    """A simple inset polygon (rhombus) standing in for a segmentation mask — flat
    [x0,y0,x1,y1,...] in image coords, as the engine's ArrowDataPlugin expects."""
    cx, cy = x + w / 2, y + h / 2
    return [cx, y, x + w, cy, cx, y + h, x, cy]


def _remote(
    state: AppState,
    url: str,
    key: tuple[str, int, int],
    body: AssistRequest,
    output_schema: dict[str, Any] | None = None,
) -> list[AssistShape]:
    """Proxy to the model endpoint — a Ray Serve deployment (GroundingDINO/SAM) per the
    merge runtime stack. WIRED, not exercised in-repo: posts the chunk-frame image URL +
    prompt + region and expects ``{shapes: [...]}``. A failing or misbehaving model
    server (HTTP error, bad JSON, invalid shape) raises
    :class:`ServiceUnavailableError` — a stable 503, never a raw 500.

    ``output_schema`` is the task's ontology as a JSON Schema — a vLLM-style backend hands it
    to its structured decoder (``guided_json``) so an off-contract annotation cannot be
    GENERATED, rather than merely being filtered after. Null when the task constrains nothing;
    a non-LLM backend is free to ignore it."""
    doc_id, speech_id, chunk_id = key
    http = state.http
    if http is None:
        raise ServiceUnavailableError("assist: HTTP client unavailable")
    payload = {
        "image_url": f"/api/chunk-frame/{doc_id}/{speech_id}/{chunk_id}",
        "prompt": body.prompt,
        "region": body.region.model_dump() if body.region else None,
        "points": [p.model_dump() for p in body.points],
        "output_schema": output_schema,
    }
    try:
        resp = http.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        return [AssistShape.model_validate(s) for s in resp.json().get("shapes", [])]
    except (httpx.HTTPError, ValueError) as e:
        raise ServiceUnavailableError("assist model server error") from e
