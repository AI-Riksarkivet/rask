"""The scoped RustFS users reach the buckets their consumers open, and nothing else.

THE BUCKET SET IS NOT KNOWABLE AT RENDER TIME, which is why neither policy enumerates one.
`catalog.warehouses.enabled` (on by default) provisions one physically separate bucket per warehouse at
`POST /v1/warehouses`, with an operator-chosen name, so no value in the chart can name it. Both
credentials open those buckets: the Ray stage/train jobs read `FROM_URI` and write `TO_URI`, which
`_resolve_roots` sets to ``<the project's warehouse root>/medallion/<tier>`` for any tenant trigger,
and `sweep.py::_buckets_to_sweep` extends its configured list from the warehouse registry itself.

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
import re
import textwrap
from fnmatch import fnmatchcase

import pytest

from tests.unit.test_invariants import _helm_template


#: A bucket name no static value in the render can possibly contain — the shape `POST /v1/warehouses`
#: mints. Deliberately NOT `*-wh`: that suffix is a convention, not a rule, and the live estate's
#: `acme-bucket` is the counterexample that broke the cascade.
RUNTIME_BUCKET = "acme-bucket"
CONTROL_BUCKET = "lance-catalog"

_BUCKET_ACTIONS = frozenset({"s3:ListBucket", "s3:GetBucketLocation", "s3:ListAllMyBuckets"})

#: EVERY control-plane prefix under a control root, read off the constants that define them rather than
#: retyped: `_protection`, `_gates`, `_trash`, `_transforms`, `_warehouses`, `_tasks`, `_policies` (the
#: `*_PREFIX` finals in `service_kit/lakehouse/`) plus `_projects` (`catalog/services/projects.py`).
#: The first policy pass denied four of the eight and the omissions were not equivalent: `_tasks/<hash>.json`
#: names an ENGINE AND A COMMAND (`task_registry.py`), so a credential that writes one changes what the
#: compute plane executes; `_transforms/` and `_gates/` steer which transform runs and which quality gate
#: admits it; and deleting a `_trash/` record makes `undrop` unreachable for bytes that still exist.
CONTROL_PREFIXES = ("_trash",)


def _render() -> str:
    return _helm_template(
        "minio.maintenanceAccessKey=rask-maintenance",
        "minio.rayComputeAccessKey=rask-ray-compute",
        "minio.medallionAccessKey=rask-medallion",
    )


def _policy(rendered: str, name: str) -> dict:
    """The policy document the Job writes for `name`, parsed.

    Read out of the heredoc rather than out of a values file on purpose: what governs the credential
    is the JSON `mc admin policy create` is handed, and a template that renders valid YAML around
    invalid JSON is exactly the failure this parse catches.
    """
    job = rendered[rendered.index("component: minio-scoped-users") :]
    start = job.index(f"cat >/tmp/{name}.json <<'POLICY'")
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
def ray() -> dict:
    return _policy(_render(), "ray-compute")


@pytest.fixture(scope="module")
def maintenance() -> dict:
    return _policy(_render(), "maintenance")


# ---- the Ray compute lane -----------------------------------------------------------------------


@pytest.mark.parametrize("guarded", CONTROL_PREFIXES)
def test_the_ray_lane_cannot_touch_control_records_in_ANY_bucket(ray: dict, guarded: str) -> None:
    """Widening the allow to every bucket widens the deny with it, or a tenant warehouse's own control
    records become reachable — which the static policy never had to think about because it reached no
    tenant bucket at all."""
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        assert not allowed(ray, action="s3:GetObject", bucket=bucket, key=f"{guarded}/x.json"), f"{bucket}/{guarded} is readable"
        assert not allowed(ray, action="s3:PutObject", bucket=bucket, key=f"{guarded}/x.json"), f"{bucket}/{guarded} is writable"


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


# ---- the provisioning step itself ----------------------------------------------------------------


def test_a_malformed_policy_fails_the_hook_instead_of_leaving_the_old_one_attached() -> None:
    """`mc admin policy create` OVERWRITES an existing policy on this RustFS — probed 2026-09-04: a
    second create with different content returned exit 0 and the readback showed the new document. So
    the `|| true` that used to sit on the create was not protecting against "already exists"; all it
    could do was swallow a REAL failure (malformed JSON, admin denied) and let the hook report success
    while the credential kept its old policy. The user-add keeps its `|| true`, which genuinely does
    guard a re-run."""
    rendered = _render()
    job = rendered[rendered.index("component: minio-scoped-users") :]
    creates = re.findall(r"mc admin policy create rfs \S+ \S+( \|\| true)?", job)
    assert creates, "no policy is created at all"
    assert not any(creates), "a policy create still swallows its failure — a bad policy renders as a successful hook"


# ---- the medallion plane: the producer and the three stage runners --------------------------------------
#
# A THIRD SHAPE, and it had to be. Reusing `rask-ray-compute` here would have broken the cascade on
# contact: that policy denies the control prefixes TOTALLY, which is right for a Ray stage job (it
# reads FROM_URI, writes TO_URI and consults nothing) and wrong for the SERVICES that drive it. The
# medallion reaches those records directly over S3, not through the catalog door — measured
# 2026-09-07: `task_register.register_tasks` WRITES `_tasks/`, and `transform_spec.resolve_task`
# / `resolve_transform` / `gate_specs` / `project_root` READ `_tasks/`, `_transforms/`, `_gates/` and
# the warehouse registry. So the deny is write-only like maintenance's, with `_tasks/` carved out
# because this plane is that prefix's REGISTRAR rather than its consumer.
