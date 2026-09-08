"""Who read which table, at which version, with which columns — the read audit log (§ J1).

A lakehouse buyer expects to answer "who looked at this data". rask could not: `audit()` exists and is
used on the GRANT and authz paths, but the catalog's data doors emit none, so a read left no record.
Measured 2026-09-08: zero `audit()` calls in `endpoints/data.py` and `endpoints/tables.py`.

THIS IS ALSO A ZERO-TRUST GAP, not only a feature gap. The estate authorizes every read — the door
FGA-checks and refuses — and then forgets it happened, so "was this table ever read by that subject"
has no answer even though the decision to allow it was made deliberately.

NOT A MIDDLEWARE, which is what the row first proposed. A middleware sees the path and the principal
and nothing else: `version` and `columns` are the fields that make a read log worth keeping, and only
the route knows them. So the doors call one helper, and the helper fixes the ACTION NAME and the field
set — which is what makes the resulting log queryable rather than a pile of differently-shaped lines.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import cast

from catalog.api.v1.endpoints import data as data_endpoints


#: Every door in `data.py` that returns rows or bytes to a caller. Named rather than derived, because a
#: new read door must be a deliberate addition here — the failure of a derived list is silence.
READ_DOORS = ("query_table", "count_table_rows", "explain_table_query_plan", "read_table_blob")


def _door(name: str) -> Callable[..., object]:
    fn = getattr(data_endpoints, name, None)
    assert fn is not None, f"{name} is gone from data.py — this pin names a door that no longer exists"
    return cast("Callable[..., object]", fn)


def test_every_data_read_door_records_who_read_it() -> None:
    """The headline. A door that returns rows without an audit line leaves no answer to "who read this"."""
    unaudited = [name for name in READ_DOORS if "audit_read" not in inspect.getsource(_door(name))]
    assert not unaudited, f"these doors return data and record nothing: {unaudited}"


def test_the_read_audit_names_a_SUBJECT() -> None:
    """A read log without the reader is a row count. Every door must take the token the subject rides."""
    subjectless = [name for name in READ_DOORS if "CurrentToken" not in str(inspect.signature(_door(name)))]
    assert not subjectless, f"these doors cannot name who read: {subjectless}"


def test_the_helper_fixes_one_action_name() -> None:
    """One action string, or the log cannot be filtered. Four doors inventing four verbs is the same as
    no log at all for anyone trying to answer a question with it."""
    from service_kit.governed.audit import audit_read

    # The constant must be the one the helper actually EMITS, not merely defined beside it: a name
    # nothing uses is the shape where the log is filterable in the docs and not in the sink.
    assert "READ_ACTION" in inspect.getsource(audit_read), "audit_read does not emit READ_ACTION, so the fixed name is decoration"
    assert "action" not in inspect.signature(audit_read).parameters, "audit_read takes the action from its caller, so four doors can spell it four ways"
