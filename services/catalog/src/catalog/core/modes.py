"""The constrained wire vocabularies this catalog accepts, each parsed ONCE into a CLOSED enum (catalog-api-16).

Every ``mode`` and ``behavior`` the spec defines on a door the catalog serves is a closed set:
``lance_docs/ns_catalog/spec.yaml`` gives each one as "Case insensitive, supports both PascalCase and
snake_case. Valid values are: …". Each decides something a caller cannot take back — whether a create
DROPS an existing table, whether it seeds the caller as owner, whether a drop takes a subtree with it —
so each gets one enum, one parser, and a reader can see the whole vocabulary in one place.

CLOSED, NOT TOLERANT (owner ruling 2026-09-25). A value outside the vocabulary raises
:class:`~lance_namespace.InvalidInputError` — spec code 13, answered 400 by the problem handlers —
naming the value and the valid set. Absent (``None``, or the blank an empty query parameter arrives as)
still means the spec's ``(default)``, because that is the spec's own answer for an omitted field.
Folding an UNKNOWN value to the default answers a request the caller did not send, and on these doors
that is a write reported as success: a typo'd create ``mode`` creates a table, and a drop whose
``mode`` is ``PURGE`` (``purge`` is a query parameter) drops recoverably and writes a fresh trash
record. Measured 2026-09-18: four such records on the live estate, each read as a successful purge.

It is also the upstream answer. Measured on pylance 12.0.0's ``DirectoryNamespace``:
``create_table(mode="bogus")`` raises ``InvalidInputError: Unsupported create_table mode 'bogus'.
Supported modes are: 'Create', 'ExistOk', 'Overwrite'``, and ``insert_into_table`` refuses a mode
outside ``append``/``overwrite`` with the same class. The same backend ignores ``mode`` and ``behavior``
on ``create_namespace`` and ``drop_namespace``, so on those doors this module is the only parser.

A member's value is its snake_case spelling; its PascalCase spelling is the same words capitalised and
joined. Both are matched case-insensitively and nothing else is, so ``EXIST_OK`` and ``existok`` parse
and ``exis_tok`` does not.
"""

from __future__ import annotations

from enum import StrEnum

from lance_namespace import InvalidInputError


def _pascal(member: StrEnum) -> str:
    """The spec's PascalCase spelling of ``member`` (``exist_ok`` -> ``ExistOk``)."""
    return "".join(word.capitalize() for word in member.value.split("_"))


def _parse_closed[E: StrEnum](vocabulary: type[E], raw: str | None, *, default: E, field: str) -> E:
    """Parse ``raw`` against ``vocabulary``: absent or blank is ``default``, a spelling of a member is that
    member, and anything else is refused.

    Raises:
        InvalidInputError: ``raw`` is not a spelling of any member. The message names ``field``, the
            value as sent, and every valid value in the spec's PascalCase.
    """
    if raw is None or raw == "":
        return default
    folded = raw.lower()
    for member in vocabulary:
        if folded in (member.value, member.value.replace("_", "")):
            return member
    valid = ", ".join(f"'{_pascal(member)}'" for member in vocabulary)
    raise InvalidInputError(f"unrecognised {field} {raw!r}: valid values are {valid} (case insensitive, PascalCase or snake_case)")


class CreateMode(StrEnum):
    """How ``POST /v1/table/{id}/create`` and ``POST /v1/namespace/{id}/create`` treat an id already in use.

    ``CREATE`` conflicts, ``OVERWRITE`` drops and re-creates (spec: "the existing table is dropped and a
    new table with this name is created"), ``EXIST_OK`` keeps the existing object untouched.
    """

    CREATE = "create"
    OVERWRITE = "overwrite"
    EXIST_OK = "exist_ok"

    @classmethod
    def parse(cls, raw: str | None) -> CreateMode:
        """Normalise a wire ``mode``. Absent or blank → :attr:`CREATE`, the spec's default.

        Idempotent, so a caller that already holds a :class:`CreateMode` may pass it straight back in.

        Raises:
            InvalidInputError: ``raw`` is none of Create, ExistOk, Overwrite.
        """
        return _parse_closed(cls, raw, default=cls.CREATE, field="create mode")


