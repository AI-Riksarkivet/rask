"""The lineage service's storage identity may READ the estate and DELETE only its own outbox.

§F2-1's third plane. The medallion and maintenance identities landed first and are the pattern; this
one is not another copy of it, because the SURFACE is different and measuring it changed the policy.

NO `PutObject` ANYWHERE, which no other scoped plane here can say. Measured 2026-09-07 against the
source rather than inferred from the service's importance: lineage opens datasets READ-ONLY to probe
versions, schema and dangling blobs (`core/reconcile.py:58,76,93,108`, `demo.py:112`), and the only
bytes it changes are DELETES of outbox objects the relay has already re-ingested
(`outbox.drop_event`). `outbox.stage_event` — the write half — is called by the PRODUCERS (medallion,
maintenance) and never here. Granting this identity a write would grant a capability it has no code
to use, which is the whole failure this section exists to stop.

THE READ IS DELIBERATELY UNSCOPED, and that is the interesting half. Lineage reconciles whatever
datasets the graph names, across warehouses this chart does not enumerate — so a narrowed read is a
reconciler that silently stops seeing part of the estate. And a reconciler that cannot read reports
`known=False`, which publishes nothing and looks exactly like a healthy estate. That failure mode is
why the READ stays wide and the WRITE is what gets scoped.

THE PAIR IS THE PART THAT HAS BITTEN TWICE. A scoped ACCESS KEY left on the default secret field
(`minio-secret-key`, the tenant ROOT's) is signed with a mismatched pair, and every operation fails
`SignatureDoesNotMatch` — the Ray and maintenance identities each paid for it. So this asserts the
field moves WITH the key, not merely that the key was set.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from test_invariants import _rendered_docs  # noqa: E402


SCOPED = "minio.lineageAccessKey=rask-lineage"


def _lineage_env(*sets: str) -> dict[str, str]:
    for doc in _rendered_docs(*sets):
        if doc.get("kind") != "Deployment" or not doc["metadata"]["name"].endswith("-lineage"):
            continue
        container = doc["spec"]["template"]["spec"]["containers"][0]
        return {e["name"]: str(e.get("value") or "") for e in container.get("env") or []}
    return {}


def _provisioning_script(*sets: str) -> str:
    """The scoped-users hook's shell script, read as the STRING it is.

    Not `str(spec)`: that renders the dict's repr and escapes every quote, so a marker containing
    `<<'POLICY'` never matches and the parse silently finds nothing — which reads as "the policy is
    missing" rather than "the gate is broken".
    """
    for doc in _rendered_docs(*sets):
        if doc.get("kind") != "Job":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers") or []:
            for part in container.get("command") or []:
                if "/tmp/lineage.json" in str(part):
                    return str(part)
    return ""


def _policy(*sets: str) -> dict[str, object] | None:
    """The `rask-lineage` policy document, parsed out of the provisioning hook's script."""
    script = _provisioning_script(*sets)
    if "cat >/tmp/lineage.json" not in script:
        return None
    body = script.split("cat >/tmp/lineage.json <<'POLICY'", 1)[1].split("\nPOLICY", 1)[0]
    return json.loads(body)


def _statements(*sets: str) -> list[dict[str, Any]]:
    """The policy's statements, NARROWED here so every reader below stays checkable.

    `json.loads` answers `object`, so subscripting the document at each call site is unverifiable and
    a suppression there would silence a genuinely wrong key in the same breath as the parse. Narrowing
    once, with the shape asserted, keeps the three gates below reading a real list of statements.
    """
    document = _policy(*sets)
    assert document is not None, "the rask-lineage policy did not render — every gate below would be vacuous"
    statements = document["Statement"]
    assert isinstance(statements, list), f"the policy's Statement is a {type(statements).__name__}, not a list of statements"
    return [s for s in statements if isinstance(s, dict)]


def test_the_gate_can_see_both_halves() -> None:
    """A renamed value or a moved hook would make every assertion below vacuous."""
    assert _lineage_env(SCOPED), "no lineage Deployment rendered — this gate is blind"
    assert _policy(SCOPED) is not None, "the rask-lineage policy did not render"


