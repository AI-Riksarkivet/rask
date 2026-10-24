"""The annotator's corpus: media datasets read as the person, through the catalog (ADR 0171).

With a live catalog behind it (`MediaSettings.rest_catalog_mode`), a media dataset is a catalog namespace and its
tables are governed tables. Each is opened through the catalog's own doors with pylance's stock namespace client on
the person's bearer, so pylance asks `DescribeTable` for the credential the catalog vends that person and reads with
it. The annotator holds no store credential, so there is no other way in, and a table the person may not read is
refused at the catalog.

Without one (a local stack) a dataset is a local Lance directory, read through the media registry: there is no
credential to hold and no lakehouse to ask.

A dataset's declared roles (row identity, the document binding) come from the `lance_media.descriptor` stamp on one
of its tables, the way the registry reads a dataset with no descriptor file, and are checked against the tables
before a route uses them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import lance
import lance_namespace
from pydantic import BaseModel, ConfigDict

from client_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY, Declared, validate_descriptor
from client_kit.lancekit.introspect import TableInfo, dataset_info
from client_kit.lancekit.registry import DatasetHandle
from client_kit.media.state import dataset_handle
from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError, UnauthorizedError


if TYPE_CHECKING:
    from client_kit.media.config import MediaSettings
    from client_kit.media.state import AppState


logger = logging.getLogger(__name__)


class Corpus(BaseModel):
    """What a route needs of one media dataset: its id, its declared roles, and on a local stack the registry's handle."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    declared: Declared
    #: The registry's handle on a local stack, which a local-mode write opens its table through; None when the catalog
    #: resolved the dataset, whose tables are reached only through the catalog.
    local: DatasetHandle | None = None


@contextmanager
def _as_the_catalog_answers(what: str) -> Generator[None]:
    """The catalog's refusal as the media plane's own: absent is 404, refused 403, an unknown caller 401."""
    try:
        yield
    except (lance_namespace.TableNotFoundError, lance_namespace.NamespaceNotFoundError) as exc:
        raise NotFoundError(f"{what} does not exist") from exc
    except lance_namespace.PermissionDeniedError as exc:
        raise ForbiddenError(f"the catalog refused {what}") from exc
    except lance_namespace.UnauthenticatedError as exc:
        raise UnauthorizedError("the catalog refused the caller's credential") from exc
    except lance_namespace.LanceNamespaceError as exc:
        raise ServiceUnavailableError(f"the catalog could not answer for {what}: {exc}") from exc


def catalog_client(settings: MediaSettings, caller_token: str | None) -> lance_namespace.LanceNamespace:
    """The stock REST namespace client, as the caller. No bearer is sent for an anonymous caller: the catalog decides."""
    if not settings.catalog_uri:
        raise ServiceUnavailableError("MEDIA_CATALOG_URI is not configured, so there is no catalog to read the corpus through")
    options = {"uri": settings.catalog_uri, "delimiter": settings.catalog_delimiter}
    if caller_token:
        options["headers.Authorization"] = f"Bearer {caller_token}"
    return lance_namespace.connect("rest", options)


def resolve(state: AppState, dataset_id: str | None, caller_token: str | None) -> Corpus:
    """One media dataset: through the catalog as the caller on a live catalog, else from the local registry.

    Blocking (catalog calls, a manifest read per table), so an async route runs it in the threadpool.
    """
    settings = state.settings
    if not settings.rest_catalog_mode:
        handle = dataset_handle(state, dataset_id)
        return Corpus(id=handle.id, declared=handle.descriptor.declared, local=handle)
    dataset = dataset_id or settings.default_dataset_id
    namespace = settings.catalog_namespace or dataset
    client = catalog_client(settings, caller_token)
    with _as_the_catalog_answers(f"dataset {dataset!r}"):
        names = list(client.list_tables(lance_namespace.ListTablesRequest(id=[namespace])).tables or [])
        tables: dict[str, TableInfo] = {}
        declared: Declared | None = None
        for name in names:
            opened = lance.dataset(namespace_client=client, table_id=[namespace, name])
            tables[name] = dataset_info(opened, name)
            stamp = (opened.schema.metadata or {}).get(DESCRIPTOR_METADATA_KEY)
            if declared is None and stamp:
                declared = Declared.model_validate(json.loads(stamp.decode()))
    if declared is None:
        raise NotFoundError(f"dataset {dataset!r} declares no descriptor: no table carries the {DESCRIPTOR_METADATA_KEY.decode()} stamp")
    if problems := validate_descriptor(declared, tables):
        raise NotFoundError(f"dataset {dataset!r}: invalid descriptor: " + "; ".join(problems))
    return Corpus(id=dataset, declared=declared)


def input_tables(state: AppState, names: list[str], caller_token: str | None) -> dict[str, str | None] | None:
    """For each of ``names`` the catalog shows the caller, the governed table an item's captured version is a version of.

    A name carrying the delimiter is a table, its own input. Any other is a dataset, whose items are rows of its default
    search's row table: the table whose version a sender captures (the zone's `rowTableVersion`), so that table at that
    version is what a publish of the items names as its input, which a write to a derived tier must. None for a dataset
    that declares no search. A name missing from the answer is one the caller cannot open on the canvas: absent,
    refused, or a dataset with no valid descriptor. None when the catalog could not be asked.
    """
    settings = state.settings
    client = catalog_client(settings, caller_token)
    found: dict[str, str | None] = {}
    for name in names:
        identifier = name.split(settings.catalog_delimiter)
        try:
            if len(identifier) > 1:
                client.table_exists(lance_namespace.TableExistsRequest(id=identifier))
                found[name] = name
            else:
                search = resolve(state, name, caller_token).declared.search
                found[name] = settings.catalog_delimiter.join(settings.catalog_table_id(name, search.row_table)) if search else None
        except (
            lance_namespace.TableNotFoundError,
            lance_namespace.NamespaceNotFoundError,
            lance_namespace.PermissionDeniedError,
            NotFoundError,
            ForbiddenError,
        ):
            continue
        except (lance_namespace.LanceNamespaceError, ServiceUnavailableError):
            logger.warning("send could not ask the catalog about %s; not refusing on a check that did not run", name)
            return None
    return found
