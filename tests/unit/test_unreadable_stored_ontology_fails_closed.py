"""A stored ontology the current model cannot parse is refused BY NAME, not as an anonymous 500.

The ontology is a CLOSED-SET contract: `send`, `import`, `assist` and `submit` all judge shapes
against it, and the publish path stamps its class list into the run facet. So a stored ontology the
running code cannot read cannot be treated as "constrains nothing" — that silently converts a
project with rules into a project with none, on the exact paths (bulk send, import, publish) where
one click is hundreds of rows. Fail closed.

Failing closed is only half of it. The refusal today is real but ANONYMOUS: both actors validate the
whole stored document in `_load`, so an unreadable ontology raises a bare `pydantic.ValidationError`
out of `get()`, crosses the sidecar as an opaque `ERR_ACTOR_INVOKE_METHOD` string, and reaches the
annotator as `500 Internal Server Error` naming neither the project nor the reason — with every
subsequent call to that project failing the same way. These tests pin the named refusal instead: a
`DomainError` naming the project (or the task) and the field that could not be read, which is what
turns "the project is bricked" into "this project's stored ontology is not readable by this build".

Driven against the REAL actors with a fake state manager — no sidecar, no placement service — so the
document under test is a genuinely persisted one, written the way an older model version would have
written it.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from annotator.projects.actor import TASK_KEY, AnnotationTaskActor
from annotator.projects.models import AnnotationProject, ProjectState, Task
from annotator.projects.project_actor import PROJECT_KEY, AnnotationProjectActor
from service_kit.exceptions import DomainError


#: An ontology document an OLDER model version would have written. Before the ontology merge the
#: project carried a separate `template` object holding `required_labels: list[str]`; that field has
#: no home on `LabelOntology`, whose `extra="forbid"` therefore refuses the whole document. This is
#: the realistic shape of the failure — not a corrupt blob, but yesterday's schema.
LEGACY_ONTOLOGY: dict[str, Any] = {
    "kind": "object-detection",
    "classes": [{"name": "seal", "tools": ["bbox"]}],
    "required_labels": ["seal"],
}


class _FakeStateManager:
    """The actor's state partition, in a dict. `try_get_state` returns (found, value) like Dapr's."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def try_get_state(self, key: str) -> tuple[bool, str | None]:
        return (key in self.store, self.store.get(key))

    async def set_state(self, key: str, value: str) -> None:
        self.store[key] = value

    async def save_state(self) -> None:
        return None


class _ProjectActor(AnnotationProjectActor):
    """The real project actor with its Dapr plumbing replaced."""

    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses Actor.__init__ (needs a runtime)
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)

    async def register_reminder(self, name: str, *args: Any, **kwargs: Any) -> None:
        return None

    async def unregister_reminder(self, name: str) -> None:
        return None


class _TaskActor(AnnotationTaskActor):
    """The real task actor with its Dapr plumbing replaced."""

    def __init__(self) -> None:  # noqa: D107 - deliberately bypasses Actor.__init__ (needs a runtime)
        self.sm = _FakeStateManager()
        self._state_manager = cast(Any, self.sm)

    async def register_reminder(self, name: str, *args: Any, **kwargs: Any) -> None:
        return None

    async def unregister_reminder(self, name: str) -> None:
        return None


def _project_with_legacy_ontology() -> tuple[_ProjectActor, str]:
    """A project actor whose PERSISTED document carries an ontology this build cannot parse.

    Hands back the project id as well: it is the id the URL and the FGA object carry, so it is the
    identifier an operator reading the refusal actually has in hand.
    """
    actor = _ProjectActor()
    project = AnnotationProject(tenant="acme", slug="charters", state=ProjectState.LABELING)
    stored = project.model_dump(mode="json")
    stored["ontology"] = LEGACY_ONTOLOGY
    actor.sm.store[PROJECT_KEY] = json.dumps(stored)
    return actor, project.project_id


