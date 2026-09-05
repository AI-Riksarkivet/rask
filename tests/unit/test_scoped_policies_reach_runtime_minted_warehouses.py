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
OBSERVABILITY_BUCKET = "rask-observability"

_BUCKET_ACTIONS = frozenset({"s3:ListBucket", "s3:GetBucketLocation", "s3:ListAllMyBuckets"})

#: EVERY control-plane prefix under a control root, read off the constants that define them rather than
#: retyped: `_protection`, `_gates`, `_trash`, `_transforms`, `_warehouses`, `_tasks`, `_policies` (the
#: `*_PREFIX` finals in `service_kit/lakehouse/`) plus `_projects` (`catalog/services/projects.py`).
#: The first policy pass denied four of the eight and the omissions were not equivalent: `_tasks/<hash>.json`
#: names an ENGINE AND A COMMAND (`task_registry.py`), so a credential that writes one changes what the
#: compute plane executes; `_transforms/` and `_gates/` steer which transform runs and which quality gate
#: admits it; and deleting a `_trash/` record makes `undrop` unreachable for bytes that still exist.
CONTROL_PREFIXES = ("_projects", "_protection", "_policies", "_warehouses", "_tasks", "_transforms", "_gates", "_trash")

#: The lance-ns NAMESPACE index — one per bucket, and the authoritative namespace->table mapping (a
#: namespace is a `__manifest` ROW, not a directory). Held apart from CONTROL_PREFIXES because the two
#: credentials need it differently: neither may ever WRITE it, and the purge must READ it to re-check a
#: trashed id's liveness before destroying bytes (`purge.py:257`).
MANIFEST_PREFIX = "__manifest"


def _render() -> str:
    return _helm_template(
        "rustfs.maintenanceAccessKey=rask-maintenance",
        "rustfs.maintenanceSecretKey=m-secret",
        "rustfs.rayComputeAccessKey=rask-ray-compute",
        "rustfs.rayComputeSecretKey=r-secret",
    )


def _policy(rendered: str, name: str) -> dict:
    """The policy document the Job writes for `name`, parsed.

    Read out of the heredoc rather than out of a values file on purpose: what governs the credential
    is the JSON `mc admin policy create` is handed, and a template that renders valid YAML around
    invalid JSON is exactly the failure this parse catches.
    """
    job = rendered[rendered.index("component: rustfs-scoped-users") :]
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


def test_the_ray_lane_reads_a_runtime_minted_warehouse_bucket(ray: dict) -> None:
    """The measured failure. `_resolve_roots` points a tenant's stage at its warehouse root, so a
    policy that names only static buckets 403s every project whose bucket was minted at runtime —
    and the mover reports it as a failed Ray job, never as a missing grant."""
    assert allowed(ray, action="s3:ListBucket", bucket=RUNTIME_BUCKET, prefix="medallion/"), (
        "the stage job cannot even LIST its upstream — this is the AccessDenied that killed the acme cascade"
    )
    assert allowed(ray, action="s3:GetObject", bucket=RUNTIME_BUCKET, key="medallion/bronze/_versions/1.manifest")
    assert allowed(ray, action="s3:PutObject", bucket=RUNTIME_BUCKET, key="medallion/silver/data/0.lance")


def test_the_ray_lane_still_cannot_list_the_control_bucket_outside_the_cascade(ray: dict) -> None:
    """The property the live hand-written policy has and the rendered one lost: the control bucket is
    listable only under `medallion*`. Widening the data allow must not widen this."""
    assert allowed(ray, action="s3:ListBucket", bucket=CONTROL_BUCKET, prefix="medallion/")
    assert not allowed(ray, action="s3:ListBucket", bucket=CONTROL_BUCKET, prefix="_projects/")
    assert not allowed(ray, action="s3:ListBucket", bucket=CONTROL_BUCKET, prefix="_warehouses/"), "the compute lane can enumerate the warehouse registry"
    # Both shapes of "list the bucket root": `mc ls bucket/` sends `prefix=` (the empty string, which
    # is not like `medallion*`), a bare ListBucket sends none at all. Neither may enumerate the
    # control plane's top-level prefixes, and the empty-string case is the one measured live.
    assert not allowed(ray, action="s3:ListBucket", bucket=CONTROL_BUCKET, prefix=""), "a root listing enumerates the control plane's prefixes"
    assert not allowed(ray, action="s3:ListBucket", bucket=CONTROL_BUCKET)


