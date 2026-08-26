"""The ONE naming rule for a corpus table's FGA object.

This lived in `viewer/api/security.py`, whose own docstring already stated why it must not be copied:
"Kept here beside `corpus_object` so both naming rules live in one file: the way this goes wrong is a
second module deriving an object string that agrees until it does not."

The annotator needs the same object for its assist plane, so the rule moved here rather than gaining a
second author. Both services' settings extend `service_kit.media.config.Settings`, which is where
`catalog_table_id` and `catalog_delimiter` live — so the shared type was always the right home; the
function was simply written in the first service that needed it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from service_kit.media.config import Settings


def corpus_object(settings: Settings, dataset_id: str, table: str) -> str:
    """The FGA object for one corpus table: ``table:<namespace><delimiter><table>``.

    Built through `catalog_table_id` so the object a service checks is the SAME identifier the
    annotator writes through and the catalog authorizes on. Deriving it any other way creates a second
    naming scheme that agrees until someone sets `MEDIA_CATALOG_NAMESPACE`.
    """
    segments = settings.catalog_table_id(dataset_id, table)
    return f"table:{settings.catalog_delimiter.join(segments)}"


def table_object(table_id: str) -> str:
    """The FGA object for a caller-supplied CATALOG TABLE ID (``bronze$pages``) — ``table:<id>``.

    Routes addressed by catalog table id directly need no `catalog_table_id` mapping: the identifier
    the caller passes IS the one the catalog authorizes on. Kept beside `corpus_object` so both naming
    rules stay in one file.
    """
    return f"table:{table_id}"
