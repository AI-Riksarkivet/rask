"""A delete that partly landed must tell the CALLER, not only the log (LH-030).

The endpoint already recorded the partial outcome — `warehouse_delete_partial`, with the namespaces it
had dropped — and then re-raised, so the caller received a problem body that said nothing about it. A
delete that did nothing and a delete that destroyed three namespaces before failing were byte-identical
on the wire, and they need different next actions: one is "retry or give up", the other is "those are
already gone; finish it".

WHY AN EXTENSION MEMBER RATHER THAN `detail`: a 5xx detail is deliberately redacted
(`ns_errors.problem_detail`) because it can carry paths, DSNs and driver text. The outcome here is
assembled by the endpoint from what it already knows about the caller's own objects, so it leaks
nothing — RFC 9457 §3.2 extension members are exactly the seam for that.

These pin the carrier. The door's own report, from a failure forced mid-cascade on real pylance, is pinned by
`test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py`.
"""

from __future__ import annotations

from service_kit.lakehouse.ns_errors import PartiallyApplied, problem_detail


def test_an_extension_member_cannot_redefine_a_reserved_field() -> None:
    """Otherwise a raising endpoint could quietly rewrite `status` or un-redact `detail`."""
    status, body = problem_detail(PartiallyApplied("half done", problem_extra={"status": 200, "detail": "leaked internals", "extra": "kept"}))

    assert body["status"] == status, "an extension member overwrote the status"
    assert body["detail"] != "leaked internals", "an extension member defeated the 5xx redaction"
    assert body["extra"] == "kept", "a non-reserved extension member was dropped"