@pytest.mark.parametrize("guarded", CONTROL_PREFIXES)
def test_the_ray_lane_cannot_touch_control_records_in_ANY_bucket(ray: dict, guarded: str) -> None:
    """Widening the allow to every bucket widens the deny with it, or a tenant warehouse's own control
    records become reachable — which the static policy never had to think about because it reached no
    tenant bucket at all."""
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        assert not allowed(ray, action="s3:GetObject", bucket=bucket, key=f"{guarded}/x.json"), f"{bucket}/{guarded} is readable"
        assert not allowed(ray, action="s3:PutObject", bucket=bucket, key=f"{guarded}/x.json"), f"{bucket}/{guarded} is writable"


def test_the_ray_lane_cannot_STEER_the_plane_that_runs_it(ray: dict) -> None:
    """The escalation the first pass left open, and the reason `_tasks/` is not just another record.

    A registration under `<control_root>/_tasks/<hash>.json` carries an ENGINE and a COMMAND, and
    `engine_choice.resolve_task_registration` refuses a task nobody registered rather than submitting a
    key as a command — so the registry IS the list of what this estate's compute plane may run. A
    credential that can write one names its own command; a credential that can write `_transforms/`
    re-points which transform a lane runs; `_gates/` decides which output is admitted.

    These are exactly the records the Ray lane is the untrusted consumer OF. It reads `FROM_URI` and
    writes `TO_URI` (`scripts/ray_stage_job.py`) and touches no control record at all, so the deny is
    total rather than write-only.
    """
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        for steering in ("_tasks", "_transforms", "_gates"):
            assert not allowed(ray, action="s3:PutObject", bucket=bucket, key=f"{steering}/x.json"), (
                f"the compute lane can rewrite {bucket}/{steering}/ — it decides what the compute lane runs"
            )
            assert not allowed(ray, action="s3:GetObject", bucket=bucket, key=f"{steering}/x.json")
            assert not allowed(ray, action="s3:DeleteObject", bucket=bucket, key=f"{steering}/x.json")


def test_neither_credential_can_destroy_a_recoverable_drop(ray: dict, maintenance: dict) -> None:
    """A `_trash/` record is the ONLY thing that makes a dropped table restorable.

    `#75`/`#96`: a recoverable drop deregisters and files the record; `undrop` re-registers FROM it.
    Delete the record and the bytes are still there and nothing can reach them — a data-loss shape
    that leaves no error behind.

    The two credentials differ, and the difference is the purge: `purge.py` clears the record after it
    destroys the bytes (`trash_purge_record_not_cleared`), so maintenance must be able to delete one
    and Ray must not be able to touch one at all. Maintenance is held to reading before it may destroy.
    """
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        assert not allowed(ray, action="s3:DeleteObject", bucket=bucket, key="_trash/table/acme-silver$features.json"), (
            "the compute lane can make a recoverably-dropped table unrecoverable"
        )
        assert not allowed(ray, action="s3:PutObject", bucket=bucket, key="_trash/table/x.json")
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key="_trash/table/x.json"), (
        "the purge cannot read the record whose deadline it enforces"
    )
    assert allowed(maintenance, action="s3:DeleteObject", bucket=CONTROL_BUCKET, key="_trash/table/x.json"), (
        "the purge destroys the bytes and then cannot clear the record, which re-runs the destruction forever"
    )


