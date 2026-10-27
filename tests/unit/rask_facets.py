"""rask's custom OpenLineage facets judged against their published schemas, for the offline conformance gate and
criterion 4's live schema leg alike ([[LH-500]]).

Each schema under `spec/facets/rask/` refers to OpenLineage's `BaseFacet` by its published URL, served here from the
vendored 2-0-2 document, so the judgement needs no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012


_REPO = Path(__file__).resolve().parents[2]
_OPENLINEAGE: dict[str, Any] = json.loads((_REPO / "tests" / "data" / "openlineage-2-0-2.json").read_text())
REGISTRY: Registry[Any] = Registry().with_resource(_OPENLINEAGE["$id"], Resource.from_contents(_OPENLINEAGE, default_specification=DRAFT202012))
#: rask's custom facets, by the key each rides under, with the schema each facet's `_schemaURL` names.
SCHEMAS: dict[str, dict[str, Any]] = {
    "rask_lance": json.loads((_REPO / "spec" / "facets" / "rask" / "RaskLanceFacet.json").read_text()),
    "rask_author": json.loads((_REPO / "spec" / "facets" / "rask" / "RaskAuthorFacet.json").read_text()),
}


def rask_facet_errors(event: dict[str, Any]) -> list[str]:
    """Each `rask_lance` and `rask_author` facet, in the run's, the dataset's and every input's and output's facet bag,
    against its schema and the `_schemaURL` naming it."""
    holders = [event.get("run") or {}, event.get("dataset") or {}, *(event.get("inputs") or []), *(event.get("outputs") or [])]
    errors: list[str] = []
    for bag in (holder.get("facets") or {} for holder in holders):
        for key, schema in SCHEMAS.items():
            if key not in bag:
                continue
            validator = Draft202012Validator(schema, registry=REGISTRY, format_checker=FormatChecker())
            errors += [f"{key}: {error.message}" for error in validator.iter_errors({key: bag[key]})]
            named = bag[key].get("_schemaURL") if isinstance(bag[key], dict) else None
            if not str(named or "").startswith(f"{schema['$id']}#/$defs/"):
                errors.append(f"{key}: _schemaURL {named!r} names no definition of {schema['$id']}")
    return errors
