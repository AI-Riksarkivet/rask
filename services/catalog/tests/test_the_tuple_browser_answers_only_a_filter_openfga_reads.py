"""The estate admin's tuple browser answers a filter OpenFGA's Read takes and refuses the rest naming what to add ([[LH-505]]).

``GET /management/v1/access/tuples`` pages the raw tuple store. OpenFGA's Read requires an object type whenever a
tuple filter is sent, and an object id or a user beside it: measured on OpenFGA v1.18.3, a Read naming
``classification:`` and no user is refused "the 'tuple_key' field was provided but the object type field is required and
both the object id and user cannot be empty". The fail-closed wrapper reports that refusal as 503 code 17 on a healthy
server, which is what the estate admin got for ``?object_type=classification`` and ``?object_type=table`` live on helm
rev 46. So the door refuses an object type alone and a user alone itself, 400 code 13 naming the half to add, and hands
OpenFGA only a filter its Read answers.

Every answer is the real OpenFGA's, on a store of this test's own. The estate admin's ``owner`` tuple on the estate is
seeded as the chart's bootstrap hook seeds it; the label tuples are written through the raw-tuple door the browser
reads them back from.
"""

from __future__ import annotations

from typing import Any

import pytest

from service_kit.governed import fga


TUPLES = "/management/v1/access/tuples"
#: The Lance Namespace spec's code for a refused input, the one a stock client reads (`spec.yaml`).
INVALID_INPUT = 13
PROBLEM = "application/problem+json"

ROOT_OWNS_THE_ESTATE = (fga.user_object("dex~root"), "owner", "estate:rask")
ALICE_CURATES_PII = (fga.user_object("dex~alice"), "curator", "classification:pii")
BOB_APPLIES_PII = (fga.user_object("dex~bob"), "apply", "classification:pii")
ALICE_APPLIES_RESTRICTED = (fga.user_object("dex~alice"), "apply", "classification:restricted")
STEWARDS_APPLY_RESTRICTED = ("role:stewards#assignee", "apply", "classification:restricted")

TYPE_ALONE = (
    "a type-only filter is not supported: OpenFGA's Read API needs an object id or a user beside an object type — "
    "add user= (that subject's tuples of this type) or object= (one object's tuples)"
)
USER_ALONE = (
    "a user-only filter is not supported: OpenFGA's Read API requires an object type whenever a tuple filter is sent — "
    "add object_type= (that subject's tuples of one type) or object= (one object's tuples)"
)
NOT_A_TYPE = "'gizmo' is not a type the authorization model defines"


@pytest.fixture
def labelled(governed_catalog: Any) -> Any:  # noqa: ANN401 — the conftest's GovernedCatalog
    """The estate with two labels: alice curates ``pii`` and bob applies it; alice and the stewards role apply ``restricted``."""
    catalog = governed_catalog
    catalog.store.write(ROOT_OWNS_THE_ESTATE)
    for user, relation, obj in (ALICE_CURATES_PII, BOB_APPLIES_PII, ALICE_APPLIES_RESTRICTED, STEWARDS_APPLY_RESTRICTED):
        written = catalog.client.post(TUPLES, json={"user": user, "relation": relation, "object": obj}, headers=catalog.bearer("root"))
        assert written.status_code == 200, written.text
    return catalog


@pytest.mark.parametrize(
    ("query", "tuples"),
    [
        pytest.param({"object": "classification:pii"}, {ALICE_CURATES_PII, BOB_APPLIES_PII}, id="one-object"),
        pytest.param({"user": "dex~alice", "object_type": "classification"}, {ALICE_CURATES_PII, ALICE_APPLIES_RESTRICTED}, id="a-subject-on-one-type"),
        pytest.param({"user": "role:stewards#assignee", "object_type": "classification"}, {STEWARDS_APPLY_RESTRICTED}, id="a-userset-on-one-type"),
        pytest.param(
            {},
            {ROOT_OWNS_THE_ESTATE, ALICE_CURATES_PII, BOB_APPLIES_PII, ALICE_APPLIES_RESTRICTED, STEWARDS_APPLY_RESTRICTED},
            id="the-whole-store",
        ),
    ],
)
def test_a_filter_openfga_reads_answers_the_tuples_it_names(
    labelled: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    query: dict[str, str],
    tuples: set[tuple[str, str, str]],
) -> None:
    answered = labelled.client.get(TUPLES, params=query, headers=labelled.bearer("root"))

    assert answered.status_code == 200, answered.text
    assert {(t["user"], t["relation"], t["object"]) for t in answered.json()["tuples"]} == tuples


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        pytest.param({"object_type": "classification"}, TYPE_ALONE, id="a-type-alone"),
        pytest.param({"user": "dex~alice"}, USER_ALONE, id="a-subject-alone"),
        pytest.param({"user": "dex~alice", "object_type": "gizmo"}, NOT_A_TYPE, id="a-type-the-model-does-not-define"),
    ],
)
def test_a_filter_openfga_cannot_read_is_refused_naming_what_to_add(
    labelled: Any,  # noqa: ANN401 — the conftest's GovernedCatalog
    query: dict[str, str],
    detail: str,
) -> None:
    refused = labelled.client.get(TUPLES, params=query, headers=labelled.bearer("root"))

    body = refused.json()
    assert (refused.status_code, refused.headers["content-type"], body["code"], body["detail"]) == (400, PROBLEM, INVALID_INPUT, detail)
