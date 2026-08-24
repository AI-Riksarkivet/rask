"""A lane declared in the natural spelling must be reachable.

THE DEFECT, proven live 2026-08-24 and then reproduced by changing ONE field.

Two spellings of one dataset name exist, and the two halves of the cascade disagree about which
they use:

* `publication_trigger.py` publishes the arrival TENANT-STRIPPED -- `dataset` is
  ``f"{source}{DELIMITER}{table}"`` where `source` has already had the project prefix removed, so a
  publication of ``table:acme-bronze$agnostic`` arrives as ``bronze$agnostic``.
* A `TransformSpec.from_id` written through the catalog's lane door is CATALOG-QUALIFIED --
  ``acme-bronze$agnostic``. That is the form the door validates, the record stores, the list
  renders, and every other surface in the estate shows.

`transform.py`'s guard compares the arrival against `{settings.from_dataset} | {from_id}`, so the
two never meet and every arrival for a declared lane is DROPped as another lane's.

WHY IT SURVIVED: the miss is silent in both directions. `medallion_publication_not_a_lane` logs at
DEBUG and acks; the mover's drop logs at INFO and acks. A lane that can NEVER fire is
indistinguishable from one that simply has no data -- the same failure shape as MEDALLION_LANE being
rendered by no chart template (`706c8ce3`) and the lineage link that denied its own run
(`78812a5b`).

MEASURED, same lane, same ingest, one field changed:

    from_id = acme-bronze$agnostic  ->  mover logs NOTHING, cascade dead
    from_id = bronze$agnostic       ->  publish 200, quality_blocked, held_for_review

THE FIX IS ONE CONVENTION, not a widened guard. A trigger's ``dataset`` is a LANE KEY -- tenant-free,
identical for every tenant, with the tenant on ``trigger.project``. A ``TransformSpec.from_id`` is a
CATALOG IDENTIFIER, because it must resolve at ``/v1/table/{id}``. Comparing one against the other was
a type error. `ingest_trigger`'s declared branch -- the ONE publisher that broke the convention -- now
emits a lane key like its own sibling branch, and the guard compares lane keys only, converting the
record's id once through the shared ``lane_key`` helper.
"""

from __future__ import annotations

from medallion.services.transform import accepted_input_names

from service_kit.lakehouse.transform_specs import TransformSpec


def _spec(from_id: str) -> TransformSpec:
    return TransformSpec.model_validate(
        {
            "lane": "agnostic",
            "project": "acme",
            "from_id": from_id,
            "to_id": "acme-silver$agnostic",
            "entrypoint": "python /home/ray/jobs/ray_stage_job.py",
            "params": {},
            "code_version": "",
        }
    )


def test_a_catalog_qualified_lane_accepts_the_tier_qualified_arrival() -> None:
    """The defect. `bronze$agnostic` is what the publication head actually sends."""
    accepted = accepted_input_names(env_from_dataset="bronze$events", declared=_spec("acme-bronze$agnostic"))
    assert "bronze$agnostic" in accepted, (
        "a lane declared as acme-bronze$agnostic is unreachable: the publication head publishes the "
        "tenant-stripped bronze$agnostic and the guard never matches it"
    )


def test_the_catalog_id_is_NOT_a_lane_key() -> None:
    """The type distinction, pinned. A catalog id in this set is what caused the defect.

    ``acme-bronze$agnostic`` is what you pass to ``/v1/table/{id}``; it is never what a trigger
    carries. Accepting it here would re-admit the ambiguity the fix removes.
    """
    accepted = accepted_input_names(env_from_dataset="bronze$events", declared=_spec("acme-bronze$agnostic"))
    assert "acme-bronze$agnostic" not in accepted
    assert accepted == {"bronze$events", "bronze$agnostic"}


def test_the_env_dataset_survives_a_declared_lane() -> None:
    """An estate mid-migration has both; neither may be dropped."""
    accepted = accepted_input_names(env_from_dataset="bronze$events", declared=_spec("acme-bronze$agnostic"))
    assert "bronze$events" in accepted


def test_an_undeclared_lane_is_env_only_byte_for_byte() -> None:
    """Declaring nothing must behave exactly as before -- the estate's standing rule."""
    assert accepted_input_names(env_from_dataset="bronze$events", declared=None) == {"bronze$events"}


def test_an_already_tier_qualified_record_is_not_double_stripped() -> None:
    """`bronze$agnostic` has no project prefix; stripping again would corrupt it."""
    accepted = accepted_input_names(env_from_dataset="bronze$events", declared=_spec("bronze$agnostic"))
    assert "bronze$agnostic" in accepted
    assert not any(name.startswith("$") for name in accepted)


def test_a_project_that_is_a_prefix_of_the_namespace_is_not_mangled() -> None:
    """`acme` vs a namespace legitimately starting with `acme` but not `acme-`.

    PROJECT_PATTERN permits hyphens, so prefix-stripping is only sound on the `<project>-` boundary.
    Stripping on the bare project name would turn `acmebronze$x` into `bronze$x` and match a lane
    that was never declared.
    """
    accepted = accepted_input_names(env_from_dataset="bronze$events", declared=_spec("acmebronze$x"))
    assert "acmebronze$x" in accepted
    assert "bronze$x" not in accepted


def test_the_shared_helper_strips_only_on_the_project_boundary() -> None:
    """`lane_key` is the sound inverse of `project_namespace`, and only on the `-` boundary.

    The unsound inverse -- recovering a project FROM a name -- is ruled out by the estate's canon,
    because PROJECT_PATTERN permits hyphens. Here the project is KNOWN, so the only judgement is a
    prefix test; doing it on the bare project name would turn `acmebronze$x` into `bronze$x`.
    """
    from service_kit.lakehouse.warehouse_registry import lane_key, project_namespace

    assert lane_key("acme", "acme-bronze$events") == "bronze$events"
    assert lane_key("acme", "acmebronze$x") == "acmebronze$x"
    assert lane_key("", "bronze$events") == "bronze$events"
    assert lane_key("acme", "bronze$events") == "bronze$events"
    # round-trip: qualify then de-qualify is identity for a known project
    for name in ("bronze$events", "silver$features", "gold$catalog"):
        assert lane_key("acme", project_namespace("acme", name)) == name
