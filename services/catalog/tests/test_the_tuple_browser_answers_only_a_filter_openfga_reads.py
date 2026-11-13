"""The estate admin's tuple browser answers a filter OpenFGA's Read takes and refuses the rest naming what to add ([[LH-505]]).

``GET /management/v1/access/tuples`` pages the raw tuple store. OpenFGA's Read requires an object type whenever a
tuple filter is sent, and an object id or a user beside it: measured on OpenFGA v1.18.3, a Read naming
``classification:`` and no user is refused "the 'tuple_key' field was provided but the object type field is required and
both the object id and user cannot be empty". The fail-closed wrapper reports that refusal as 503 code 17 on a healthy
server, which is what the estate admin got for ``?object_type=classification`` and ``?object_type=table`` live on helm
rev 46. So the door refuses an object type alone and a user alone itself, 400 code 13 naming the half to add, and hands
OpenFGA only a filter its Read answers.

A table or namespace is held by its uuid (``table:<uuid>``, [[LH-524]]), so the browser answers each such object's
current name beside it, and its ``object`` filter, like every door, takes the object by its name or its uuid
([[LH-591]]).

Every answer is the real OpenFGA's, on a store of this test's own. The estate admin's ``owner`` tuple on the estate is
seeded as the chart's bootstrap hook seeds it; the label tuples are written through the raw-tuple door the browser
reads them back from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed import fga


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


_WAREHOUSE = "warehouse:lance_catalog"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


def _ok(catalog: Any, path: str, caller: str = "alice", **kwargs: Any) -> dict[str, Any]:
    answer = catalog.client.post(path, headers={**catalog.bearer(caller), **kwargs.pop("headers", {})}, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.status_code} {answer.text}"
    return answer.json()


def _uuid(catalog: Any, kind: str, name: str) -> str:
    return str(_ok(catalog, f"/v1/{kind}/{name}/describe", json={})["properties"]["rask.uuid"])


@pytest.fixture
def named(governed_catalog: Any, tmp_path: Path) -> Any:
    """alice's namespace ``db`` holding ``db$inner``, a table ``db$t`` the catalog made and renamed ``db$u``, and a table
    ``db$reg`` registered at a location of its registrar's (``ext/reg``, which names no uuid). The raw door grants bob
    ``reader`` on ``db$t`` by name, and grants and revokes carol the same by name."""
    catalog = governed_catalog
    catalog.store.write(ROOT_OWNS_THE_ESTATE)
    _ok(catalog, "/v1/namespace/db/create", json={})
    _ok(catalog, "/v1/namespace/db$inner/create", json={})
    _ok(catalog, "/v1/table/db$t/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    lance.write_dataset(_ROWS, str(tmp_path / "ext" / "reg"), data_storage_version="2.2", enable_stable_row_ids=True)
    _ok(catalog, "/v1/table/db$reg/register", json={"location": "ext/reg"})
    for door, user in (("post", "bob"), ("post", "carol"), ("delete", "carol")):
        grant = {"user": catalog.person(user), "relation": "reader", "object": "table:db$t"}
        answered = catalog.client.request(door.upper(), TUPLES, json=grant, headers=catalog.bearer("root"))
        assert answered.status_code == 200, answered.text
    _ok(catalog, "/v1/table/db$t/rename", json={"new_table_name": "u"})
    return catalog


def test_the_browser_names_each_table_and_namespace_as_it_is_named_now_and_filters_by_name(named: Any) -> None:
    table, registered = f"table:{_uuid(named, 'table', 'db$u')}", f"table:{_uuid(named, 'table', 'db$reg')}"
    namespace, inner = f"namespace:{_uuid(named, 'namespace', 'db')}", f"namespace:{_uuid(named, 'namespace', 'db$inner')}"

    page = named.client.get(TUPLES, headers=named.bearer("root"))

    assert page.status_code == 200, page.text
    alice, bob = named.person("alice"), named.person("bob")
    assert {(t["user"], t["relation"], t["object"], t["name"]) for t in page.json()["tuples"]} == {
        (fga.user_object("dex~root"), "owner", "estate:rask", None),
        (alice, "owner", namespace, "db"),
        (_WAREHOUSE, "parent", namespace, "db"),
        (namespace, "child", _WAREHOUSE, None),
        (alice, "owner", inner, "db$inner"),
        (namespace, "parent", inner, "db$inner"),
        (inner, "child", namespace, "db"),
        (alice, "owner", table, "db$u"),
        (bob, "reader", table, "db$u"),
        (namespace, "parent", table, "db$u"),
        (table, "child", namespace, "db"),
        (alice, "owner", registered, "db$reg"),
        (namespace, "parent", registered, "db$reg"),
        (registered, "child", namespace, "db"),
    }
    by_name = named.client.get(TUPLES, params={"object": "table:db$u"}, headers=named.bearer("root")).json()
    assert by_name == named.client.get(TUPLES, params={"object": table}, headers=named.bearer("root")).json()
    assert {t["user"] for t in by_name["tuples"]} == {alice, bob, namespace}
