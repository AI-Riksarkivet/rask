"""Search endpoints — GET (query string) and POST (multipart, image upload).

Ported from ``backend.search.router`` (minus the doc-transcript /
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

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from search.api.dependencies import EmbedderFactoryDep, RerankerFactoryDep, StateDep
from search.services.filters import TOPIC_FILTER, extract_filters
from search.services.result_cache import run_cached
from search.services.service import run_search
from search.services.spec import PostSearchSpec, SearchMode, SearchSpec
from service_kit.lancekit.registry import DatasetHandle
from service_kit.media.exceptions import ValidationError
from service_kit.media.state import AppState, dataset_handle


router = APIRouter(prefix="/api", tags=["search"])

#: Cap on an uploaded query image, matching the voice upload's 25 MB bound.
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _filterable(handle: DatasetHandle) -> list[str]:
    search = handle.descriptor.declared.search
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
        handle,
        spec,
        filters,
        image_bytes,
        _run,
    )


@router.get("/search")
def search_get(
    request: Request,
    state: StateDep,
    get_embedder: EmbedderFactoryDep,
    get_reranker: RerankerFactoryDep,
    # The generic knobs bind to the spec model (FastAPI Pydantic query-param
    # model): each field is one query param, validated + clamped by SearchSpec
    # itself — including the optional `dataset` selector. The descriptor-declared
    # filter params ride alongside in the same query string and are extracted
    # below (extra="ignore" keeps them out of the model), so the wire shape is
    # unchanged.
    spec: Annotated[SearchSpec, Query()],
) -> list[dict[str, Any]]:
    handle = dataset_handle(state, spec.dataset)
    filters = extract_filters(request.query_params, _filterable(handle))
    # Empty-input short-circuit: only the topic facet triggers filter-only
    # browse (the Tree page contract); other filters without a query stay [].
    if not spec.q and not spec.q_vec and not filters.get(TOPIC_FILTER):
        return []
    return _cached_search(state, handle, spec, filters, None, get_embedder, get_reranker)


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
async def search_post(
    request: Request,
    state: StateDep,
    get_embedder: EmbedderFactoryDep,
    get_reranker: RerankerFactoryDep,
    spec: Annotated[PostSearchSpec, Depends(_post_spec)],
    image: Annotated[UploadFile | None, File()] = None,
    dataset: Annotated[str | None, Query(description="Dataset id (default DB when omitted)")] = None,
) -> list[dict[str, Any]]:
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
    filters = extract_filters(form_values, _filterable(handle))
    if not spec.q and not spec.q_vec and not image_bytes and not filters.get(TOPIC_FILTER):
        return []
    # The cache lookup does blocking version reads and run_search makes blocking
    # vLLM (httpx) + Lance calls — offload the whole cached path off the event loop.
    return await run_in_threadpool(_cached_search, state, handle, spec, filters, image_bytes, get_embedder, get_reranker)
