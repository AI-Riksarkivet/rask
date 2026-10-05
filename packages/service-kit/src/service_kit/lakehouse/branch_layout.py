"""Where a branch's files live, and which branch names would land inside another branch's files ([[LH-203]]).

The format joins a branch name verbatim onto ``tree/``: `lance_docs/file_format.md` § "Branch Dataset
Layout" puts a branch's files at ``{dataset_root}/tree/{branch_name}/`` and says the name is used "as is
to form the path, which means `/` would create a logical subdirectory". It also allows `_` in a segment
(§ "Branch Name", rule 5). Nothing in the format stops branch ``a/_versions`` from laying its files under
``tree/a/_versions/``, which is branch ``a``'s own version directory, or ``a/b`` from sitting inside
``tree/a/``. Lance treats whatever lies under a branch's directories as that branch's. Measured on
pylance 12.0.0:

* ``cleanup_old_versions`` through ``tree/a`` lists the manifests of ``a/_versions`` as ``a``'s own and
  deletes the ones below ``a``'s head; a cold read of ``a/_versions`` then fails ``Not found``;
* ``branches.delete("a/_versions")`` removes ``tree/a/_versions`` recursively, which is ``a``'s whole
  history, and ``a`` no longer opens;
* ``branches.delete("a")`` while ``a/b`` exists removes only the ref and leaves ``tree/a``'s manifests,
  transactions and data behind.

So the layout is a rule the estate keeps, and this module is that rule in one place: the catalog's
create, delete and vend doors and maintenance's reclaim all ask it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final
from urllib.parse import unquote


#: The directories a branch writes its own files into under ``tree/<branch>/``. `file_format.md` § "Branch
#: Dataset Layout" lists the first four; ``data`` holds the files a branch writes itself (measured on
#: pylance 12.0.0: an append to a branch lands its fragment under ``tree/<branch>/data/``). A
#: branch-scoped maintain credential grants exactly these and nothing else under the branch; a
#: branch-scoped write credential grants only ``data`` ([[LH-202]]).
BRANCH_FILE_DIRS: Final = ("_versions", "_transactions", "_deletions", "_indices", "data")

#: Segment names a branch may not carry: the directories above, plus the dataset-root names a branch
#: directory would shadow (``_refs``, ``_mem_wal``, and ``tree`` itself). A segment equal to one of them
#: puts the branch's files where Lance looks for another ref's.
RESERVED_SEGMENTS: Final = frozenset({*BRANCH_FILE_DIRS, "_refs", "_mem_wal", "tree"})

#: Where Lance keeps one JSON file per branch, `file_format.md` § "Branch Metadata Path". A ``/`` in a
#: branch name is written ``%2F`` in the file name.
BRANCH_REFS_DIR: Final = "_refs/branches"

#: The directory a dataset root keeps its branches under.
BRANCH_CONTAINER: Final = "tree"


def reserved_segment(name: str) -> str | None:
    """The first segment of ``name`` that is a layout name, or ``None``."""
    return next((segment for segment in name.split("/") if segment in RESERVED_SEGMENTS), None)


def branches_inside(name: str, branches: Iterable[str]) -> list[str]:
    """The branches whose directory lies under ``tree/<name>/``, deepest first.

    Deepest first because that is the order a delete has to take them in: removing a parent before its
    child leaves the parent's files behind (Lance keeps a directory another branch lives in).
    """
    return sorted((b for b in branches if b.startswith(f"{name}/")), key=lambda b: (-b.count("/"), b))


def branches_enclosing(name: str, branches: Iterable[str]) -> list[str]:
    """The branches whose directory ``tree/<name>/`` lies under, shallowest first."""
    return sorted((b for b in branches if name.startswith(f"{b}/")), key=lambda b: (b.count("/"), b))


def lies_in_files_of(name: str, parent: str) -> bool:
    """Whether branch ``name``'s directory is one of ``parent``'s own file directories or inside one.

    ``a/_versions`` and ``a/data/x`` are; ``a/b`` is not. Deleting such a branch removes ``parent``'s files
    with it, because Lance removes the branch's directory recursively.
    """
    if not name.startswith(f"{parent}/"):
        return False
    return name[len(parent) + 1 :].split("/", 1)[0] in BRANCH_FILE_DIRS


def branch_name_from_ref_file(file_name: str) -> str | None:
    """The branch a ``_refs/branches`` entry names, or ``None`` for anything that is not a ref file."""
    if not file_name.endswith(".json"):
        return None
    return unquote(file_name.removesuffix(".json")) or None


def branch_named_by(location: str, branches: Iterable[str]) -> str | None:
    """The branch whose directory ``location`` is (``<root>/tree/<name>``), or ``None``.

    The longest match wins, so ``<root>/tree/a/b`` names ``a/b`` and not ``a``.
    """
    path = location.rstrip("/")
    named = [b for b in branches if path.endswith(f"/{BRANCH_CONTAINER}/{b}")]
    return max(named, key=len, default=None)