def test_the_DEFAULT_is_the_scoped_identity_and_EMPTY_is_the_way_back() -> None:
    """Measured 2026-09-08: lineage was the one plane of three whose identity was built, provisioned
    and never selected — `helm get values` named maintenance and medallion and not lineage — so the
    running pod presented `minioadmin` while holding the tightest policy in the estate. The default
    now names it; explicitly emptying the key is the deliberate way back."""
    default = _lineage_env()
    assert default.get("LINEAGE_S3_ACCESS_KEY_ID") == "rask-lineage", "lineage presents the tenant root on a default install"
    assert default.get("LINEAGE_DAPR_SECRET_S3_FIELD") == "lineage-s3-secret-key", "the scoped key is paired with the tenant root's secret field"

    unnamed = _lineage_env("minio.lineageAccessKey=")
    assert unnamed.get("LINEAGE_S3_ACCESS_KEY_ID") == "minioadmin", "an explicitly emptied identity did not fall back"
    assert "LINEAGE_DAPR_SECRET_S3_FIELD" not in unnamed, "the scoped secret field is set with no scoped key"


def test_the_KEY_and_its_SECRET_FIELD_move_together() -> None:
    """THE PAIR, and the half that has bitten twice. A scoped access key left on the default secret
    field is signed against the tenant ROOT's secret: `SignatureDoesNotMatch` on every operation."""
    env = _lineage_env(SCOPED)
    assert env["LINEAGE_S3_ACCESS_KEY_ID"] == "rask-lineage"
    assert env["LINEAGE_DAPR_SECRET_S3_FIELD"] == "lineage-s3-secret-key", (
        "the scoped key is set and the secret field still points at the tenant root's secret — every S3 operation will fail SignatureDoesNotMatch"
    )


def test_the_secret_is_SEEDED_under_the_name_the_service_reads() -> None:
    """The third half again: a field the service is told to read and nothing writes is a fail-closed
    boot, because `fetch_required_secrets` raises rather than degrading."""
    seeded = any(
        "lineage-s3-secret-key=" in str(part)
        for doc in _rendered_docs(SCOPED)
        if doc.get("kind") == "Job" and "openbao" in doc["metadata"]["name"]
        for container in (doc["spec"]["template"]["spec"].get("containers") or [])
        for part in (container.get("command") or []) + (container.get("args") or [])
    )
    assert seeded, "LINEAGE_DAPR_SECRET_S3_FIELD names a secret the OpenBao seed never mints"


def test_the_policy_grants_NO_write_anywhere() -> None:
    """THE GATE. This plane has no code that writes an object; a policy permitting one grants a
    capability nothing uses, which is exactly what §F2-1 exists to remove."""
    statements = _statements(SCOPED)
    allowed = {a for s in statements if s["Effect"] == "Allow" for a in s["Action"]}
    forbidden = {"s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts", "s3:*"}
    assert not (allowed & forbidden), f"the lineage policy allows writes it has no code to perform: {sorted(allowed & forbidden)}"


def test_the_only_DELETE_is_its_own_outbox() -> None:
    """`drop_event` is the one mutation this plane makes, and the prefix it makes it on is its own."""
    statements = _statements(SCOPED)
    deletes = [s for s in statements if s["Effect"] == "Allow" and "s3:DeleteObject" in s["Action"]]
    assert deletes, "the relay cannot drain its outbox — every re-ingested event is re-delivered forever"
    for statement in deletes:
        for resource in statement["Resource"]:
            assert resource.endswith("/_lineage_outbox/*"), f"delete is permitted outside the outbox: {resource}"


def test_the_READ_stays_wide_on_purpose() -> None:
    """Narrowing it would be the silent failure, not the safe choice: lineage reconciles datasets this
    chart does not enumerate, and a reader that cannot read reports `known=False` — which publishes
    nothing and is indistinguishable from a healthy estate."""
    statements = _statements(SCOPED)
    reads = [s for s in statements if s["Effect"] == "Allow" and "s3:GetObject" in s["Action"]]
    assert reads, "lineage cannot read the datasets it reconciles"
    assert any("arn:aws:s3:::*/*" in s["Resource"] for s in reads), "the read was narrowed to a bucket set the chart cannot know"