def _task_with_legacy_ontology() -> _TaskActor:
    """A task actor whose PERSISTED document carries an ontology this build cannot parse."""
    actor = _TaskActor()
    stored = Task(
        task_id="t-77",
        project_id="p-charters",
        source={"kind": "chunks", "keys": ["t-77"]},
        media={"kind": "image", "image_url": "s3://b/t-77.jpg"},
    ).model_dump(mode="json")
    stored["ontology"] = LEGACY_ONTOLOGY
    actor.sm.store[TASK_KEY] = json.dumps(stored)
    return actor


@pytest.mark.asyncio
async def test_the_project_seam_names_the_project_and_the_reason() -> None:
    """`AnnotationProjectActor._load` must refuse by name, not by `pydantic.ValidationError`.

    The identifier is the whole point of the refusal: an operator reading the response has to be
    able to go from it to the one project whose document needs rewriting, without a log dive.
    """
    actor, project_id = _project_with_legacy_ontology()

    with pytest.raises(DomainError) as caught:
        await actor.get()

    detail = str(caught.value)
    assert "ontology" in detail, f"the refusal does not name what could not be read: {detail!r}"
    assert project_id in detail, f"the refusal does not name the project: {detail!r}"
    assert caught.value.extensions.get("fields") == ["ontology.required_labels"], "the refusal does not say which field to rewrite"


@pytest.mark.asyncio
async def test_the_task_seam_names_the_task_and_the_reason() -> None:
    """The task actor captures its OWN copy of the ontology, so it is a second, independent seam."""
    actor = _task_with_legacy_ontology()

    with pytest.raises(DomainError) as caught:
        await actor.get()

    detail = str(caught.value)
    assert "ontology" in detail, f"the refusal does not name what could not be read: {detail!r}"
    assert "t-77" in detail, f"the refusal does not name the task: {detail!r}"


@pytest.mark.asyncio
async def test_the_refusal_is_not_a_server_error() -> None:
    """A 5xx tells the caller to retry, and a retry cannot fix yesterday's schema.

    It also hides the failure in the noise every service's 500 counter already carries. The refusal
    is a client-visible 4xx precisely so it is legible as "this document, this build".
    """
    actor, _ = _project_with_legacy_ontology()

    with pytest.raises(DomainError) as caught:
        await actor.get()

    assert caught.value.status_code < 500, f"an unreadable stored ontology answered {caught.value.status_code}"


@pytest.mark.asyncio
async def test_a_readable_project_is_untouched() -> None:
    """The guard must be invisible to every document the current model CAN parse."""
    actor = _ProjectActor()
    await actor.create(AnnotationProject(tenant="acme", slug="charters", state=ProjectState.LABELING).model_dump(mode="json"))

    project = await actor.get()

    assert project is not None
    assert project["slug"] == "charters"


