"""`CompactionPlanRequest`'s schema hook publishes `batch_size`/`num_threads` as required non-null integers.

The model keeps both optional so a missing bound is the dataplane's 400 rather than FastAPI's 422, and
the hook is what makes the SCHEMA say what the door enforces. It is asserted on the model's own JSON
Schema here: FastAPI's OpenAPI generation drops `default: null` by itself (measured 2026-09-25), so the
published-contract test in `tests/integration/test_the_compaction_doors_hand_work_to_a_worker.py` cannot
see whether the hook removes it.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from catalog.schemas import CompactionPlanRequest, _publish_the_executor_bounds_as_required


@pytest.mark.parametrize("bound", ["batch_size", "num_threads"])
def test_a_bound_is_published_as_a_required_non_null_integer_with_no_default(bound: str) -> None:
    schema = CompactionPlanRequest.model_json_schema()
    published = schema["properties"][bound]

    assert bound in schema["required"], schema["required"]
    assert published["type"] == "integer", published
    assert "anyOf" not in published and "default" not in published, f"{bound} is published as optional: {published}"


def test_a_field_the_model_already_requires_keeps_its_required_entry() -> None:
    """The hook adds the two bounds to ``required``; a field Pydantic already requires must stay in it."""

    class _Plan(BaseModel):
        model_config = ConfigDict(json_schema_extra=_publish_the_executor_bounds_as_required)

        table: str
        batch_size: int | None = Field(default=None, ge=1)
        num_threads: int | None = Field(default=None, ge=1)

    assert _Plan.model_json_schema()["required"] == ["table", "batch_size", "num_threads"]
