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

from maintenance.services.optimize import classify_maintain_failure


def test_an_invalid_ref_name_is_classified_as_a_refusal() -> None:
    """THE DEFECT: reported as an error, so the sweep carries failures nothing can clear."""
    gate = classify_maintain_failure("Ref is invalid: Branch segment 'has space' contains invalid characters. Only alphanumeric, '.', '-', '_' are allowed.")

    assert gate == "invalid_ref"
