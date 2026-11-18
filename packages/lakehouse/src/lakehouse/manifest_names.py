"""The names a ref's manifests carry under ``_versions/``: the one place a lakehouse process spells or reads one.

``lance_docs/lance/format/table/transaction.md`` § Manifest Naming Schemes gives a committed manifest two names: V1
``{version}.manifest`` and V2 ``{u64::MAX - version:020}.manifest``, whose reverse order lets a listing find the latest
version first. Two more names are the catalog's managed versioning (ADR 0157): a stock writer stages
``<stem>.manifest-<uuid>``, which no reader takes for a version, and the catalog judges a commit on a detached copy,
``d<id>.manifest``, before it finalizes it.
"""

from __future__ import annotations

import re
from typing import Final


#: The directory of a ref that holds its manifests.
VERSIONS_DIR: Final = "_versions"

#: ``u64::MAX``, which a V2 manifest's name counts its version down from.
U64_MAX: Final = (1 << 64) - 1

_V2_DIGITS: Final = 20
_DETACHED: Final = re.compile(r"d\d+\.manifest")
_STAGED: Final = re.compile(r"(?P<stem>\d+)\.manifest-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def committed_names(version: int) -> tuple[str, str]:
    """The two names the committed manifest of ``version`` may carry: V2 first, the scheme every table the catalog
    creates is written in, then V1."""
    return f"{U64_MAX - version:0{_V2_DIGITS}d}.manifest", f"{version}.manifest"


def _version_of(stem: str) -> int:
    """The version a manifest name's digits name: counted down from ``u64::MAX`` in a V2 stem, the version itself in V1."""
    return U64_MAX - int(stem) if len(stem) == _V2_DIGITS else int(stem)


def committed_version(path: str) -> int | None:
    """The version a committed manifest's file name names, in either scheme; ``None`` for any other name (a staged or a
    detached one, which no cleanup lists)."""
    stem, dot, extension = path.rsplit("/", 1)[-1].partition(".")
    if dot != "." or extension != "manifest" or not stem.isdigit():
        return None
    return _version_of(stem)


def detached_name(manifest_id: int) -> str:
    """The name of the detached manifest ``manifest_id``."""
    return f"d{manifest_id}.manifest"


def is_detached(path: str) -> bool:
    """Whether the file name is a detached manifest's."""
    return _DETACHED.fullmatch(path.rsplit("/", 1)[-1]) is not None


def staged_version(path: str) -> int | None:
    """The version a stock writer staged the manifest for, ``<stem>.manifest-<uuid>`` with the stem of either scheme
    (measured on pylance 13.0.0 against a V2 table: the stem counts down from ``u64::MAX``); ``None`` for any other name."""
    match = _STAGED.fullmatch(path.rsplit("/", 1)[-1])
    return _version_of(match["stem"]) if match is not None else None
