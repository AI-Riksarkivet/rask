"""What the erasure suites share: the subject they erase, the rows that carry it, the verification of a table's heads,
and the erasure doors on an app of their own.

The conftest's ``erase`` fixture runs the bring-your-own job and then :func:`verify_heads`; a suite whose verification
takes more than a table and a retention keeps a wrapper of its own.
"""

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Final

import lance
import pyarrow as pa
from fastapi import FastAPI
from lance_namespace import LanceNamespace

from catalog.api.dependencies import get_namespace, get_storage_options, request_settings
from catalog.api.v1.endpoints import erasure as door
from catalog.core.config import Settings
from catalog.services.erasure import ErasureReport, ref_heads, verify
from lakehouse.events.control_emit import NoopControlEmitter
from lakehouse.problems.ns_errors import install_problem_handlers
from lakehouse.storage.options import StorageOptions


__all__ = ["NOW", "PREDICATE", "SUBJECT", "Erase", "erasure_doors", "rows", "verify_heads"]

#: The person an erasure is for, and the predicate that finds their rows in :func:`rows`'s ``pii`` column.
SUBJECT: Final = "alice"
PREDICATE: Final = f"pii = '{SUBJECT}'"

#: A retention window that keeps no version back from the reclaim.
NOW: Final = timedelta(0)

#: An erasure run and verified: ``erase(uri)``, or ``erase(uri, retention)``.
type Erase = Callable[..., ErasureReport]


def rows(*names: str) -> pa.Table:
    """One row per name, in the ``pii`` column the predicates read."""
    return pa.table({"pii": pa.array(list(names))})


def verify_heads(uri: str, predicate: str, *, retention: timedelta = NOW, trusted: dict[str, int] | None = None) -> ErasureReport:
    """The verification of an obligation for ``predicate`` recorded at ``trusted``'s heads, by default the table's heads now."""
    return verify(
        lance.dataset(uri),
        storage_options=StorageOptions(),
        reopen=lambda: lance.dataset(uri),
        table="acme-bronze$subjects",
        predicate=predicate,
        retention=retention,
        trusted=trusted if trusted is not None else ref_heads(lance.dataset(uri)),
        history=None,
        derived=list,
    )


def erasure_doors(namespace: LanceNamespace, settings: Settings) -> FastAPI:
    """The erasure doors on an app of their own over ``namespace``, read with ``settings``, control emission off as the
    lifespan leaves it. A test that names its caller, its clock or its control lane overrides those itself."""
    application = FastAPI()
    install_problem_handlers(application, logging.getLogger(__name__))
    application.include_router(door.table_router)
    application.include_router(door.obligations_router)
    application.state.control_emitter = NoopControlEmitter()
    application.dependency_overrides[request_settings] = lambda: settings
    application.dependency_overrides[get_namespace] = lambda: namespace
    application.dependency_overrides[get_storage_options] = lambda: StorageOptions()
    return application
