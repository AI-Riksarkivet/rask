"""Maintenance's own identity reads every bucket it sweeps and writes nothing but its own records ([[XC-084]]).

THE BUCKET SET IS NOT KNOWABLE AT RENDER TIME, so the read grant enumerates none. `catalog.warehouses.enabled` (on by
default) provisions one physically separate bucket per warehouse at `POST /management/v1/warehouses`, with an
operator-chosen name, and the sweep's discovery and the purge's protection pre-pass open those buckets
(`sweep.py::_buckets_to_sweep` extends its configured list from the warehouse registry itself). Measured on the live
estate 2026-09-04/05: a policy that reached warehouses through a `*-wh` name pattern refused `acme-bucket`.

EVERY WRITE IS A VEND'S, owner, 2026-11-03: a compromised maintenance pod must not be able to defeat an erasure or a hold.
So its own identity writes only the records no table owns, on the control root: its due records, its cadence stamps and
its lineage outbox. A table's bytes, refs and records are written under the catalog's maintain-tier vend for that table,
and the trash under the reclaim session the catalog vends for one record (`catalog.core.vending`).

The evaluator below is IAM-shaped (explicit Deny wins, otherwise an Allow must match) so each test states the OUTCOME a
credential gets. A string grep cannot: it can only check the buckets the chart names, and a registry-discovered one is
by definition not among them.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.test_invariants import _helm_template


#: A bucket name no static value in the render can possibly contain — the shape `POST /management/v1/warehouses`
#: mints. Deliberately NOT `*-wh`: that suffix is a convention, not a rule, and the live estate's
#: `acme-bucket` is the counterexample that broke the cascade.
RUNTIME_BUCKET = "acme-bucket"
CONTROL_BUCKET = "lance-catalog"

_BUCKET_ACTIONS = frozenset({"s3:ListBucket", "s3:GetBucketLocation", "s3:ListAllMyBuckets"})


def _policy(rendered: str, name: str) -> dict:
    """The policy `<release>-object-store-identities` declares for the `name` identity, parsed.

    Read out of the render rather than out of a values file on purpose: what governs the credential is the JSON the hook
    hands the store's `add-canned-policy` call, and a template that renders valid YAML around invalid JSON is exactly
    the failure this parse catches.
    """
    [identities] = [
        doc["data"]
        for doc in yaml.load_all(rendered, Loader=FAST_LOADER)
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"].endswith("-object-store-identities")
    ]
    [document] = [body for key, body in identities.items() if key.endswith(f"-sa-{name}.json")]
    return json.loads(document)


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
    return _policy(_helm_template(), "maintenance")


# ---- the maintenance sweep ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "bucket", "key", "granted"),
    [
        pytest.param("s3:GetObject", RUNTIME_BUCKET, "medallion/_versions/1.manifest", True, id="reads-a-runtime-warehouse"),
        pytest.param("s3:GetObject", CONTROL_BUCKET, "_protection/table-x.json", True, id="reads-the-records-it-judges-by"),
        pytest.param("s3:PutObject", CONTROL_BUCKET, "_maintenance_due/table-x.json", True, id="writes-its-due-records"),
        pytest.param("s3:PutObject", CONTROL_BUCKET, "_policies/state/u/cadence-x.json", True, id="writes-its-cadence-stamps"),
        pytest.param("s3:DeleteObject", CONTROL_BUCKET, "_lineage_outbox/run.json", True, id="drains-its-own-outbox"),
        pytest.param("s3:DeleteObject", RUNTIME_BUCKET, "medallion/_versions/1.manifest", False, id="deletes-no-version"),
        pytest.param("s3:PutObject", RUNTIME_BUCKET, "medallion/_refs/tags/t.json", False, id="writes-no-ref"),
        pytest.param("s3:PutObject", CONTROL_BUCKET, "_policies/table-x.json", False, id="writes-no-policy"),
        pytest.param("s3:DeleteObject", CONTROL_BUCKET, "_erasure_obligations/x.json", False, id="ends-no-erasure"),
        pytest.param("s3:DeleteObject", CONTROL_BUCKET, "_trash/table-x.json", False, id="ends-no-trash-record"),
        pytest.param("s3:PutObject", CONTROL_BUCKET, "_history_fences/table-x.json", False, id="holds-no-fence"),
    ],
)
def test_maintenance_reads_the_estate_and_writes_only_its_own_records(maintenance: dict, action: str, bucket: str, key: str, granted: bool) -> None:
    assert allowed(maintenance, action=action, bucket=bucket, key=key) is granted
