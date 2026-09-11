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


REPO = Path(__file__).resolve().parents[2]
_CHART_JOB = REPO / "chart/templates/bootstrap-admin.yaml"
_MODEL = REPO / "packages/service-kit/src/service_kit/governed/auth/model.json"

#: The object the bootstrap job grants against — `FGA_ROOT_OBJECT`, a `warehouse:` id.
_GRANTED_TYPE = "warehouse"

#: `(subject_expr, "relation")` pairs, as the job's own `grants` tuple spells them.
_GRANT_PAIR = re.compile(r'\(\s*[^,()]+,\s*"([a-z_]+)"\s*\)')


def _relations_the_chart_grants() -> set[str]:
    body = _CHART_JOB.read_text(encoding="utf-8")
    start = body.index("grants = (")
    end = body.index(")", body.index("stagers", start))
    return set(_GRANT_PAIR.findall(body[start : body.index("for subject_id, relation in grants", end)]))


def _relations_the_model_defines(type_name: str) -> set[str]:
    model = json.loads(_MODEL.read_text(encoding="utf-8"))
    for definition in model["type_definitions"]:
        if definition["type"] == type_name:
            return set(definition.get("relations", {}))
    raise AssertionError(f"the model defines no {type_name!r} type — this gate would pass vacuously")


def test_every_relation_the_bootstrap_job_grants_exists_on_the_model() -> None:
    """The gate. A missing relation is an upgrade that fails on a tuple write, every time."""
    granted = _relations_the_chart_grants()
    assert granted, "no grant pairs parsed out of bootstrap-admin.yaml — this gate would pass vacuously"

    defined = _relations_the_model_defines(_GRANTED_TYPE)
    missing = sorted(granted - defined)

    assert not missing, (
        f"bootstrap-admin grants {missing} on {_GRANTED_TYPE!r}, which the authorization model does not "
        f"define — every upgrade will fail on 'relation {_GRANTED_TYPE}#{missing[0]} not found'. "
        f"The model defines: {sorted(defined)}"
    )


def test_the_two_model_spellings_agree_on_those_relations() -> None:
    """`model.fga` is the DSL and `model.json` is what the service actually writes.

    They are two files, and only the JSON reaches the store. A relation added to the DSL alone would
    read as present to anyone grepping the model and be absent from every estate.
    """
    dsl = (_MODEL.parent / "model.fga").read_text(encoding="utf-8")
    for relation in sorted(_relations_the_chart_grants()):
        assert f"define {relation}:" in dsl, f"{relation!r} is granted and is in model.json, but the DSL does not define it"
