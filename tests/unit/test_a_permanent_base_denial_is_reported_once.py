"""A base probe the credential can never pass is CONFIGURATION, not an incident, and says so once.

[[LH-163]]. `gather_compaction_bases` probes every base a manifest declares and records a failure as
the unknown it is — "unknown resolves to refusal", which is correct and stays. What was wrong is how
it reported one particular failure: a PERMISSION DENIAL was logged at WARNING with a full stack trace,
on every dataset, on every pass.

MEASURED on the running sweep 2026-09-15: **134 rendered tracebacks in a single pass, every one of
them `base='s3://lance-catalog/models/' denied=True`** — the same base, the same credential, the same
answer. `rask-maintenance` signs with a scoped key that cannot read the model-registry prefix, so the
probe can never succeed.

WHY ONCE PER PROCESS IS THE EXACT SHAPE rather than merely a quieter one. The S3 credential is
resolved ONCE, at boot, from the Dapr secret store (`apply_dapr_secrets` runs in the lifespan). It
cannot change while the process lives, so a denial for a given base is deterministic for the life of
that process — reporting it again tells the reader nothing that was not in the first line. A rotated
credential arrives by rollout, which is a new process and therefore a fresh report.

AND A TRANSIENT FAILURE MUST KEEP ITS TRACEBACK, which is the half that makes this safe: a store
outage, a malformed URI or a bug in the probe is exactly the case where the stack matters. Only the
denial — the one failure whose cause is fully named by its own classification — loses it.

The cost of getting this wrong is not tidiness. That pass carried 315 refusals that were CORRECT (the
shallow-clone protection), and 134 stack traces of a permanent misconfiguration sitting beside them is
how a reader learns to scroll past the whole category.
"""

from __future__ import annotations

import logging
from typing import cast

import pytest

from service_kit.lakehouse import features


class _Ref:
    """One declared base, shaped as `manifest_base_path_refs` returns them."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.is_dataset_root = False


class _Carrier:
    """A `FragmentCarrier` whose manifest declares one base and whose fragments read cleanly.

    `_ds` raises rather than returning a stub: `manifest_base_path_refs` is monkeypatched in every
    test here, so nothing may reach the real manifest seam — and a test double that quietly answered
    would hide the day that stopped being true.
    """

    @property
    def _ds(self) -> object:
        raise AssertionError("the manifest seam was reached — this suite stubs `manifest_base_path_refs`")

    def get_fragments(self) -> list[object]:
        return []


@pytest.fixture(autouse=True)
def _a_cold_process() -> None:
    """Each test starts having reported nothing — the set is process-scoped by design."""
    features.forget_reported_base_denials()


def _probe_denied(_path: str) -> bool:
    raise OSError("AWS Error ACCESS_DENIED during HeadObject operation: No response body")


def _probe_broken(_path: str) -> bool:
    raise OSError("connection reset by peer")


def _gather(monkeypatch: pytest.MonkeyPatch, probe, *, base: str = "s3://lance-catalog/models/") -> features.CompactionBases:
    monkeypatch.setattr(features, "manifest_base_path_refs", lambda _ds: [_Ref(base)])
    return features.gather_compaction_bases(cast("features.FragmentCarrier", _Carrier()), probe)


def test_a_denial_is_reported_ONCE_however_many_datasets_declare_it(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """THE DEFECT: 134 datasets declaring one unreadable base produced 134 stack traces."""
    with caplog.at_level(logging.WARNING, logger=features.__name__):
        for _ in range(5):
            _gather(monkeypatch, _probe_denied)

    denials = [r for r in caplog.records if "denied" in r.message or "denial" in r.message]
    assert len(denials) == 1, f"a permanent denial was reported {len(denials)} times: {[r.message for r in denials]}"


def test_the_denial_line_carries_NO_traceback(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A stack trace of an S3 403 names nothing the reader can act on — the classification already did."""
    with caplog.at_level(logging.WARNING, logger=features.__name__):
        _gather(monkeypatch, _probe_denied)

    assert caplog.records, "the denial was not reported at all"
    assert all(r.exc_info is None for r in caplog.records), "a permanent denial still renders a stack trace"


def test_a_TRANSIENT_failure_keeps_its_traceback_and_repeats(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The half that makes the silence safe: only the denial is de-duplicated.

    A store outage or a probe bug is exactly where the stack matters, and it is NOT deterministic for
    the life of the process — so it is reported every time, with `exc_info`, as before.
    """
    with caplog.at_level(logging.WARNING, logger=features.__name__):
        for _ in range(3):
            _gather(monkeypatch, _probe_broken)

    failures = [r for r in caplog.records if r.exc_info is not None]
    assert len(failures) == 3, f"a transient probe failure was silenced or de-duplicated: {len(failures)}"


def test_the_REFUSAL_is_unchanged_by_any_of_this(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logging is all that moved. `unknown resolves to refusal` is the guard, and it still holds.

    Pinned because a de-duplication bug that also dropped the evidence would silently PERMIT the
    rewrite these bases exist to refuse — the failure direction that costs a clone its reason to exist.
    """
    gathered = _gather(monkeypatch, _probe_denied)

    assert [b.probed_dataset_root for b in gathered.bases] == [None], "the unknown was resolved to something"
    assert [b.probe_denied for b in gathered.bases] == [True], "the denial evidence was lost"


def test_two_DIFFERENT_bases_are_each_reported(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Per BASE, not one line for the whole process — a second unreadable prefix is its own news."""
    with caplog.at_level(logging.WARNING, logger=features.__name__):
        _gather(monkeypatch, _probe_denied, base="s3://lance-catalog/models/")
        _gather(monkeypatch, _probe_denied, base="s3://other-bucket/archive/")

    denials = [r for r in caplog.records if "denied" in r.message or "denial" in r.message]
    assert len(denials) == 2, f"a second unreadable base was swallowed by the first: {len(denials)}"
