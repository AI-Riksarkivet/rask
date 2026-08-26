"""The search service's limiter — module-level, because the decorators are import-time.

`@limiter.limit(...)` is evaluated when `router.py` is imported, so a limiter built inside the
lifespan could not back it. The mechanism, the key function and the problem+json 429 all live in
`service_kit.rate_limit`; only the instance and the rate belong to this service.

WHY THESE ROUTES AND WHY ONE BUCKET. `/search`, `POST /search` and `/search/similar` all drive the
same expensive resource — a GPU embedding forward pass, and a cross-encoder rerank on top when it is
asked for. They share a `scope` so the three cannot be used in rotation to get three times the
budget: slowapi counts decorators with the same scope into one bucket.

30/minute is deliberately above interactive human use and far below a script's. A person refining a
query does not reach it; a loop does so immediately.
"""

from service_kit.rate_limit import make_limiter


limiter = make_limiter()

#: One bucket for every search entry point — see the module docstring.
SEARCH_LIMIT = "30/minute"
SEARCH_SCOPE = "search"