class RegisterMode(StrEnum):
    """How ``POST /v1/table/{id}/register`` treats an id that is already registered.

    The spec gives this door TWO modes — "Create (default): the operation fails with 409. Overwrite: the
    existing table registration is replaced with the new registration." — and no ``ExistOk``. Parsing it
    with :class:`CreateMode` would admit a third value this door has no meaning for, so it is its own set.
    """

    CREATE = "create"
    OVERWRITE = "overwrite"

    @classmethod
    def parse(cls, raw: str | None) -> RegisterMode:
        """Normalise a wire ``mode``. Absent or blank → :attr:`CREATE`, the spec's default.

        Raises:
            InvalidInputError: ``raw`` is neither Create nor Overwrite — ``ExistOk`` included.
        """
        return _parse_closed(cls, raw, default=cls.CREATE, field="register mode")


class InsertMode(StrEnum):
    """What ``POST /v1/table/{id}/insert`` does with the rows already in the table.

    ``APPEND`` (the spec's default) keeps them; ``OVERWRITE`` removes them and then inserts. The values
    are pylance's own write-mode spellings for the same two operations, so a parsed member is passed to
    either arm of the door as it is.
    """

    APPEND = "append"
    OVERWRITE = "overwrite"

    @classmethod
    def parse(cls, raw: str | None) -> InsertMode:
        """Normalise a wire ``mode``. Absent or blank → :attr:`APPEND`, the spec's default.

        Raises:
            InvalidInputError: ``raw`` is neither Append nor Overwrite — ``create`` included, which is in
                pylance's write-mode vocabulary but not in the spec's for this door.
        """
        return _parse_closed(cls, raw, default=cls.APPEND, field="insert mode")


class DropMode(StrEnum):
    """What ``drop_namespace`` does when the namespace to drop IS NOT THERE.

    Orthogonal to :class:`DropBehavior`, which answers what to do about the CONTENTS — the spec's
    request carries both and they decide different things. From the generated model: "Fail (default):
    the server must return 400 indicating the namespace to drop does not exist. Skip: the server must
    return 204 indicating the drop operation has succeeded."

    ``SKIP`` IS THE IDEMPOTENCY LEVER, which is why it is worth its own vocabulary rather than a
    boolean: it is how a client makes a drop safe to retry, so a namespace a previous attempt already
    removed counts as success instead of failing the retry.

    Not a :class:`CreateMode`: Fail/Skip is a different set, and neither of its words is a spelling of a
    create mode.
    """

    FAIL = "fail"
    SKIP = "skip"

    @classmethod
    def parse(cls, raw: str | None) -> DropMode:
        """Normalise a wire ``mode``. Absent or blank → :attr:`FAIL`, the spec's default.

        Raises:
            InvalidInputError: ``raw`` is neither Fail nor Skip.
        """
        return _parse_closed(cls, raw, default=cls.FAIL, field="drop mode")


class DropBehavior(StrEnum):
    """What ``drop_namespace`` does about the namespace's contents.

    ``RESTRICT`` (the spec's default) refuses a non-empty namespace; ``CASCADE`` takes the subtree with
    it (recoverably, when a trash grace period is configured).
    """

    RESTRICT = "restrict"
    CASCADE = "cascade"

    @classmethod
    def parse(cls, raw: str | None) -> DropBehavior:
        """Normalise a wire ``behavior``. Absent or blank → :attr:`RESTRICT`, the spec's default.

        Raises:
            InvalidInputError: ``raw`` is neither Restrict nor Cascade.
        """
        return _parse_closed(cls, raw, default=cls.RESTRICT, field="drop behavior")
