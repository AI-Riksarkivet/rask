"""A maintenance 403 must not instruct an operator to grant on an object that is not a table.

[[LH-164]]. The catalog's authorization gate runs BEFORE existence resolution, so "this identity holds
no `can_maintain`" and "no such table is registered" are the SAME 403 on the wire. The sweep cannot
tell them apart — and for three revisions it wrote as though it could, ending every refusal with
*"Grant can_maintain on table:<id> if it should be."*

MEASURED 2026-09-15 on the live estate, which is what makes this a defect and not a wording quibble:
a sweep pass refused five datasets that way, and `lakehouse$silver` — the id in the message — is a
NAMESPACE. `discover_datasets` finds datasets by BUCKET SCAN and derives an id from the path, so a
dataset that was never registered yields an id no table door can act on. The tier the cascade actually
governs is `lakehouse$silver$features`, and it maintains fine (`can_maintain` = True live).

WHY THE WORDING IS LOAD-BEARING HERE. The same pass carried 315 refusals that were CORRECT — the
shallow-clone protection doing its job. An instruction nobody can carry out, repeated beside them, is
exactly how a reader learns to skip the category, and this category is the one that refuses to sign a
rewrite with the deployment's ambient key.
"""

from __future__ import annotations

from maintenance.services.compaction_executor import denial_remedy


def test_it_names_the_authorization_cause() -> None:
    remedy = denial_remedy(table_id="db1$users", identity="service-maintenance")

    assert "can_maintain" in remedy
    assert "service-maintenance" in remedy


def test_it_ALSO_names_the_cause_the_sweep_cannot_rule_out() -> None:
    """The half that was missing: the id may not be a table at all."""
    remedy = denial_remedy(table_id="lakehouse$silver", identity="service-maintenance")

    assert "ungoverned" in remedy, f"the refusal still assumes the id is a registered table: {remedy}"
    assert "no such table is registered" in remedy


def test_it_says_WHY_the_two_are_indistinguishable() -> None:
    """Without this the reader has no way to know the message is not simply vague.

    The gate ordering is the reason, and it is the one fact that turns "check both" from hedging into
    an instruction: an operator who knows the 403 precedes existence resolution knows to verify the id
    names a table BEFORE reaching for a grant.
    """
    remedy = denial_remedy(table_id="db1$users", identity="service-maintenance")

    assert "before existence resolution" in remedy


def test_it_does_not_instruct_a_bare_grant() -> None:
    """THE DEFECT ITSELF: an imperative that asserts the object is a table.

    `Grant can_maintain on table:<id>` reads as a step to take. It is only a step when the id names a
    table, and the sweep does not know that — so the refusal asks the reader to check first.
    """
    remedy = denial_remedy(table_id="lakehouse$silver", identity="service-maintenance")

    assert "Grant can_maintain on table:lakehouse$silver" not in remedy
    assert "Check that the id names a TABLE" in remedy


def test_both_refusal_sites_use_the_one_wording() -> None:
    """Two call sites phrased this independently and drifted; one function is what stops that again."""
    import inspect
    import linecache

    from maintenance.services import catalog_compaction, credentials

    for module in (catalog_compaction, credentials):
        linecache.checkcache(module.__file__)
        source = inspect.getsource(module)
        assert "denial_remedy(" in source, f"{module.__name__} phrases its own maintenance refusal again"
        assert "Grant can_maintain on table:" not in source, f"{module.__name__} still instructs a bare grant"
