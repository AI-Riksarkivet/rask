"""A relation the chart grants must exist on the type it grants against (LH-135's residue).

WHAT THIS WOULD HAVE CAUGHT. `bootstrap-admin` grants four rungs on the FGA root object — `owner`,
`reader`, `maintainer`, `event_stager` — and nothing checked that the authorization model defines
them. When `event_stager` was added to the model and the estate kept running a catalog image built
before it, every upgrade failed on
`Invalid tuple 'warehouse:lance_catalog#event_stager@user:service-ingest'. Reason: relation
'warehouse#event_stager' not found`. That was the only signal, it fired on every upgrade, and it was
never chased.

WHAT IT CANNOT CATCH, stated so the gate is not mistaken for more than it is: a DEPLOYED image whose
bundled model is older than the repo's. The catalog is the provisioner — it rewrites the model at boot
from its own copy — so a stale catalog is a stale model, and only the running estate can answer that.
This gate covers the other direction, which is the one a commit can get wrong: the chart naming a
relation the repo does not define.

READ OFF THE TEMPLATE, not a hand-kept list. A second list of "relations the chart grants" would be a
third thing to keep in step with the other two, and the drift it is meant to catch would simply move
into it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
_CHART_JOB = REPO / "chart/templates/bootstrap-admin.yaml"
_MODEL = REPO / "packages/service-kit/src/service_kit/governed/auth/model.json"

#: Each grant list the bootstrap job builds, and the FGA TYPE whose object it is written against.
#: Two lists, because `fga_root_object` (the estate coordinate) and `fga_default_warehouse_object`
#: (the structural root a top-level namespace hangs off) are two settings naming two types — and a
#: rung valid on one is routinely absent from the other. `maintainer` is the live example: it is a
#: warehouse rung with no estate meaning, and writing it against `estate:rask` would fail the upgrade
#: on `relation 'estate#maintainer' not found`, the same way `warehouse#event_stager` once did.
_GRANT_LISTS: dict[str, str] = {"grants": "warehouse", "estate_grants": "estate"}

#: `(subject_expr, "relation")` pairs, as the job's own grant tuples spell them.
_GRANT_PAIR = re.compile(r'\(\s*[^,()]+,\s*"([a-z_]+)"\s*\)')


def _relations_the_chart_grants(list_name: str) -> set[str]:
    """The relations in ONE of the job's grant lists, read off the template."""
    body = _CHART_JOB.read_text(encoding="utf-8")
    start = body.index(f"{list_name} = (")
    # Each list is a parenthesised expression terminated by a line that is exactly `)` at the script's
    # own indent — bounding on the NEXT list's name would make the last one run to end of file.
    end = body.index("\n              )\n", start)
    return set(_GRANT_PAIR.findall(body[start:end]))


def _relations_the_model_defines(type_name: str) -> set[str]:
    model = json.loads(_MODEL.read_text(encoding="utf-8"))
    for definition in model["type_definitions"]:
        if definition["type"] == type_name:
            return set(definition.get("relations", {}))
    raise AssertionError(f"the model defines no {type_name!r} type — this gate would pass vacuously")


@pytest.mark.parametrize(("list_name", "type_name"), sorted(_GRANT_LISTS.items()))
def test_every_relation_the_bootstrap_job_grants_exists_on_the_model(list_name: str, type_name: str) -> None:
    """The gate. A missing relation is an upgrade that fails on a tuple write, every time."""
    granted = _relations_the_chart_grants(list_name)
    assert granted, f"no grant pairs parsed out of {list_name!r} — this gate would pass vacuously"

    defined = _relations_the_model_defines(type_name)
    missing = sorted(granted - defined)

    assert not missing, (
        f"bootstrap-admin's {list_name!r} grants {missing} on {type_name!r}, which the authorization "
        f"model does not define — every upgrade will fail on 'relation {type_name}#{missing[0]} not "
        f"found'. The model defines: {sorted(defined)}"
    )


def test_the_two_model_spellings_agree_on_those_relations() -> None:
    """`model.fga` is the DSL and `model.json` is what the service actually writes.

    They are two files, and only the JSON reaches the store. A relation added to the DSL alone would
    read as present to anyone grepping the model and be absent from every estate.
    """
    dsl = (_MODEL.parent / "model.fga").read_text(encoding="utf-8")
    for list_name in _GRANT_LISTS:
        for relation in sorted(_relations_the_chart_grants(list_name)):
            assert f"define {relation}:" in dsl, f"{relation!r} is granted and is in model.json, but the DSL does not define it"


def test_the_estate_root_is_seeded_before_anything_checks_it() -> None:
    """The tuples for the estate root must already be written when a service first reads it.

    ORDER, not content — and it is the half of a root migration that has no local reproduction. The
    bootstrap hook is `post-install,post-upgrade`, so the fleet's pods are rolling before it runs. A
    release that repoints `fga_root_object` at `estate:rask` in the SAME upgrade that first seeds the
    object hands every estate door a 403 for as long as the hook takes, and the doors that matter here
    are the admin console and the whole-estate lineage projection.

    So the seeding release must precede the repointing one. While `fga_root_object` still names a
    `warehouse:`, the estate grants are inert-but-present, which is exactly what is wanted; once it
    names the estate, they are what answers.
    """
    settings = (REPO / "packages/service-kit/src/service_kit/governed/settings.py").read_text(encoding="utf-8")
    root = re.search(r'fga_root_object:\s*str\s*=\s*Field\(default="([^"]+)"', settings)
    assert root, "could not read fga_root_object's default"

    job = _CHART_JOB.read_text(encoding="utf-8")
    seeded = re.search(r'estate_obj = "([^"]+)"', job)
    assert seeded, "the bootstrap job names no estate object — the seeding half is missing"

    if root.group(1).startswith("estate:"):
        assert root.group(1) == seeded.group(1), (
            f"the fleet checks its estate privileges on {root.group(1)!r} and the bootstrap job seeds "
            f"{seeded.group(1)!r} — every estate door is denied against an object nobody grants on."
        )