@pytest.mark.asyncio
async def test_assist_does_not_swallow_the_refusal_as_a_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_task_ontology` degrades to "no contract" on a TRANSPORT failure — and must not here.

    The two look identical at the call site (both arrive as an exception from the actor proxy) and
    mean opposite things. A sidecar that did not answer says nothing about the rules; a stored
    ontology that will not parse says the rules exist and this build cannot read them. Treating the
    second as the first is how an unenforced prediction reaches an annotator's canvas looking
    exactly like an enforced one.
    """
    import annotator.api.v1.endpoints.tasks as tasks_mod  # noqa: PLC0415 - patched by name below
    from annotator.api.v1.endpoints.assist import _task_ontology  # noqa: PLC0415 - endpoint import is heavy

    actor = _task_with_legacy_ontology()
    monkeypatch.setattr(tasks_mod, "_proxy", lambda _tid: actor)

    with pytest.raises(DomainError):
        await _task_ontology("t-77")


def _over_the_sidecar(exc: BaseException) -> Exception:
    """What the client SDK raises for an actor-side `exc`, escaping and all.

    `dapr/ext/fastapi/actor.py` answers 500 with `repr(ex)` inside a JSON body; the SDK then nests
    that body inside its own message. Two `json.dumps` reproduce the two escaping layers. The model
    is the one `services/annotator/tests/test_actor_errors_cross_the_boundary_losslessly.py`
    established for `IllegalTransition`; it is restated here because that suite is a separate pytest
    testpath and neither can import the other.
    """
    body = json.dumps({"errorCode": "ERR_ACTOR_INVOKE_METHOD", "message": repr(exc)})
    return RuntimeError(f"Dapr invocation failed: {json.dumps(body)}")


@pytest.mark.asyncio
async def test_the_refusal_survives_the_sidecar_hop() -> None:
    """In production the seam and the endpoint are on OPPOSITE sides of Dapr's one text channel.

    Without a decoder on the caller's side the named refusal is named only inside the actor pod, and
    the annotator gets exactly the anonymous 500 this change exists to remove. So the hop is the
    half that has to hold in the cluster, and it is the half a same-process test cannot see.
    """
    from annotator.projects.proxies import _translating  # noqa: PLC0415 - opens a channel at import

    actor, project_id = _project_with_legacy_ontology()
    try:
        await actor.get()
    except BaseException as raised:  # noqa: BLE001 - the seam's own refusal is the payload under test
        actor_side = raised
    else:
        raise AssertionError("the seam accepted a document the current model cannot parse")

    async def call(*_args: Any, **_kwargs: Any) -> Any:
        raise _over_the_sidecar(actor_side)

    with pytest.raises(DomainError) as caught:
        await _translating(call)()

    assert project_id in str(caught.value), "the project id did not survive the hop"
    assert caught.value.extensions.get("fields") == ["ontology.required_labels"], "the failing field did not survive the hop"


# -------------------------------------------------------------------------------------------------
# The rolling-upgrade window. Within one deployed version the actor's `_load` refuses first, so the
# endpoint-side parses below are unreachable — which is exactly why they were dead fail-open branches.
# During an upgrade that CHANGES `LabelOntology` they are reachable: the call can be served by an
# actor pod still on the OLD code, which loads and dumps its document happily and hands that dump to
# a NEW-code endpoint whose model rejects it. The seams are modelled here by an actor double that
# returns such a dump — the one shape a same-version test cannot produce.
# -------------------------------------------------------------------------------------------------


class _OldCodeActor:
    """An actor pod running the PREVIOUS build: its own model parsed the document, so it answers."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    async def get(self) -> dict[str, Any]:
        return self.document


@pytest.mark.asyncio
async def test_assist_names_the_refusal_when_an_old_pod_serves_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_task_ontology` parses the ontology sub-document itself, so it is its own seam."""
    import annotator.api.v1.endpoints.tasks as tasks_mod  # noqa: PLC0415 - patched by name below
    from annotator.api.v1.endpoints.assist import _task_ontology  # noqa: PLC0415 - endpoint import is heavy

    monkeypatch.setattr(tasks_mod, "_proxy", lambda _tid: _OldCodeActor({"task_id": "t-77", "ontology": LEGACY_ONTOLOGY}))

    with pytest.raises(DomainError) as caught:
        await _task_ontology("t-77")

    assert "t-77" in str(caught.value), f"the refusal does not name the task: {caught.value!s}"
    assert caught.value.extensions.get("fields") == ["ontology.required_labels"], "the failing field is not reported the same way at every seam"


def test_a_bulk_send_names_the_refusal_when_an_old_pod_serves_the_read() -> None:
    """`_validated_predictions` re-parses the project's ontology to check the pre-annotations.

    This is the worst of the three seams to fail open at: one bulk send is up to a thousand tasks,
    each carrying pre-annotations that publish carries through verbatim.
    """
    from annotator.api.v1.endpoints.project_events import SendItem, SendItemsRequest, _validated_predictions  # noqa: PLC0415 - endpoint import is heavy

    project = {"project_id": "p-9", "ontology": LEGACY_ONTOLOGY}
    payload = SendItemsRequest(
        items=[
            SendItem.model_validate(
                {
                    "source": {"kind": "chunks", "keys": ["k1"]},
                    "media": {"kind": "image", "image_url": "s3://b/k1.jpg"},
                    "prediction": [{"shape_type": "bbox", "label": "seal", "x": 0, "y": 0, "width": 4, "height": 4}],
                }
            )
        ]
    )

    with pytest.raises(DomainError) as caught:
        _validated_predictions(project, payload)

    assert "p-9" in str(caught.value), f"the refusal does not name the project: {caught.value!s}"
    assert caught.value.extensions.get("fields") == ["ontology.required_labels"], "the failing field is not reported the same way at every seam"
