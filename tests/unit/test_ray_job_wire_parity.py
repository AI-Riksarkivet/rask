"""`RayJob` is declared twice, in two languages, and the two disagreed while both suites stayed green.

`packages/ray-kit/src/ray_kit/schemas.py::RayJob` is what the compute service SENDS;
`frontend/packages/api/src/ray.ts::RayJobSchema` is what the SPA parses. Nothing tied them together,
so they drifted into asserting opposite things about the same field and each side's tests passed:

* `ray.test.ts` — "still carries metadata when the submitter set it — the medallion path" — asserts
  `metadata['rask.originator'] === 'alice'` survives the wire.
* `test_dashboard_bounds.py` — "bulky ray fields are dropped not retained" — asserts
  `"metadata" not in kept`.

Both are green. Only one can describe the running estate, and it is the Python one: `RayJob` is
`extra="ignore"` and does not declare `metadata`, so the field the SPA parses is one the service never
sends and can only ever be its `{}` default.

WHICH SIDE IS RIGHT, because the answer is not "make them the same". Stripping is correct and
security-motivated: Ray's `JobDetails` carries `runtime_env` (the job's full env, which on this estate
includes `S3_SECRET` and the lineage service token) and an arbitrary user `metadata` dict — and the
medallion's own submitter puts **`rask.token`** in that dict (`ray_submit.py:166`). Retaining it whole
would put a token into every jobs-board row. So `metadata` is TS-only ON PURPOSE, and this file records
that as a declared asymmetry with its reason rather than letting it read as drift.

What the SPA's own test should NOT claim is that the medallion path delivers it, because through
`/api/ray/jobs` it cannot. `GET /api/jobs/<id>` — the endpoint `rask-notifications` names for
recovering who a dead job was for — is **Ray's own dashboard API**, not a rask route: the compute
service serves `/jobs` and `/jobs/{id}/logs` and nothing else.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
_PY_MODEL = REPO_ROOT / "packages/ray-kit/src/ray_kit/schemas.py"
_TS_SCHEMA = REPO_ROOT / "frontend/packages/api/src/ray.ts"

#: Fields the TS schema declares that the Python model deliberately does not send, with the reason.
#: An entry here is a claim someone has to justify — the same contract as the medallion's emit
#: exemptions.
_TS_ONLY = {
    "metadata": (
        "stripped by RayJob's `extra=\"ignore\"` on purpose. Ray's JobDetails carries an arbitrary "
        "user dict, and the medallion's own submitter puts `rask.token` in it (ray_submit.py:166), so "
        "retaining it would put a token into every jobs-board row. The SPA declares it OPTIONAL with a "
        "`{}` default so the same parser can also read Ray's dashboard directly, where it IS present."
    ),
}


def _python_fields() -> set[str]:
    """The annotated fields of `class RayJob`."""
    tree = ast.parse(_PY_MODEL.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RayJob":
            return {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id != "model_config"
            }
    raise AssertionError("class RayJob not found — the model moved and this gate is vacuous")


def _typescript_fields() -> set[str]:
    """The keys of `RayJobSchema`, read from the object literal rather than from a hand-kept list."""
    text = _TS_SCHEMA.read_text(encoding="utf-8")
    match = re.search(r"RayJobSchema\s*=\s*v\.object\(\{(.*?)\n\}\)", text, re.DOTALL)
    assert match, "RayJobSchema's object literal not found — the schema moved and this gate is vacuous"
    body = "\n".join(line for line in match.group(1).splitlines() if not line.strip().startswith("//"))
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.MULTILINE))


def test_the_two_declarations_have_not_drifted() -> None:
    """The gate the estate did not have. Two languages, one wire contract, no link between them."""
    python, typescript = _python_fields(), _typescript_fields()

    assert python, "no fields parsed from RayJob"
    assert typescript, "no fields parsed from RayJobSchema"

    missing_in_ts = python - typescript
    assert not missing_in_ts, (
        f"the compute service sends fields the SPA does not parse: {sorted(missing_in_ts)}. valibot "
        "objects ignore unknown keys, so these arrive and are silently discarded — the field exists on "
        "the wire and reaches no surface."
    )

    extra_in_ts = typescript - python - set(_TS_ONLY)
    assert not extra_in_ts, (
        f"the SPA parses fields the service never sends: {sorted(extra_in_ts)}. Every one is dead "
        "shape at best; if it is required rather than optional it takes the WHOLE payload down, "
        "because the response is parsed as one document. Declare it on RayJob, or record it in "
        "_TS_ONLY with the reason it is deliberately not sent."
    )


def test_every_ts_only_field_is_still_absent_from_the_python_model() -> None:
    """A declared asymmetry that stops being asymmetric must be deleted, not left as folklore.

    If someone later adds `metadata` to `RayJob` — filtered to the safe `rask.*` keys, say — this entry
    becomes a false explanation of a field that IS sent, and the next reader would trust it.
    """
    python = _python_fields()
    stale = sorted(name for name in _TS_ONLY if name in python)
    assert not stale, (
        f"these are recorded as deliberately-not-sent but RayJob now declares them: {stale}. Delete "
        "the _TS_ONLY entry — the asymmetry it explains no longer exists."
    )
