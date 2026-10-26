"""A classified column is never vended raw, however a writer reshapes the table ([[LH-058]], [[LH-207]]).

A vended credential's unit is an object, and Lance stores columns written together in one data file, so a table carrying
a label on any field is served through the catalog and never handed a direct credential. Both vend doors answer it:
``/management/v1/table/{id}/credentials`` and the spec's ``DescribeTable(vend_credentials=true)``. A writer holding only
``can_write_data`` must not be able to turn a classified table into a directly vendable one. Measured on pylance 13.0.0:
a same-type ``alter_columns`` re-mints the field id and drops its metadata, ``add_columns({"leak": "secret"})`` copies
the values unlabelled, and ``drop_columns`` leaves every earlier version readable from the same data file.

A credential for any ref reads every ref's files: a stock client opens a branch with the table's credential
(``checkout_version((branch, None))``), and a branch reads its parent's fragments through a base on the table root
(`lance_docs/lancemultibasebranchingblobv2.md` § Building block 3). So a label on any ref holds the table back.

Every decision is the real model's, on a store of this test's own: alice owns ``db`` and ``db$t`` and may apply the
label ``pii``; bob is a writer on ``db``. The vendor records every call, since a credential minted and then discarded is
still a grant at the store.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest

from catalog.api.dependencies import get_vendor
from catalog.schemas import VendedCredentials
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


#: The Lance Namespace spec's error codes a stock client reads (`spec.yaml`).
INVALID_INPUT, PERMISSION_DENIED = 13, 15
TABLE = "/v1/table/db$t"
PII = {"rask.classification": "pii"}


class _Vendor:
    """Records every credential it is asked for: the claim is that it is never asked."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def vend(self, **kwargs: Any) -> VendedCredentials:  # noqa: ANN401 — the vendor protocol's keyword arguments
        self.calls.append(kwargs)
        return VendedCredentials(storage_options={"aws_access_key_id": "AK", "aws_secret_access_key": "SK"})


