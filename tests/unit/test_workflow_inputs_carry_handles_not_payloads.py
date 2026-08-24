"""A workflow's input and output carry HANDLES, not data.

Dapr persists every activity result into workflow history: it is written on completion, replayed on
recovery, and re-read once per dependent. So a large value in a workflow model is not "a big
argument" — it is a row in an append-only log that is read many times and never shrinks. B9 exists
because of exactly this, and `services/flows` still writes a 256 KiB `payload_text` into history as
an output and again per dependent.

The medallion is currently correct BY CONSTRUCTION rather than by rule: `StageJobSpec` carries
`from_uri` / `to_uri`, so the workflow moves pointers and the bytes stay in Lance. This test makes
that a rule, so the next field added to a workflow model has to be a handle or argue its way past a
name.

WHY A NAME CHECK AND NOT A SIZE CHECK. A size can only be measured at runtime with real data, which
is the measurement B9 is blocked on. A field NAME is available now and catches the class of mistake
at the moment it is introduced — `payload: bytes` never reaches production to be measured. The two
are complements: this stops the obvious case today, B9 sets the ceiling for the rest.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel


#: Field names that mean "the data itself" rather than "where the data is".
_PAYLOAD_NAMES = frozenset({"payload", "data", "rows", "records", "batch", "content", "body", "blob", "bytes_", "buffer"})

#: Types that carry bulk. `str` is deliberately ABSENT — every id, uri and token is a str, so
#: banning it would ban the handles this rule exists to encourage.
_BULK_TYPES = ("bytes", "bytearray", "memoryview")


def _workflow_models() -> list[type[BaseModel]]:
    """Every Pydantic model that crosses a Dapr Workflow boundary in the medallion."""
    from medallion import workflow

    return [obj for obj in vars(workflow).values() if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel]


def test_at_least_one_workflow_model_is_inspected() -> None:
    """A rule that silently inspects nothing is not a rule — this fails if the import moves."""
    assert _workflow_models(), "no workflow models found; the discovery above went stale"


@pytest.mark.parametrize("model", _workflow_models(), ids=lambda m: m.__name__)
def test_no_workflow_model_carries_bulk(model: type[BaseModel]) -> None:
    """A handle names WHERE the data is; a payload IS the data and must not enter history."""
    offenders: list[str] = []
    for name, field in model.model_fields.items():
        annotation = str(field.annotation)
        if name in _PAYLOAD_NAMES or any(t in annotation for t in _BULK_TYPES):
            offenders.append(f"{name}: {annotation}")

    assert not offenders, (
        f"{model.__name__} carries {offenders} across a workflow boundary. Dapr writes every "
        "activity result into workflow history — persisted, replayed on recovery, re-read per "
        "dependent — so this grows an append-only log with data that belongs in Lance. Carry a URI "
        "or a table id and let the activity read it."
    )
