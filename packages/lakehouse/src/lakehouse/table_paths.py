"""The two places a table recorded at a location keeps its objects: the key its location decodes to, and the location as
spelled ([[LH-319]]).

``describe_table`` answers a location percent-encoding the table's name. pylance opens it at each segment decoded once
(:func:`lakehouse.base_refs.decoded_path`), and the ``dir`` namespace writes the table's reservation marker under the
location as spelled: measured on pylance 13.0.0 through the catalog's create door over moto, ``räksmörgås`` keeps its
versions, data and branches under ``…$räksmörgås/`` and the marker under ``…$r%C3%A4ksm%C3%B6rg%C3%A5s/``, and ``sp ace``
keeps both under ``…$sp ace/``. The two are one place for a name that needs no escape.

Every function that reads the store is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from lakehouse.base_refs import decoded_path, normalise, store_of
from lakehouse.storage_options import StorageOptions
from lakehouse.stored_objects import StoredObject, list_under


def spellings(location: str) -> tuple[str, str]:
    """``location`` as the key it decodes to, where its dataset is, and as spelled, where its reservation marker is: two
    locations the store lists under, equal for a name that needs no escape. A ``file://`` location is a local path."""
    scheme = store_of(location)
    if scheme == "file":
        return f"/{decoded_path(location)}", f"/{normalise(location)}"
    return f"{scheme}://{decoded_path(location)}", f"{scheme}://{normalise(location)}"


def objects_at(location: str, storage_options: StorageOptions) -> list[StoredObject]:
    """Every object the table recorded at ``location`` holds, under both of its :func:`spellings`.

    Listed by key and nothing else (:func:`lakehouse.stored_objects.list_under`), what a session scoped to the location
    may ask.
    """
    found = {stored.path: stored for place in dict.fromkeys(spellings(location)) for stored in list_under(place, storage_options)}
    return list(found.values())
