"""The LANCE-ONLY product invariant, at one choke point.

STANDING RULING (owner, 2026-08-15): rask stores Lance tables and no other format, ever. That makes
this a PRODUCT invariant rather than an implementation detail of any one door, which is why it lives
in `core/` and not beside the door it was first written for.

It began as a module-private helper inside `api/v1/endpoints/data.py` with exactly one caller, the
create door. `declare_table`, `register_table`, `create_namespace` and `update_table` all take the
same `properties` map through their spec request models and none of them checked it — so a client
could select a non-Lance format through four of the five doors that accept one. Moving it here is
what lets every door call the same rule instead of re-deriving it or forgetting to.
"""

from __future__ import annotations

from lance_namespace import InvalidInputError


#: Format-selecting properties an Iceberg / Unity-Catalog client might send, expecting to choose a file
#: format. This catalog stores Lance ONLY (columnar, self-describing, versioned), so honouring them is
#: impossible — echoing them back would let the client believe it got a format it did not.
FORMAT_KEYS = ("write.format.default", "data_source_format")


def reject_unsupported_format(properties: object) -> None:
    """Raise 400 if ``properties`` request a non-Lance file format — never a silent no-op.

    Tolerant of a non-mapping (``None``, an unparsed string) on purpose: the doors differ in whether
    they hand over a parsed dict or a raw body field, and a guard that raised on shape would turn a
    format check into a schema check.
    """
    if not isinstance(properties, dict):
        return
    for key in FORMAT_KEYS:
        requested = properties.get(key)
        if requested is not None and str(requested).lower() != "lance":
            raise InvalidInputError(
                f"file format {requested!r} ({key}) is not supported — this catalog stores Lance only; format-selecting properties are not silently ignored"
            )
