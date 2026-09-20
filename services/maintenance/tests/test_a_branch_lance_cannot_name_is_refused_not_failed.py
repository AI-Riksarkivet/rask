"""A ref whose NAME Lance will never accept is a refusal, not a per-tick failure.

Measured against the live estate 2026-09-20 by running `scripts/e2e_live.sh
tests/e2e-py/test_maintenance_e2e.py`: eleven datasets report
`maintain: Ref is invalid: …` every sweep, across three parents —

    tree/has space   Branch segment 'has space' contains invalid characters
    tree/tilde~name  Branch segment 'tilde~name' contains invalid characters
    tree/a\\b         Branch name cannot contain '..' or '\\'
    tree/feat.lock   Branch name cannot end with '.lock'
    tree/trailing    Branch name cannot start or end with a '/'

WHERE THEY COME FROM. `discover_datasets` descends into `<dataset>/tree/` and treats each child
directory as a dataset URI, so a branch is swept like any other dataset. These particular names are
residue from the suite that pins the door's refusal of malformed branch names — the door is fixed, the
directories it created before that are still on disk. Same shape as the estate's other residues: the
producer is closed and the leftovers remain.

WHY IT MUST NOT BE AN `error`. `DatasetResult.refused` already exists precisely for this distinction,
and its own comment draws it: a skip is "not this tick", an error is "something failed", and a refusal
is "we declined, before touching a byte". A name Lance cannot parse can NEVER be maintained — no
retry, no upgrade, no operator action on this pass changes it. Reporting it as an error makes every
sweep carry eleven failures that nothing can clear, and it turns the live maintenance suite
permanently red, which costs the estate the signal that suite exists to give.

IT IS ITS OWN GATE, not folded into `manifest_flags`. The existing two are a LAYOUT fact (a feature
this pass cannot rewrite) and someone ELSE'S clone; this is a NAME the ref should never have had.
`summarize_refusals`' breakdown is only actionable while the gates stay distinct — `manifest_flags` is
a pylance upgrade away from clearing, `protected_base` stays true forever, and this one clears when
somebody deletes a directory.
"""

from __future__ import annotations

from maintenance.services.optimize import DatasetResult, classify_maintain_failure


def test_an_invalid_ref_name_is_classified_as_a_refusal() -> None:
    """THE DEFECT: reported as an error, so the sweep carries failures nothing can clear."""
    gate = classify_maintain_failure("Ref is invalid: Branch segment 'has space' contains invalid characters. Only alphanumeric, '.', '-', '_' are allowed.")

    assert gate == "invalid_ref"


def test_every_shape_the_live_estate_produces_is_recognised() -> None:
    """All five messages measured on the deployed estate. One regex that matches only the first would
    leave the rest failing forever while looking fixed."""
    for message in (
        "Ref is invalid: Branch segment 'tilde~name' contains invalid characters. Only alphanumeric, '.', '-', '_' are allowed.",
        "Ref is invalid: Branch name cannot contain '..' or '\\'",
        "Ref is invalid: Branch name cannot contain consecutive '/'",
        "Ref is invalid: Branch name cannot start or end with a '/'",
        "Ref is invalid: Branch name cannot end with '.lock'",
    ):
        assert classify_maintain_failure(message) == "invalid_ref", message


def test_a_REAL_failure_is_still_a_failure() -> None:
    """The line this must not cross. Classifying too broadly would silence genuine faults as
    'declined', which is the one thing worse than reporting them every tick."""
    for message in (
        "the object store went away mid-scan",
        "Generic S3 error: Error after 10 retries",
        "index_stats panicked",
        "Ref not found: dev",
    ):
        assert classify_maintain_failure(message) is None, message


def test_the_refusal_keeps_its_own_gate_name() -> None:
    """`summarize_refusals` is only actionable while the gates stay distinct: `manifest_flags` clears
    on a pylance upgrade, `protected_base` never clears, and this one clears when a directory is
    removed. Merging them produces a count nobody can act on."""
    from maintenance.services.optimize import summarize_refusals

    results = [
        DatasetResult(uri="s3://b/d/tree/has space", refused="Ref is invalid", refused_by="invalid_ref"),
        DatasetResult(uri="s3://b/e", refused="flag 16", refused_by="manifest_flags"),
    ]

    assert summarize_refusals(results) == {"invalid_ref": 1, "manifest_flags": 1}


def test_the_HANDLER_passes_the_classifier_s_gate_through() -> None:
    """The suite above proves the classifier answers correctly and that `summarize_refusals` counts
    distinct gates. Neither proves the failure handler uses the answer.

    Measured: replacing `result.refused_by = gate` with the literal `"manifest_flags"` left all 321
    tests green — every refusal would have been filed under a gate that clears on a pylance upgrade,
    so an operator would wait for an upgrade to remove a stale directory. The rule was tested and the
    wiring was not.

    Asserted on the source because reaching that `except` for real needs a Lance dataset with a
    malformed branch directory, which is a fixture this suite would have to create through a door the
    catalog now (correctly) refuses.
    """
    import ast
    import inspect

    from maintenance.services import optimize

    tree = ast.parse(inspect.getsource(optimize))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "refused_by" for t in node.targets)
        and isinstance(node.value, ast.Name)
    ]

    assert assignments, (
        "no `result.refused_by = <name>` assignment — every refusal gate is written as a literal, so `classify_maintain_failure`'s answer reaches nothing"
    )