def test_neither_credential_can_rewrite_the_namespace_INDEX(ray: dict, maintenance: dict) -> None:
    """`__manifest` is the authoritative namespace->table mapping — a namespace is a ROW in it.

    Writing it re-points or erases what a table id resolves to, which is the whole catalog's ground
    truth, and neither of these credentials has any business doing it: a stage job is handed its URIs,
    and compaction rewrites DATA FILES under a dataset root. The purge is the one legitimate reader —
    it re-checks a trashed id's liveness against `__manifest` before destroying bytes (`purge.py:257`),
    so this deny is on WRITES, not on reads.
    """
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        for policy, who in ((ray, "the compute lane"), (maintenance, "the sweep")):
            assert not allowed(policy, action="s3:PutObject", bucket=bucket, key=f"{MANIFEST_PREFIX}/acme-silver.json"), (
                f"{who} can rewrite {bucket}'s namespace index"
            )
            assert not allowed(policy, action="s3:DeleteObject", bucket=bucket, key=f"{MANIFEST_PREFIX}/acme-silver.json")
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key=f"{MANIFEST_PREFIX}/acme-silver.json"), (
        "the purge cannot re-check liveness, so it either refuses every record or destroys live bytes"
    )


def test_the_sweep_cannot_steer_the_plane_either(maintenance: dict) -> None:
    """Write-only, matching this policy's existing shape: maintenance reads records to decide what it
    may do (`_warehouses` for the bucket set, `_protection` for the pre-pass verdict) and writes none
    of the ones that govern it. It has no reader for `_tasks`/`_transforms`/`_gates` at all — a sweep
    does not choose a transform — so writing one could only ever be an escalation."""
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        for steering in ("_tasks", "_transforms", "_gates"):
            assert not allowed(maintenance, action="s3:PutObject", bucket=bucket, key=f"{steering}/x.json")
            assert not allowed(maintenance, action="s3:DeleteObject", bucket=bucket, key=f"{steering}/x.json")


def test_the_widening_DOES_disclose_the_bucket_list_and_that_is_the_trade(ray: dict) -> None:
    """The cost of allowing the data planes on every bucket, stated rather than implied.

    Neither policy grants `s3:ListAllMyBuckets` to the compute lane, and the commit that widened them
    claimed that made "list a bucket you can name" different from "enumerate the estate". MEASURED
    against the live RustFS with the deployed credential: `mc ls ray/` returns 104 buckets. The server
    answers a bucket ENUMERATION by falling back to per-bucket `ListBucket`/`GetBucketLocation`, which
    `arn:aws:s3:::*` grants everywhere — so withholding the account-level action does not withhold the
    list, and the claim was wrong.

    It stays wide, deliberately. The alternative is the bucket-name allow-list this policy replaced,
    which could not name a runtime-minted warehouse and broke every tenant's cascade — a correctness
    failure traded for a disclosure one. What is disclosed is bucket NAMES and, under `*/*`, object
    reads outside the denied control prefixes; the narrowing that actually fixes it is per-table vended
    credentials on the Ray lane, tracked as a row rather than pretended away here.

    Asserted so the false claim cannot come back: the policy's own shape says the enumeration is
    reachable, and a future edit that believes otherwise fails here.
    """
    assert not any(
        "s3:ListAllMyBuckets" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])
        for s in ray["Statement"]
        if s["Effect"] == "Allow"
    ), "the compute lane now has the account-level action, which is a widening beyond the measured fallback"
    # Every bucket answers a per-bucket list, which is what the server uses to enumerate.
    assert allowed(ray, action="s3:ListBucket", bucket="any-tenant-bucket", prefix="medallion/")
    assert allowed(ray, action="s3:GetBucketLocation", bucket="any-tenant-bucket")


def test_the_ray_lane_never_reaches_the_observability_store(ray: dict) -> None:
    assert not allowed(ray, action="s3:GetObject", bucket=OBSERVABILITY_BUCKET, key="anything")
    assert not allowed(ray, action="s3:ListBucket", bucket=OBSERVABILITY_BUCKET, prefix="")


# ---- the maintenance sweep ----------------------------------------------------------------------


