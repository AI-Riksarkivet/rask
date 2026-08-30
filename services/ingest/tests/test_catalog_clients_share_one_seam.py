"""Every catalog implementation must accept the call the runtime actually makes.

MEASURED on the live estate 2026-08-26, driving a 600-object backfill::

    "status": "FAILED",
    "errors": {"run": "... Activity task #9 failed:
                CatalogServiceClient.ensure() got an unexpected keyword argument 'external_base'"}

`runtime.py` calls `catalog.ensure(namespace, dataset, external_base=...)`. The LOCAL catalog accepts
that keyword; the in-cluster `CatalogServiceClient` did not. So the parameter was added to one side of
a two-implementation seam, every unit test that exercised the local path stayed green, and the failure
appeared only in-cluster — after the run had been accepted, at the activity that creates the table.

`ingest/catalog.py`'s own docstring states the property that was broken: "the local and service
catalogs present ONE seam … The caller never learns which it has, which is the whole point of the
seam — and is what lets the in-cluster swap be a config change rather than a code change at every
call site." A seam only holds if both sides accept the same call.

Compared by SIGNATURE rather than by calling them, because constructing a real service client needs a
catalog and constructing the local one needs a filesystem root — and neither is what this is about.
The question is whether the runtime's call is expressible against both, which `inspect.signature`
answers exactly.
"""

from __future__ import annotations

import inspect

import pytest

from ingest.catalog import LocalCatalog
from ingest.catalog_service import CatalogServiceClient


#: What `runtime.ensure_dataset_at` actually passes. Keep in step with that call site — a keyword
#: added there and not here would slip through exactly as `external_base` did.
_RUNTIME_KEYWORDS = ("external_base",)

_IMPLEMENTATIONS = (LocalCatalog, CatalogServiceClient)


@pytest.mark.parametrize("impl", _IMPLEMENTATIONS, ids=lambda c: c.__name__)
@pytest.mark.parametrize("keyword", _RUNTIME_KEYWORDS)
def test_every_catalog_accepts_the_keyword_the_runtime_passes(impl: type[LocalCatalog] | type[CatalogServiceClient], keyword: str) -> None:
    signature = inspect.signature(impl.ensure)

    assert keyword in signature.parameters, (
        f"{impl.__name__}.ensure() does not accept {keyword!r}, but ingest/runtime.py passes it — "
        f"so a run against this implementation dies with TypeError after it has been accepted. "
        f"signature: {signature}"
    )


@pytest.mark.parametrize("keyword", _RUNTIME_KEYWORDS)
def test_the_keyword_is_optional_everywhere(keyword: str) -> None:
    """A required parameter on one side would break every OTHER caller — the same asymmetry, mirrored."""
    for impl in _IMPLEMENTATIONS:
        parameter = inspect.signature(impl.ensure).parameters.get(keyword)
        assert parameter is not None, f"{impl.__name__}.ensure() is missing {keyword!r}"
        assert parameter.default is not inspect.Parameter.empty, (
            f"{impl.__name__}.ensure() requires {keyword!r}; callers that have no base must still be able to call it"
        )
