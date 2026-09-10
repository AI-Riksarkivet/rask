"""A lineage event this lane cannot deliver must be STAGED, not dropped.

§ E1's second half. Four of the five lakehouse producers stage durably through the shared object-store
outbox; `ingest` had zero outbox usage and emitted bare, so a refused or unreachable door lost the
event outright. That is recorded as having happened twice on this lane — `service_identity.py`: "a 401
there does not surface as an error, it surfaces as a permanent gap in the graph that looks exactly like
a healthy estate. That has already happened twice on this lane (the trainer in 2026-07,
`service-ingest` on 2026-08-06, a day of 403s while the data landed)."

The swallow itself is CORRECT and stays — a run whose data landed must not be reported as failed
because the graph was unreachable (I8). Staging is what honours that constraint without losing the
event: it cannot fail the run either, and what is staged is drained later by lineage's reconcile cron.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


class _Refusing:
    """The measured shape: the door answers 401/403, the emitter logs, counts, and reports False."""

    def emit(self, event: Any) -> bool:
        return False


def _refuse_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject the refusing emitter through ingest's OWN seam.

    Patching `lineage_kit.emitter.default_emitter` does not work and the reason is worth writing down:
    `runs.py` binds the name at import (`from lineage_kit.emitter import default_emitter`), so the
    module attribute a test replaces is not the one the run resolves. `ingest.lineage._emitter` is the
    seam this service actually injects through, which makes it the honest place to substitute.
    """
    from ingest import lineage as ingest_lineage

    monkeypatch.setattr(ingest_lineage, "_emitter", lambda: _Refusing())


def test_a_refused_event_is_written_to_the_outbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bytes must be there afterwards — a local filesystem outbox, so this asserts on real IO
    rather than on a mock having been called."""
    from ingest import lineage as ingest_lineage

    _refuse_everything(monkeypatch)
    outbox = tmp_path / "_lineage_outbox"
    monkeypatch.setenv("RASK_INGEST_LINEAGE_OUTBOX_URI", str(outbox))
    ingest_lineage.LineageRecorder().start("run-e1", "proj", "ds", "s3", {}, originator="user:alice")

    staged = list(outbox.glob("*.json"))
    assert staged, "the door refused and nothing was staged — the event is gone and the graph looks healthy"
    body = json.loads(staged[0].read_text())
    assert body.get("eventType"), f"what was staged is not a RunEvent the relay can re-ingest: {sorted(body)}"


def test_an_UNWIRED_outbox_stages_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty means off. Writing to a guessed prefix nothing drains is a leak wearing recovery's name —
    the same argument `stage_lineage_url` makes for not posting to a guessed address."""
    from ingest import lineage as ingest_lineage

    _refuse_everything(monkeypatch)
    monkeypatch.delenv("RASK_INGEST_LINEAGE_OUTBOX_URI", raising=False)
    ingest_lineage.LineageRecorder().start("run-e1b", "proj", "ds", "s3", {})
    assert not list(tmp_path.glob("**/*.json")), "an unwired deployment staged events anyway"


def test_an_UNREACHABLE_outbox_never_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """I8 at the recovery layer. A stager that cannot reach the object store is the same outage one
    level down, and must not turn a run whose data landed into a failed one."""
    from ingest import lineage as ingest_lineage

    _refuse_everything(monkeypatch)
    # A path the process cannot create, so the failure is IMMEDIATE. An unreachable S3 URI proves the
    # same thing and costs the suite twenty seconds of connection timeouts to do it.
    monkeypatch.setenv("RASK_INGEST_LINEAGE_OUTBOX_URI", "/proc/self/mem/_lineage_outbox")
    ingest_lineage.LineageRecorder().start("run-e1c", "proj", "ds", "s3", {})  # must not raise


def test_the_staging_options_carry_a_VENDED_credential(monkeypatch) -> None:
    """The staged write is signed, and it was not — the backstop could not reach its own bucket.

    `_outbox_storage_options` returned `{"endpoint": ...}` and nothing else, under a docstring claiming
    the options were "resolved the way every other S3 caller in this service resolves it". They were
    not: every other caller vends from the catalog, and this one signed with nothing. Measured twice
    inside one real run 2026-09-10 — `stage_event` died `AWS Error ACCESS_DENIED during HeadBucket` —
    so the recovery path for a refused emit could never fire, on the very run whose emit had just been
    refused.

    A STATIC KEY IS NOT THE FIX and the estate's rule says so ("STS for storage"). Ingest holding no S3
    key is the stronger posture, so the credential comes from the catalog's outbox door, scoped by an
    STS session policy to that one prefix and gated on `can_stage_events`.
    """
    from ingest import catalog_service
    from ingest import lineage as lineage_mod
    from service_kit.lakehouse.vended_credentials import VendedCredential

    # The REAL dependency, patched where it lives — `_outbox_storage_options` imports it inside the
    # function. Inventing an indirection in production code so a test can reach it would make the seam
    # exist for the test's benefit rather than the caller's.
    monkeypatch.setattr(
        catalog_service,
        "vend_outbox_options",
        lambda **_kw: VendedCredential(options={"aws_access_key_id": "K", "aws_secret_access_key": "S", "aws_session_token": "T"}, expires_at_millis=None),
    )
    options = lineage_mod._outbox_storage_options()
    assert options.get("aws_access_key_id") == "K", f"the staged write is unsigned: {sorted(options)}"
    assert options.get("aws_session_token") == "T", "a vended STS credential is a triple; the token carries the scoping"


def test_a_refused_vend_DEGRADES_and_never_ends_the_run(monkeypatch) -> None:
    """Staging is the backstop, so a vend that fails must not turn a landed commit into a failed run.

    I8's rule, applied one layer down: `_stage_undelivered` is documented never to raise, and the
    credential it needs is fetched over the network. A vend that is refused or unreachable therefore
    leaves the write to fail as it did before — loudly, naming the cause — rather than propagating.
    """
    from ingest import catalog_service
    from ingest import lineage as lineage_mod

    def _boom(**_kw: object) -> None:
        raise RuntimeError("catalog unreachable")

    monkeypatch.setattr(catalog_service, "vend_outbox_options", _boom)
    assert lineage_mod._outbox_storage_options() is not None, "a refused vend must degrade, not raise"