@pytest.fixture
def table(governed_catalog: Any) -> Iterator[tuple[Any, _Vendor]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's ``db$t`` (``id``, ``secret``, ``payload.ssn``), bob a writer on ``db``, and the recording vendor."""
    catalog = governed_catalog
    vendor = _Vendor()
    catalog.client.app.dependency_overrides[get_vendor] = lambda: vendor
    catalog.store.write((catalog.person("alice"), "apply", "classification:pii"))
    alice = catalog.bearer("alice")
    assert catalog.client.post("/v1/namespace/db/create", json={}, headers=alice).status_code == 200
    rows = pa.table({"id": pa.array([1, 2], pa.int64()), "secret": pa.array(["a", "b"]), "payload": pa.array([{"ssn": "x"}, {"ssn": "y"}])})
    created = catalog.client.post(f"{TABLE}/create", content=encode_arrow_stream(rows), headers={**alice, "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    granted = catalog.client.post("/management/v1/namespace/db/access/grant", json={"user": "dex~bob", "relation": "writer"}, headers=alice)
    assert granted.status_code == 200, granted.text
    yield catalog, vendor
    catalog.client.app.dependency_overrides.pop(get_vendor, None)


def _label(catalog: Any, path: str, *, branch: str | None = None) -> None:  # noqa: ANN401
    body: dict[str, Any] = {"updates": [{"path": path, "metadata": PII}], **({"branch": branch} if branch else {})}
    labelled = catalog.client.post(f"{TABLE}/update_field_metadata", json=body, headers=catalog.bearer("alice"))
    assert labelled.status_code == 200, labelled.text


def _bob(catalog: Any, door: str, body: dict[str, Any]) -> tuple[int, int | None]:  # noqa: ANN401
    answered = catalog.client.post(f"{TABLE}/{door}", json=body, headers=catalog.bearer("bob"))
    return answered.status_code, answered.json().get("code") if answered.status_code != 200 else None


def _vends_raw(catalog: Any) -> list[str]:  # noqa: ANN401
    """The doors that handed bob a direct credential for the table."""
    raw = []
    door = catalog.client.post("/management/v1/table/db$t/credentials", headers=catalog.bearer("bob"))
    assert door.status_code == 200, door.text
    if door.json().get("mode") != "server_mediated":
        raw.append("credentials")
    described = catalog.client.post(f"{TABLE}/describe", json={"vend_credentials": True}, headers=catalog.bearer("bob"))
    assert described.status_code == 200, described.text
    if "aws_access_key_id" in (described.json().get("storage_options") or {}):
        raw.append("describe")
    return raw


@pytest.mark.parametrize(
    ("label", "branch", "laundering", "refused"),
    [
        pytest.param("secret", None, [("drop_columns", {"columns": ["secret"]})], [(400, INVALID_INPUT)], id="a-writer-drops-the-labelled-column"),
        pytest.param(
            "secret",
            None,
            [("alter_columns", {"alterations": [{"path": "secret", "data_type": {"type": "utf8"}}]})],
            [(400, INVALID_INPUT)],
            id="a-writer-recasts-it-to-the-same-type",
        ),
        pytest.param(
            "secret",
            None,
            [("add_columns", {"new_columns": [{"name": "leak", "expression": "secret"}]}), ("drop_columns", {"columns": ["secret"]})],
            [(403, PERMISSION_DENIED), (400, INVALID_INPUT)],
            id="a-writer-copies-it-then-drops-it",
        ),
        pytest.param("secret", None, [("update", {"updates": [["id", "length(secret)"]]})], [(403, PERMISSION_DENIED)], id="an-update-reads-it"),
        pytest.param(
            "secret",
            None,
            [
                ("add_columns", {"new_columns": [{"name": "a'b", "expression": "id"}]}),
                ("add_columns", {"new_columns": [{"name": "leak", "expression": "concat(`a'b`, secret, 'x')"}]}),
            ],
            [(200, None), (403, PERMISSION_DENIED)],
            id="a-copy-hides-behind-a-quote-in-a-column-name",
        ),
        pytest.param(
            "secret", None, [("alter_columns", {"alterations": [{"path": "secret", "rename": "taxpayer_id"}]})], [(200, None)], id="a-writer-renames-it"
        ),
        pytest.param("payload.ssn", None, [], [], id="the-label-is-on-a-nested-field"),
        pytest.param("secret", "work", [], [], id="the-label-is-on-a-branch"),
    ],
)
def test_a_classified_table_stays_unvendable_raw_whatever_a_writer_does(
    table: tuple[Any, _Vendor],
    label: str,
    branch: str | None,
    laundering: list[tuple[str, dict[str, Any]]],
    refused: list[tuple[int, int]],  # noqa: ANN401
) -> None:
    catalog, vendor = table
    if branch:
        made = catalog.client.post(f"{TABLE}/branches/create", json={"name": branch}, headers=catalog.bearer("alice"))
        assert made.status_code == 200, made.text
    _label(catalog, label, branch=branch)

    answers = [_bob(catalog, door, body) for door, body in laundering]

    assert (answers, _vends_raw(catalog), vendor.calls) == (refused, [], [])


def test_a_writer_cannot_create_a_table_carrying_a_label(table: tuple[Any, _Vendor]) -> None:
    # A label decides how a table's bytes leave the store, so setting one at create asks what setting one later asks.
    catalog, _ = table
    field = pa.field("secret", pa.string(), metadata={b"rask.classification": b"pii"})
    rows = encode_arrow_stream(pa.table({"secret": pa.array(["a"])}, schema=pa.schema([field])))

    created = catalog.client.post("/v1/table/db$u/create", content=rows, headers={**catalog.bearer("bob"), "Content-Type": ARROW_STREAM_MEDIA_TYPE})

    assert (created.status_code, created.json().get("code")) == (403, PERMISSION_DENIED), created.text
