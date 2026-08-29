"""Which Lance failure is the CALLER'S fault, and which is the estate's (VS-06).

Every retrieval site in this package used to catch bare ``Exception`` and re-raise
:class:`~service_kit.exceptions.ValidationError`, which the problem+json handler renders as HTTP
400. So an unreachable object store, an expired credential and a corrupt manifest were all reported
as "you sent a bad request": the caller is told to fix input that was fine, the operator is told
nothing is wrong on the server, and the failure never enters a 5xx-based SLO or alert.

The split is MEASURED against the pinned lancedb/pylance, not guessed. Every caller-input failure
the query path can produce — a predicate naming a missing column, a SQL parse error, a literal that
cannot be cast to the column's type, a wrong-dimension query vector, a ``select`` of a missing
column — arrives as ``RuntimeError`` (lancedb) or ``ValueError`` (pylance) whose message contains
``Invalid user input``. An outage does not. So the marker phrase is the whole classifier, and
:func:`is_caller_input_error` is deliberately the only thing in this module: a wider net (catching
``ValueError``, or matching "not found") would put the estate's own faults back behind a 400.

The same message-classification shape `target.py` already uses for its open-table split, in one
place so the six call sites cannot drift apart.
"""

from __future__ import annotations


#: Lance/DataFusion's own prefix for "the query you handed me is malformed". Lowercased at the
#: comparison, because the two bindings differ in how they wrap it.
_CALLER_INPUT_MARKER = "invalid user input"


def is_caller_input_error(exc: BaseException) -> bool:
    """True when ``exc`` says the REQUEST was malformed, rather than that the estate is broken.

    A False answer means the exception must propagate: the global handler turns it into the 500
    that names an outage as an outage.
    """
    return _CALLER_INPUT_MARKER in str(exc).lower()
