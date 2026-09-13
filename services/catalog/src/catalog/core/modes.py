"""The constrained wire vocabularies this catalog accepts, parsed ONCE (catalog-api-16).

``create``'s ``mode`` and ``drop_namespace``'s ``behavior`` are closed sets, and both arrived as a bare
``str | None`` that every decision re-derived for itself with ``(value or "").lower()`` against a
hand-written tuple — four copies of the mode vocabulary across the endpoint and the data plane. On this
door the copies are not cosmetic: they decide whether a create DROPS an existing table, whether it
seeds the caller as owner, and whether a failed grant may compensate by deleting. One vocabulary, one
parser, and a reader can see the whole set in one place.

Both parsers are TOLERANT in exactly the way the copies were: case-insensitive, absent means the
default, and an unrecognised value falls through to the default rather than raising. That tolerance is
preserved deliberately rather than endorsed — every copy behaved this way, and turning a typo'd
``mode`` into a 400 is a contract change for existing callers, not a refactor.
"""

from __future__ import annotations

from enum import StrEnum


class CreateMode(StrEnum):
    """How ``POST /v1/table/{id}/create`` treats a table that already exists.

    ``CREATE`` conflicts, ``OVERWRITE`` drops and re-creates (spec: "the existing table is DROPPED and
    a new table created"), ``EXIST_OK`` keeps the existing table untouched and reports its version.
    """

    CREATE = "create"
    OVERWRITE = "overwrite"
    EXIST_OK = "exist_ok"

    @classmethod
    def parse(cls, raw: str | CreateMode | None) -> CreateMode:
        """Normalise a wire ``mode``. Absent, blank or unrecognised → :attr:`CREATE`.

        ``ExistOk`` (the spec's camel-case spelling) and ``exist_ok`` are the SAME mode; both were
        accepted by every hand-written copy and both stay accepted here. Idempotent, so a caller that
        already holds a :class:`CreateMode` may pass it straight back in.
        """
        return _CREATE_MODES.get(str(raw or "").lower(), cls.CREATE)


#: Every spelling the four hand-written copies accepted, in one table. ``existok`` is the spec's
#: camel-case ``ExistOk`` lowercased; ``exist_ok`` is the snake-case form clients also send.
_CREATE_MODES: dict[str, CreateMode] = {
    "": CreateMode.CREATE,
    "create": CreateMode.CREATE,
    "overwrite": CreateMode.OVERWRITE,
    "existok": CreateMode.EXIST_OK,
    "exist_ok": CreateMode.EXIST_OK,
}


class DropMode(StrEnum):
    """What ``drop_namespace`` does when the namespace to drop IS NOT THERE.

    Orthogonal to :class:`DropBehavior`, which answers what to do about the CONTENTS — the spec's
    request carries both and they decide different things. From the generated model: "Fail (default):
    the server must return 400 indicating the namespace to drop does not exist. Skip: the server must
    return 204 indicating the drop operation has succeeded."

    ``SKIP`` IS THE IDEMPOTENCY LEVER, which is why it is worth its own vocabulary rather than a
    boolean: it is how a client makes a drop safe to retry, so a namespace a previous attempt already
    removed counts as success instead of failing the retry.

    A FOURTH VOCABULARY, and not a :class:`CreateMode`. Parsing ``Fail``/``Skip`` with that one folds
    both to ``CREATE``, so a ``Skip`` would read as the default and change nothing.

    Tolerant in the same way as its siblings — case-insensitive, absent or unrecognised falls to the
    default. The default is ``FAIL`` because that is the spec's, and because it is the direction that
    errors rather than silently reporting a drop that never happened.
    """

    FAIL = "fail"
    SKIP = "skip"

    @classmethod
    def parse(cls, raw: str | DropMode | None) -> DropMode:
        """Normalise a wire ``mode``. Absent, blank or unrecognised → :attr:`FAIL`."""
        return cls.SKIP if str(raw or "").lower() == cls.SKIP.value else cls.FAIL


class DropBehavior(StrEnum):
    """What ``drop_namespace`` does about the namespace's contents.

    ``RESTRICT`` refuses a non-empty namespace; ``CASCADE`` takes the subtree with it (recoverably,
    when a trash grace period is configured).
    """

    RESTRICT = "restrict"
    CASCADE = "cascade"

    @classmethod
    def parse(cls, raw: str | DropBehavior | None) -> DropBehavior:
        """Normalise a wire ``behavior``. Absent, blank or unrecognised → :attr:`RESTRICT`.

        Defaulting the unknown case to RESTRICT is the safe direction and matches what the single
        ``== "cascade"`` comparison already did: anything that was not cascade behaved as restrict.
        """
        return cls.CASCADE if str(raw or "").lower() == cls.CASCADE.value else cls.RESTRICT
