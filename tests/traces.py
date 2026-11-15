"""A W3C trace per name, for a test that sends several requests and reads which records each one caused (ADR 0209).

The trace a request runs under is the one id its log lines, audit records, commits and events carry, so a test that
tells requests apart sends each one under a trace of its own and filters the records by that trace's id.
"""

from __future__ import annotations

import hashlib


def trace_named(name: str) -> str:
    """The 32-hex trace id of the trace named ``name``: the same for the same name, and for no other."""
    return hashlib.sha256(name.encode()).hexdigest()[:32]


def trace_id_of(traceparent: str) -> str:
    """The 32-hex trace id a W3C ``traceparent`` names: its second field."""
    return traceparent.split("-")[1]


def traceparent_named(name: str) -> dict[str, str]:
    """The ``traceparent`` header that sends a request in the trace named ``name``."""
    return {"traceparent": f"00-{trace_named(name)}-00f067aa0ba902b7-01"}
