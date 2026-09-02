"""The spec's `delimiter` query parameter, refused when it is not the one this server uses.

`delimiter` is a spec query parameter and the reference REST client sends it on EVERY request, taken
from its own configuration. rask declares it on no route and every handler splits the identifier with
the SERVER's `LANCE_NS_DELIMITER` (`$` by default) through `parse_identifier`. A client configured
with any other delimiter therefore has every multi-segment identifier reinterpreted, silently: with
`.`, `POST /v1/table/db.t/exists?delimiter=.` is looked up as the single top-level table `db.t` and
answers 404 TableNotFound — a real table reported absent, with nothing anywhere saying why.

REFUSING RATHER THAN HONOURING, and the reason is authorization. Honouring the client's delimiter
means threading it through `parse_identifier` AND `fga.canonical_object_id`, because the FGA object
id is derived from the same split; getting that wrong decides authorization against a
differently-spelled object, which is a worse failure than the one being fixed. A 400 naming the
server's delimiter turns a silent wrong answer into one the caller can act on today, and the full
form stays A4 in `open_lakehouse_diff_left.md`.

ROUTER-LEVEL, never per route: a per-route check is the one the next route added forgets, which is
how `delimiter` came to be declared on 0 of 153 operations in the first place.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from lance_namespace import InvalidInputError

from catalog.api.dependencies import SettingsDep


def require_the_servers_delimiter(
    settings: SettingsDep,
    delimiter: Annotated[
        str | None, Query(description="Identifier separator. Must match the server's, which is returned in the refusal when it does not.")
    ] = None,
) -> None:
    """Refuse a request whose `delimiter` this server does not use.

    An ABSENT delimiter is fine — most callers send none and get the server's. Only a stated
    disagreement is refused, because that is the case where the caller believes something about how
    their identifier will be split that is not true.
    """
    if delimiter is not None and delimiter != settings.delimiter:
        raise InvalidInputError(
            f"this catalog splits identifiers with {settings.delimiter!r}, not {delimiter!r}. "
            f"Send the identifier separated by {settings.delimiter!r}, or omit `delimiter`."
        )


DelimiterGuard = Depends(require_the_servers_delimiter)
