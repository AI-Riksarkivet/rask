"""Query parameters rask adds to operations the Lance Namespace spec owns.

[[LH-021]]. A spec operation cannot move off `/v1` — conformance requires it there — so a capability
rask adds to one has nowhere else to live. What it must not do is enter the DOCUMENT: a spec client
reading the served OpenAPI then meets vocabulary its own spec cannot explain, and has no way to tell
an extension from a version skew it should support.

`include_in_schema=False` is therefore the whole mechanism, and it is deliberately the weakest one
available: it changes what is advertised and nothing about what is accepted, so every existing caller
keeps working. rask's own callers build these requests by hand rather than from the generated client
(`annotator/projects/lakehouse.py` assembles `params` for httpx), which is why hiding them costs
nothing at the call sites.

These aliases exist so the reasoning is written once instead of at nine parameter declarations, and so
a tenth extension is spelled the same way by construction. Pinned by
`tests/integration/test_the_spec_surface_carries_only_spec_parameters.py`, which derives the spec's
own vocabulary from the pinned client rather than listing parameter names.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query


#: A rask-only boolean switch on a spec operation (`force`, `purge`).
RaskFlag = Annotated[bool, Query(include_in_schema=False)]

#: The version-pinned upstream a table DERIVES FROM. Lineage input, not a spec concept.
RaskSource = Annotated[str | None, Query(include_in_schema=False)]
RaskSourceVersion = Annotated[int | None, Query(ge=1, include_in_schema=False)]

#: Approved buckets to spread a table's fragments across (Lance multi-base).
RaskDataBase = Annotated[list[str], Query(include_in_schema=False)]
