"""B4 — a stage's transform identity, and the resume predicate that reads it.

The capability is "re-run only the rows whose transform has changed". It needs three things: an
identity for the transform, that identity written WITH the data, and a resume predicate that compares
them. This covers the first and pins the property the other two depend on — the identity moves when,
and only when, the transform does.

It is derived from the stage's DECLARATION plus the two things a declaration cannot see: the actor
class the composition root actually bound, and the runner env. A stage whose Python changed but whose
declaration did not is still a different transform, and the qualname is what carries that.
"""

from __future__ import annotations

from ratch.core.registry import ActorConfig, Stage, StageShape


def _stage(**overrides: object) -> Stage:
    """Build a stage from a baseline plus overrides.

    `model_validate` rather than `Stage(**merged)`: the merged mapping is `str -> object` by
    construction, and splatting it into the typed constructor is an unsound call the checker is right
    to reject. Validation takes a mapping, which is exactly what this is.
    """
    base: dict[str, object] = {
        "name": "embed",
        "shape": StageShape.SCAN_COLUMN,
        "table": "chunks",
        "read_columns": ("text",),
        "key_columns": ("id",),
        "output_columns": ("vector",),
    }
    return Stage.model_validate({**base, **overrides})


def test_identity_is_stable_for_an_unchanged_stage() -> None:
    assert _stage().identity() == _stage().identity()


def test_identity_moves_when_the_declaration_moves() -> None:
    baseline = _stage().identity()
    assert _stage(read_columns=("text", "title")).identity() != baseline
    assert _stage(output_columns=("embedding",)).identity() != baseline
    assert _stage(runner="topics").identity() != baseline
    assert _stage(actor=ActorConfig(num_gpus=1.0)).identity() != baseline


def test_identity_moves_when_the_bound_actor_changes() -> None:
    """The declaration cannot see the actor class; a swapped implementation is still a new transform."""
    stage = _stage()
    assert stage.identity(actor_qualname="A") != stage.identity(actor_qualname="B")


def test_identity_moves_when_the_runner_env_changes() -> None:
    stage = _stage()
    assert stage.identity(runner_env="torch==2.4") != stage.identity(runner_env="torch==2.5")


def test_identity_is_short_enough_to_store_per_row() -> None:
    """It is written on every row, so a full sha256 hex would be 64 bytes of repetition per row."""
    value = _stage().identity()
    assert len(value) == 16 and value.isalnum()


def test_key_columns_do_not_change_the_transform() -> None:
    """Identity answers "would this row compute differently"; addressing is not part of that."""
    assert _stage(key_columns=("id", "shard")).identity() == _stage().identity()
