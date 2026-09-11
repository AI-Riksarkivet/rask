"""The train-watcher's FAIL event must carry an authorizable SUBJECT, not a role literal.

`_publish_train_fail` builds its RunEvent by hand rather than through `build_run_event` — deliberately,
because it reports a job that died before it could emit anything itself. That hand-building is how it
was left behind when LH-121 fixed the author subject at all seven `build_run_event` sites: it stamps
``custom_facet(_PRODUCER, name=settings.author, sub=settings.author)``, putting the DISPLAY NAME in the
subject slot too.

Since 35fcabb4 the lineage bus door authorizes AS ``author.sub`` and demands ``can_write_data`` on every
output. In the deployed producer pod ``settings.author`` is the stage-runner default ``data_eng``, and
live OpenFGA answers ``user:data_eng can_write_data table:models$auditprobe`` = **false** while
``service-medallion-producer`` = **true**. So the event is refused and dropped.

WHAT IS LOST IS PROVENANCE, NOT THE NOTIFICATION — and the distinction matters, because the fix is
cheap only if the cost is stated accurately. The person is still told through the control lane. What
disappears is the graph record of a training run that failed before emitting, which is the ONE thing
this lane exists to produce.

The correct shape is two fields apart, and its sibling in this same module already has it:
`workflow.py:747-748` passes ``author=settings.author`` (the human-readable name) alongside
``author_subject=settings.fga_service_identity`` (the identity the bus can check).
"""

from __future__ import annotations

from medallion.core.config import MedallionSettings


def test_the_settings_keep_the_display_name_and_the_subject_apart() -> None:
    """The premise: these are two different fields, and conflating them is what the defect did."""
    settings = MedallionSettings.model_validate({})

    assert settings.author != settings.fga_service_identity, (
        "if the display name and the service identity were the same value this defect would be invisible and the gate below would prove nothing"
    )


def test_the_train_fail_event_stamps_the_service_identity_as_its_subject() -> None:
    """THE GATE, read off the source because the publish needs a workflow runtime to drive.

    Asserted on the call rather than through a live emit: the bus-door consequence is already measured
    (`user:data_eng can_write_data` is false on the live store), so what this must stop is the SPELLING
    coming back — a future edit re-reaching for `settings.author` because it is the nearer name.
    """
    from pathlib import Path

    body = (Path(__file__).resolve().parents[1] / "src/medallion/workflow.py").read_text(encoding="utf-8")
    start = body.index("def _publish_train_fail")
    fragment = body[start : start + 4000]

    assert "sub=settings.author" not in fragment, (
        "the train FAIL event stamps the role literal `settings.author` as its subject; the lineage bus "
        "authorizes AS that value and `user:data_eng` holds can_write_data on nothing, so the event is "
        "refused and the failed run leaves no trace in the graph"
    )
    assert "sub=settings.fga_service_identity" in fragment, "the subject must be the identity the bus can actually authorize"
