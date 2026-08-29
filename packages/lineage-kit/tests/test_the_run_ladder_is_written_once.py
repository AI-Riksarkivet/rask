"""Opening a child run resolves ONE ladder, in one place, and reads settings once (PS-24, PS-25).

PS-24 — `stage.py`, `actor.py` and `runs.py` each carried their own copy of "compose the child job
name off the parent, then default the namespace from (argument → parent → settings)". Three copies of
a four-branch ladder is three chances to drift, and they HAD drifted: `job_run` dropped the parent
branch entirely, so a run opened as the child of a run in namespace `htr` silently landed in `rask`.

PS-25 — each of those copies built a fresh `LineageSettings()` — a full pydantic-settings environment
read and validation — on EVERY run open. A `@stage` callable is invoked per batch inside a Ray Data
pipeline, so that is per unit of work, for a value that cannot change inside a process.
"""

from __future__ import annotations

import pytest
from lineage_kit import LineageContext, RecordingEmitter, job_run, stage, use_context
from lineage_kit.context import child_job_name, resolve_namespace


def _ctx(namespace: str = "htr", job_name: str = "ingest") -> LineageContext:
    return LineageContext.root(namespace=namespace, job_name=job_name, run_id="6f2b1a5e-1f3d-5a0e-9c4b-2f9f0a7d1c33")


# ── PS-24: one ladder ────────────────────────────────────────────────────────────────────────


def test_the_namespace_ladder_prefers_argument_then_parent_then_settings() -> None:
    parent = _ctx(namespace="htr")
    assert resolve_namespace("explicit", parent) == "explicit"
    assert resolve_namespace(None, parent) == "htr"
    assert resolve_namespace(None, None) == "rask"  # the LineageSettings default


def test_a_child_job_name_composes_off_the_parent_and_stands_alone_without_one() -> None:
    assert child_job_name(_ctx(job_name="ingest"), "layout") == "ingest.layout"
    assert child_job_name(None, "layout") == "layout"


def test_a_job_run_opened_under_a_parent_inherits_the_parents_namespace(recording: RecordingEmitter) -> None:
    """The drifted copy: `job_run` defaulted straight to settings, ignoring the parent it was given."""
    with job_run("promote", parent=_ctx(namespace="htr"), emitter=recording):
        pass
    assert [e.job.namespace for e in recording.events] == ["htr", "htr"]


def test_a_stage_under_an_ambient_parent_inherits_its_namespace(recording: RecordingEmitter) -> None:
    @stage("layout", emitter=recording)
    def layout() -> None:
        return None

    with use_context(_ctx(namespace="htr")):
        layout()
    assert {e.job.namespace for e in recording.events} == {"htr"}
    assert {e.job.name for e in recording.events} == {"ingest.layout"}


# ── PS-25: the settings read is not per run ──────────────────────────────────────────────────


def test_the_namespace_default_is_read_once_per_process(monkeypatch: pytest.MonkeyPatch, recording: RecordingEmitter) -> None:
    from lineage_kit import config

    built: list[int] = []
    original = config.LineageSettings.__init__

    def counting(self, **kwargs) -> None:
        built.append(1)
        original(self, **kwargs)

    monkeypatch.setattr(config.LineageSettings, "__init__", counting)
    config.lineage_settings.cache_clear()

    @stage("layout", emitter=recording)
    def layout() -> None:
        return None

    layout()
    layout()
    layout()
    assert built == [1], f"a full settings read per run open: {len(built)} for three stage invocations"


def test_the_cached_settings_are_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage_kit import config

    monkeypatch.setenv("RASK_LINEAGE_NAMESPACE", "audio")
    config.lineage_settings.cache_clear()
    assert config.lineage_settings().namespace == "audio"
