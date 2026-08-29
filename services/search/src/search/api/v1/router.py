"""Search endpoints — GET (query string) and POST (multipart, image upload).

Ported from ``backend.search.router`` (minus the doc-chunks /
chunk-alignments sub-resources, which belong to the media group). Both handlers
build a :class:`SearchSpec`, short-circuit empty input, and delegate to the
framework-free :func:`run_search`. The wire shape is unchanged — same paths,
params, and hit shape — plus one addition: an OPTIONAL ``dataset`` query param
selecting the Lance DB (``None`` → the default dataset). Metadata filter params
are no longer hardcoded fields: whatever names the dataset's descriptor lists
under ``search.filterable`` are read dynamically from the query string / form,
so ``?language=sv`` keeps working against the default corpus without this
module knowing the name.

GET stays sync (threadpooled); POST is async to await the upload, then offloads
the blocking vLLM + Lance work. No ``from __future__ import annotations`` here:
FastAPI introspects these signatures at runtime, so the annotations stay real
objects.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from pydantic import ValidationError as PydanticValidationError
from starlette.concurrency import run_in_threadpool

from search.api.dependencies import EmbedderFactoryDep, RerankerFactoryDep, StateDep
from search.api.security import AuthorizedCorpora, CheckerDep, CurrentSubject, SettingsDep, require_search
from search.core.rate_limit import SEARCH_LIMIT, SEARCH_SCOPE, limiter
from search.services.filters import TOPIC_FILTER, extract_filters
from search.services.fuse import reciprocal_rank_fusion
from search.services.result_cache import run_cached
from search.services.service import run_search
from search.services.similar import SimilarSpec, drop_seed, key_predicate, seed_vector
from search.services.spec import PostSearchSpec, SearchMode, SearchSpec
from search.services.target import resolve_target
from search.services.vector import vector_search
from service_kit.exceptions import DomainError, ValidationError
from service_kit.lancekit.registry import DatasetHandle
from service_kit.media.state import AppState, dataset_handle


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api", tags=["search"])

#: Cap on an uploaded query image, matching the voice upload's 25 MB bound.
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _filterable(handle: DatasetHandle, table: str | None = None) -> list[str]:
    """The filter fields the SELECTED searchable table declares.

    Table-aware, and it has to be: `filterable` lives on each `Search`, so reading the DEFAULT
    table's list while searching another would silently drop that table's own filters and silently
    accept the default's — filters that do not exist on the rows being searched.
    """
    search = handle.descriptor.declared.search_named(table)
    return search.filterable if search is not None else []


def _cached_search(
    state: AppState,
    handle: DatasetHandle,
    spec: SearchSpec,
    filters: dict[str, str],
    image_bytes: bytes | None,
    get_embedder: Any,
    get_reranker: Any,
) -> list[dict[str, Any]]:
    """Run the search behind the version-keyed result cache (blocking; the POST
    path offloads this whole call to the threadpool). ``run_search`` stays the
    single source of truth — the cache only memoizes its output per query."""

    def _run() -> list[dict[str, Any]]:
        return run_search(
            handle,
            get_embedder=get_embedder,
            get_reranker=get_reranker,
            spec=spec,
            filters=filters,
            image_bytes=image_bytes,
        )

    return run_cached(
        state.search_cache,
        state.settings.search_cache_size,
        state.settings.search_cache_bytes,
        handle,
        spec,
        filters,
        image_bytes,
        _run,
    )


def _spec_from_query(request: Request) -> SearchSpec:
    """Bind ``SearchSpec`` from the flat query string OURSELVES — never via FastAPI's
    query-param-model pattern (``Annotated[SearchSpec, Query()]``).

    That pattern is BROKEN on the deployed FastAPI (0.140.13, measured 2026-08-07): under a real
    uvicorn socket the model never flattens — the server demands a single required ``spec`` param and
    422s every flat call, which killed live search estate-wide — while ``TestClient`` binds the same
    request happily, so no in-process test can see it. Isolated by driving the identical app object
    through both paths inside the running pod. ``SearchSpec`` already owns validation, clamping and
    ``extra="ignore"`` (filters + ``corpus`` ride the same query string), so binding is one call; a
    type error (``n=abc``) surfaces as the same clean 422 problem body the old wire shape promised.
    """
    try:
        return SearchSpec.model_validate(dict(request.query_params))
    except PydanticValidationError as exc:
        raise ValidationError(str(exc)) from exc


@router.get("/search")
@limiter.shared_limit(SEARCH_LIMIT, scope=SEARCH_SCOPE)
def search_get(
    request: Request,
    state: StateDep,
    get_embedder: EmbedderFactoryDep,
    get_reranker: RerankerFactoryDep,
    # FAN-OUT, ALREADY AUTHORIZED. `?corpus=a&corpus=b` still declares the fan-out (the dependency
    # takes the same query param), but what arrives here is the SUBSET this caller may read —
    # filtered, not refused, per `datasets.list_datasets`' rule. `None` means a single-corpus search,
    # which the dependency has already 403'd if the caller is not entitled to it. The gate is a
    # dependency because this handler is a sync `def` with a blocking Lance body and cannot await
    # the checker itself (open_python-audit X6 / VS-13).
    corpus: AuthorizedCorpora = None,
) -> list[dict[str, Any]]:
    spec = _spec_from_query(request)
    if corpus is not None:
        # An empty list means the caller asked to fan out and may read NONE of them: an empty result
        # is the same shorter-list answer, not an error.
        return _fused_search(state, corpus, request, spec, get_embedder, get_reranker) if corpus else []
    handle = dataset_handle(state, spec.dataset)
    filters = extract_filters(request.query_params, _filterable(handle, spec.table))
    # Empty-input short-circuit: only the topic facet triggers filter-only
    # browse (the Tree page contract); other filters without a query stay [].
    if not spec.q and not spec.q_vec and not filters.get(TOPIC_FILTER):
        return []
    return _cached_search(state, handle, spec, filters, None, get_embedder, get_reranker)


def _fused_search(
    state: AppState,
    corpora: list[str],
    request: Request,
    spec: SearchSpec,
    get_embedder: Any,
    get_reranker: Any,
) -> list[dict[str, Any]]:
    """Search several corpora and fuse the results by RECIPROCAL RANK.

    Not by score: BM25 is normalised per index and vector distances depend on the space, so
    `_score` from one corpus and `_score` from another are not comparable. Sorting a merged list by
    them produces an ordering that looks authoritative and means nothing — and nothing in the rows
    would show it. See `services/fuse.py`.

    Each corpus keeps its OWN filters, because `filterable` is declared per searchable table: a
    filter valid in one corpus may name a column another does not have. A corpus that refuses the
    request (unknown table, no search bindings) is SKIPPED with its reason logged rather than failing
    the whole fan-out — one misconfigured corpus should not take the others down with it.

    Sequential, deliberately: `run_search` is CPU- and IO-blocking and already offloaded by the
    caller, and the corpus count is small. Concurrency here would need a bounded pool to avoid N
    simultaneous embedder calls, which is a change worth making when a measurement asks for it.
    """
    ranked: list[list[dict[str, Any]]] = []
    key_fields: list[str] = []
    for dataset_id in dict.fromkeys(corpora):  # de-duplicated, order preserved
        try:
            handle = dataset_handle(state, dataset_id)
            filters = extract_filters(request.query_params, _filterable(handle, spec.table))
            if not spec.q and not spec.q_vec and not filters.get(TOPIC_FILTER):
                continue
            hits = _cached_search(state, handle, spec, filters, None, get_embedder, get_reranker)
        except DomainError as exc:
            logger.warning("fan-out: skipping corpus %s — %s", dataset_id, exc)
            continue
        if hits and not key_fields:
            key_fields = list(handle.descriptor.declared.identity.key_fields)
        ranked.append(hits)

    if not key_fields:
        return []
    return reciprocal_rank_fusion(ranked, key_fields=key_fields, limit=spec.n)


def _post_spec(
    q: Annotated[str, Form()] = "",
    n: Annotated[int, Form()] = 20,
    mode: Annotated[str, Form()] = SearchMode.HYBRID.value,
    rerank: Annotated[bool, Form()] = False,
    rerank_n: Annotated[int, Form()] = 20,
    weight: Annotated[float | None, Form()] = None,
    fuzziness: Annotated[int, Form()] = 0,
    phrase: Annotated[bool, Form()] = False,
    q_vec: Annotated[str, Form()] = "",
    where: Annotated[str | None, Form()] = None,
    prefilter: Annotated[bool, Form()] = True,
) -> PostSearchSpec:
    """Marshal the multipart form fields into the spec model.

    A dependency (not an ``Annotated[…, Form()]`` model on the route) because a
    Form model next to a ``File()`` param makes FastAPI EMBED the model — it
    would then expect a literal ``spec`` form key instead of the flat fields.
    This keeps the wire shape flat and ``spec.py`` free of FastAPI imports. The
    descriptor-declared filter fields are read off the parsed form in the
    handler (Starlette caches it, so the second read is free).
    """
    return PostSearchSpec(
        q=q,
        n=n,
        mode=mode,
        rerank=rerank,
        rerank_n=rerank_n,
        weight=weight,
        fuzziness=fuzziness,
        phrase=phrase,
        q_vec=q_vec,
        where=where,
        prefilter=prefilter,
    )


@router.post("/search")
@limiter.shared_limit(SEARCH_LIMIT, scope=SEARCH_SCOPE)
async def search_post(
    request: Request,
    state: StateDep,
    get_embedder: EmbedderFactoryDep,
    get_reranker: RerankerFactoryDep,
    spec: Annotated[PostSearchSpec, Depends(_post_spec)],
    subject: CurrentSubject,
    checker: CheckerDep,
    settings: SettingsDep,
    image: Annotated[UploadFile | None, File()] = None,
    dataset: Annotated[str | None, Query(description="Dataset id (default DB when omitted)")] = None,
    table: Annotated[str | None, Query(description="Searchable table name (the corpus default when omitted)")] = None,
) -> list[dict[str, Any]]:
    await require_search(state, dataset, table=table, subject=subject, checker=checker, settings=settings)
    image_bytes = None
    if image is not None:
        # Bound the read so a large multipart part can't be buffered whole
        # before validation (mirrors the voice upload's cap).
        image_bytes = await image.read(_MAX_IMAGE_BYTES + 1)
        if len(image_bytes) > _MAX_IMAGE_BYTES:
            raise ValidationError(f"image upload exceeds {_MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    # First resolution opens the Lance DB (blocking IO) — keep the loop free.
    handle = await run_in_threadpool(dataset_handle, state, dataset)
    form = await request.form()  # already parsed by FastAPI; cached by Starlette
    form_values = {k: v for k, v in form.items() if isinstance(v, str)}
    filters = extract_filters(form_values, _filterable(handle, table))
    # The POST takes `table` as a QUERY param (the form carries the spec fields), but `run_search`
    # reads `spec.table`. Setting it here is what makes the two agree — without it the selector
    # would filter by the right table's fields and then SEARCH the default one.
    spec = spec.model_copy(update={"table": table})
    if not spec.q and not spec.q_vec and not image_bytes and not filters.get(TOPIC_FILTER):
        return []
    # The cache lookup does blocking version reads and run_search makes blocking
    # vLLM (httpx) + Lance calls — offload the whole cached path off the event loop.
    return await run_in_threadpool(_cached_search, state, handle, spec, filters, image_bytes, get_embedder, get_reranker)


def _run_similar(state: AppState, spec: SimilarSpec, dataset: str | None, table: str | None) -> list[dict[str, Any]]:
    """The whole "more like this" path, off the event loop (every call below is blocking Lance IO)."""
    handle = dataset_handle(state, dataset)
    target = resolve_target(handle, table)

    # WHICH vector space. A named one must exist — falling back to the default would answer a
    # different question than the one asked ("visually similar" vs "semantically similar" are not
    # interchangeable), and the swap would be invisible in the results.
    if not target.vectors:
        raise ValidationError(f"corpus {target.dataset_id!r} declares no vector space — there is nothing to be similar in")
    if spec.space is not None and spec.space not in target.vectors:
        raise ValidationError(f"unknown vector space {spec.space!r}; this corpus declares {sorted(target.vectors)}")
    space = spec.space or next(iter(target.vectors))
    binding = target.vectors[space]

    vec_tbl = target.table_for(binding.table)
    if vec_tbl is None:
        # `resolve_target` degrades an unopenable binding table to absent so the OTHER search legs
        # still rank. Here it is the only leg, so absent means the request cannot be answered.
        raise ValidationError(f"vector table {binding.table!r} could not be opened")

    where = key_predicate(handle.descriptor.declared, spec.key)
    vec = seed_vector(vec_tbl, where=where, column=binding.column)

    hits = vector_search(
        vec_tbl,
        _AsList(vec),
        binding.column,
        payload_columns=target.payload_columns,
        n=spec.fetch_n,
        where=spec.where,
    )
    # The seed is its own nearest neighbour at distance 0. Dropped AFTER the search rather than
    # excluded in the WHERE, because a prefilter on the key would also cost the index.
    return drop_seed(hits, spec.key, target.key_fields)[: spec.n]


class _AsList:
    """`vector_search` calls `.tolist()` on its query vector (it is written for numpy). The seed
    vector arrives as a plain list, so this adapts it rather than making the shared retrieval
    module care which of the two it was handed."""

    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return self._values


@router.get("/search/similar")
@limiter.shared_limit(SEARCH_LIMIT, scope=SEARCH_SCOPE)
async def search_similar(
    # `request` exists for slowapi, which requires it in the signature. THE SAME SCOPE as /search on
    # purpose: this route drives the same Lance ANN fan-out from a stored seed vector (no embedder,
    # so the GPU half is smaller — but a third unmetered door beside two metered ones is budget in
    # rotation, which is what the shared scope exists to prevent). Found unmetered by the adversarial
    # re-audit of the search closure, whose note claimed all three entry points were covered.
    request: Request,
    state: StateDep,
    subject: CurrentSubject,
    checker: CheckerDep,
    settings: SettingsDep,
    key: Annotated[str, Query(description="The seed row, its identity fields joined by '/'")],
    n: Annotated[int, Query(ge=1, le=200, description="How many neighbours")] = 24,
    space: Annotated[str | None, Query(description="Declared vector space (the corpus's first when omitted)")] = None,
    where: Annotated[str | None, Query(description="Raw SQL filter, ANDed")] = None,
    dataset: Annotated[str | None, Query(description="Dataset id (default DB when omitted)")] = None,
    table: Annotated[str | None, Query(description="Searchable table name (the corpus default when omitted)")] = None,
) -> list[dict[str, Any]]:
    """ "More like this" — the k nearest rows to one the caller already has.

    Search answers a query; the query for "another four hundred pages that look like this one" is
    the page itself. Its embedding is already in the table, and until now nothing could reach it.

    This is `open_browse.md` §3, the bulk surface's example-driven selector: pick one item, get a
    labelling batch. The atlas lasso is a second CALLER of this, not a second mechanism.

    The seed is excluded from its own results — it ranks first at distance 0, and a "more like this"
    whose best answer is the thing you clicked reads as broken.
    """
    # AUTHORIZED BEFORE THE SEED IS READ. This route takes a raw SQL `where` ANDed into the query
    # (open_python-audit VS-13 names that as the sharp edge of the service having no authz at all);
    # the predicate stays — `duration > 60` is a real feature — but it can now only ever run against
    # the corpus TABLE this caller may read — `table=`, the same one `_run_similar` resolves rows from.
    await require_search(state, dataset, table=table, subject=subject, checker=checker, settings=settings)
    spec = SimilarSpec(key=key, n=n, space=space, where=where)
    return await run_in_threadpool(_run_similar, state, spec, dataset, table)
