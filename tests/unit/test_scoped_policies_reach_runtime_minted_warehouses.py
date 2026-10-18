"""The scoped RustFS users reach the buckets their consumers open, and nothing else.

THE BUCKET SET IS NOT KNOWABLE AT RENDER TIME, which is why neither policy enumerates one.
`catalog.warehouses.enabled` (on by default) provisions one physically separate bucket per warehouse at
`POST /management/v1/warehouses`, with an operator-chosen name, so no value in the chart can name it. The sweep's
credential opens those buckets: `sweep.py::_buckets_to_sweep` extends its configured list from the warehouse
registry itself.

So the policies ALLOW the data planes on every bucket and DENY the control plane precisely — every
`*_PREFIX` under a control root plus the `__manifest` namespace index — and the control bucket keeps
its tight listing through `StringNotLike` on `s3:prefix` rather than through its name.

MEASURED on the live estate 2026-09-04/05, which is what these tests encode. A cascade re-run for
project `acme` submitted a Ray job that died `AccessDenied` on
``GET /acme-bucket?list-type=2&prefix=medallion/_versions/``, because the policy reached warehouses
only through a `*-wh` name pattern and `acme-bucket` does not match it. `StringNotLike` was probed
against this estate's RustFS (1.0.0-beta.8) before the policy was written to depend on it: with a
`Deny s3:ListBucket` conditioned `StringNotLike s3:prefix medallion*`, listing `lance-catalog/medallion/`
was ALLOWED while `lance-catalog/` and `lance-catalog/_projects/` were DENIED and other buckets listed
freely. After the fix, from the Ray credential itself, `PUT` to all nine control prefixes answered
DENIED and the three warehouse data paths answered ALLOWED.

The evaluator below is IAM-shaped (explicit Deny wins, otherwise an Allow must match) so each test
states the OUTCOME a credential gets. A string grep cannot: it can only check the buckets the chart
names, and a registry-discovered one is by definition not among them.
"""

from __future__ import annotations

import json
import textwrap
from fnmatch import fnmatchcase

import pytest

from tests.unit.test_invariants import _helm_template


#: A bucket name no static value in the render can possibly contain — the shape `POST /management/v1/warehouses`
#: mints. Deliberately NOT `*-wh`: that suffix is a convention, not a rule, and the live estate's
#: `acme-bucket` is the counterexample that broke the cascade.
RUNTIME_BUCKET = "acme-bucket"
CONTROL_BUCKET = "lance-catalog"

_BUCKET_ACTIONS = frozenset({"s3:ListBucket", "s3:GetBucketLocation", "s3:ListAllMyBuckets"})


def _render() -> str:
    return _helm_template(
        "objectStore.maintenanceAccessKey=rask-maintenance",
    )


def _policy(rendered: str, name: str) -> dict:
    """The policy document the Job writes for `name`, parsed.

    Read out of the heredoc rather than out of a values file on purpose: what governs the credential
    is the JSON the hook hands the store's `add-canned-policy` call, and a template that renders valid YAML around
    invalid JSON is exactly the failure this parse catches.
    """
    job = rendered[rendered.index("component: object-store-scoped-users") :]
    start = job.index(f"cat >/tmp/policies/rask-{name}.json <<'POLICY'")
    body = job[start:]
    body = body[body.index("\n") + 1 :]
    body = body[: body.index("POLICY\n")]
    return json.loads(textwrap.dedent(body))


def _matches(pattern: str, value: str) -> bool:
    return fnmatchcase(value, pattern)


def _condition_holds(condition: dict, *, prefix: str | None) -> bool:
    """Only the operators these policies use. An UNKNOWN operator raises rather than passing.

    A condition this evaluator silently ignored would make a Deny look narrower (or an Allow wider)
    than RustFS treats it, which is the one way a policy test can be worse than no test at all.
    """
    for operator, clauses in condition.items():
        for key, patterns in clauses.items():
            if key != "s3:prefix":
                raise AssertionError(f"unhandled condition key {key!r} — teach the evaluator before relying on it")
            wanted = [patterns] if isinstance(patterns, str) else list(patterns)
            # An ABSENT key resolves differently per direction, and getting it backwards makes a Deny
            # look narrower than it is: a positive operator fails vacuously, a NEGATED one holds
            # vacuously. RustFS agrees in the direction that matters — the live probe listed the
            # control bucket's root under exactly this policy and got AccessDenied, which is the
            # `StringNotLike` Deny applying to a listing that carries no useful prefix.
            hit = prefix is not None and any(_matches(p, prefix) for p in wanted)
            if operator == "StringLike" and not hit:
                return False
            if operator == "StringNotLike" and hit:
                return False
            if operator not in {"StringLike", "StringNotLike"}:
                raise AssertionError(f"unhandled condition operator {operator!r}")
    return True


def _statement_applies(stmt: dict, *, action: str, arn: str, prefix: str | None) -> bool:
    actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
    if not any(_matches(a, action) for a in actions):
        return False
    resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
    if not any(_matches(r, arn) for r in resources):
        return False
    return _condition_holds(stmt.get("Condition") or {}, prefix=prefix)


def allowed(policy: dict, *, action: str, bucket: str, key: str = "", prefix: str | None = None) -> bool:
    """IAM evaluation, cut to what these two policies use: explicit Deny wins, else an Allow must match."""
    arn = f"arn:aws:s3:::{bucket}" if action in _BUCKET_ACTIONS else f"arn:aws:s3:::{bucket}/{key}"
    statements = policy["Statement"]
    if any(s["Effect"] == "Deny" and _statement_applies(s, action=action, arn=arn, prefix=prefix) for s in statements):
        return False
    return any(s["Effect"] == "Allow" and _statement_applies(s, action=action, arn=arn, prefix=prefix) for s in statements)


@pytest.fixture(scope="module")
def maintenance() -> dict:
    return _policy(_render(), "maintenance")


# ---- the maintenance sweep ----------------------------------------------------------------------


def test_the_sweep_still_reads_the_records_it_must_read(maintenance: dict) -> None:
    """READ, not write. It resolves the buckets to sweep out of `_warehouses/` and the per-object
    protection verdict out of `_protection/`; denying the read would blind the pre-pass rather than
    constrain it."""
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key="_warehouses/acme-bucket.json")
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key="_protection/table.json")
    assert allowed(maintenance, action="s3:ListBucket", bucket=CONTROL_BUCKET, prefix="_warehouses/")
    # [[LH-279]] The base records: read by the pre-pass, removed by the purge with the table it destroys, and
    # never written — a maintainer that could write one could sanction any base it liked.
    record = "_bases/table-0123456789abcdef01234567.json"
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key=record)
    assert allowed(maintenance, action="s3:DeleteObject", bucket=CONTROL_BUCKET, key=record)
    assert not allowed(maintenance, action="s3:PutObject", bucket=CONTROL_BUCKET, key=record)