def test_the_sweep_rewrites_a_bucket_it_discovered_from_the_registry(maintenance: dict) -> None:
    """`_buckets_to_sweep` extends the configured list from the warehouse registry, so the policy has
    to cover buckets no render can name. Its failure mode is the quiet one the sweep's own comment
    describes: `compact_one` reports AccessDenied per dataset as an `open:` error, which reads as a
    broken dataset rather than a missing grant, and `ack_for` acks it as SUCCESS."""
    assert allowed(maintenance, action="s3:ListBucket", bucket=RUNTIME_BUCKET, prefix="medallion/")
    assert allowed(maintenance, action="s3:PutObject", bucket=RUNTIME_BUCKET, key="medallion/silver/data/0.lance")
    assert allowed(maintenance, action="s3:DeleteObject", bucket=RUNTIME_BUCKET, key="medallion/silver/_versions/1.manifest")


@pytest.mark.parametrize("guarded", ["_projects", "_protection", "_warehouses"])
def test_the_sweep_cannot_rewrite_the_records_that_govern_it_in_ANY_bucket(maintenance: dict, guarded: str) -> None:
    """The whole point of the scoped user: a compaction credential that can rewrite `_protection/` can
    turn off the shallow-clone guard that stops it destroying another dataset's source."""
    for bucket in (CONTROL_BUCKET, RUNTIME_BUCKET):
        assert not allowed(maintenance, action="s3:PutObject", bucket=bucket, key=f"{guarded}/x.json")
        assert not allowed(maintenance, action="s3:DeleteObject", bucket=bucket, key=f"{guarded}/x.json")


def test_the_sweep_still_reads_the_records_it_must_read(maintenance: dict) -> None:
    """READ, not write. It resolves the buckets to sweep out of `_warehouses/` and the per-object
    protection verdict out of `_protection/`; denying the read would blind the pre-pass rather than
    constrain it."""
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key="_warehouses/acme-bucket.json")
    assert allowed(maintenance, action="s3:GetObject", bucket=CONTROL_BUCKET, key="_protection/table.json")
    assert allowed(maintenance, action="s3:ListBucket", bucket=CONTROL_BUCKET, prefix="_warehouses/")


def test_the_sweep_keeps_writing_its_own_cadence_stamp(maintenance: dict) -> None:
    """`_stamp_cadence` writes under `_policies/state/` on every maintained dataset and
    `_policy_skip_reason` reads a missing stamp as "maintain" — so denying this prefix does not fail
    loudly, it silently compacts every policied dataset on every tick. Pinned so a future tightening
    has to argue with a test rather than a comment."""
    assert allowed(maintenance, action="s3:PutObject", bucket=CONTROL_BUCKET, key="_policies/state/some-dataset.json")
    assert allowed(maintenance, action="s3:PutObject", bucket=RUNTIME_BUCKET, key="_policies/state/some-dataset.json")


def test_the_sweep_never_reaches_the_observability_store(maintenance: dict) -> None:
    assert not allowed(maintenance, action="s3:GetObject", bucket=OBSERVABILITY_BUCKET, key="anything")


def test_the_drift_report_can_still_enumerate_the_account(maintenance: dict) -> None:
    """`reconcile` lists every bucket to find orphans — a bucket claimed by no warehouse record. It is
    the one thing here that is deliberately account-wide."""
    assert allowed(maintenance, action="s3:ListAllMyBuckets", bucket="*")


# ---- the provisioning step itself ----------------------------------------------------------------


def test_a_malformed_policy_fails_the_hook_instead_of_leaving_the_old_one_attached() -> None:
    """`mc admin policy create` OVERWRITES an existing policy on this RustFS — probed 2026-09-04: a
    second create with different content returned exit 0 and the readback showed the new document. So
    the `|| true` that used to sit on the create was not protecting against "already exists"; all it
    could do was swallow a REAL failure (malformed JSON, admin denied) and let the hook report success
    while the credential kept its old policy. The user-add keeps its `|| true`, which genuinely does
    guard a re-run."""
    rendered = _render()
    job = rendered[rendered.index("component: rustfs-scoped-users") :]
    creates = re.findall(r"mc admin policy create rfs \S+ \S+( \|\| true)?", job)
    assert creates, "no policy is created at all"
    assert not any(creates), "a policy create still swallows its failure — a bad policy renders as a successful hook"
