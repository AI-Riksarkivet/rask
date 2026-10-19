"""The annotator, driven from its own release, works the lakehouse through its public doors only (XC-167, ADR 0171).

The annotator is a client of the lakehouse like Label Studio would be: it keeps its projects, tasks and roles in its own
state, reads a governed media table through the catalog as the person on the credential the catalog vends them, and
publishes into the tenant's silver as its own ServiceAccount, which a project admin granted writer there through the
catalog's grant door. One drive, against the deployed estate:

* a scratch tenant's admin seeds a governed media dataset in bronze (a chunks table stamped with its descriptor, and an
  empty annotations table), grants the annotating person writer on it, and grants the annotator's account writer on
  the tenant's silver and read on its inputs;
* the admin creates a project over the dataset; the annotating person, a member of no tenant, is refused it until the
  admin grants them a role on the project, which is the annotator's own decision;
* the person saves an annotation on the corpus, which the annotator writes through the catalog on the person's own
  token, so the commit is theirs;
* the admin sends the item, assigns it to the person, who drafts and submits it; the admin accepts it, freezes the
  project and publishes it, and the annotator's account commits the published table for the admin, who is told;
* every table the drive wrote is read back through the catalog with pylance's stock client.

Run with `bash scripts/e2e_live.sh -m annotator --require-live` against an estate the annotator's release is installed
beside (`make k3s-annotator`, which `make k3s-up` runs after rask); the e2e-stack lane installs it after rask too and runs
this module on every push.
"""

from __future__ import annotations

import io
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import byo_scenario
import lance
import pyarrow as pa
import pytest
import requests
import scratch_tenant
from correctness import rask_properties
from pydantic import BaseModel
from scratch_tenant import CATALOG, Identity, Tenant, ok

from annotator.annotations.schema import EMPTY_SCHEMA
from service_kit.lancekit.descriptor import DESCRIPTOR_METADATA_KEY


ANNOTATOR = os.environ.get("LANCE_E2E_ANNOTATOR_URL", "").rstrip("/")
RELEASE = os.environ.get("LANCE_E2E_RELEASE", "rask")
RELEASE_NAMESPACE = os.environ.get("LANCE_E2E_RELEASE_NAMESPACE", "default")
#: The principal the catalog verifies the annotator's projected ServiceAccount token as.
ANNOTATOR_ACCOUNT = os.environ.get("LANCE_E2E_ANNOTATOR_PRINCIPAL", f"kubernetes~system:serviceaccount:{RELEASE_NAMESPACE}:{RELEASE}-sa-annotator")
SETTLE_SECONDS = float(os.environ.get("LANCE_E2E_SETTLE_SECONDS", "180"))

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.annotator,
    pytest.mark.skipif(
        not (ANNOTATOR and CATALOG and scratch_tenant.DEX and byo_scenario.NOTIFICATIONS),
        reason="LANCE_E2E_ANNOTATOR_URL, LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX and LANCE_E2E_NOTIFICATIONS_URL",
    ),
]

#: The media dataset's declared row identity, and the one searchable table that carries it.
DESCRIPTOR = {"identity": {"key_fields": ["doc_id", "speech_id", "chunk_id"]}, "searches": [{"name": "default", "row_table": "chunks"}]}
IDENTITY = [("doc_id", pa.string()), ("speech_id", pa.int64()), ("chunk_id", pa.int64())]
UNIT = ("d1", 0, 0)
LABEL = "seal"


class Drive(BaseModel):
    """What the drive did and what came of it, read once it ended."""

    project_id: str
    #: The annotating person's answer for the project before and after the admin granted them a role on it.
    refused: int
    admitted: int
    #: The corpus's annotations table, read back as the person, and who the catalog announced each write to it as.
    annotations: list[dict[str, Any]]
    annotation_authors: list[str]
    #: The version of the corpus's row table the person selected the item from.
    selected: int
    #: The published table, read back as the admin, and what its commit carries.
    published_table: str
    published: list[dict[str, Any]]
    published_commit: dict[str, Any]
    #: The admin's inbox rows since the drive began, as (reason, object).
    told: list[tuple[str, str]]


def _stream(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _create_table(admin: Identity, table_id: str, table: pa.Table) -> None:
    answer = admin.http.post(
        f"{CATALOG}/v1/table/{table_id}/create", data=_stream(table), headers={"content-type": "application/vnd.apache.arrow.stream"}, timeout=60
    )
    ok(answer)


def _seed_corpus(tenant: Tenant) -> None:
    """The governed media dataset in bronze: chunks stamped with the descriptor, and an empty annotations table."""
    chunks = pa.Table.from_pylist(
        [{"doc_id": UNIT[0], "speech_id": UNIT[1], "chunk_id": UNIT[2], "text": "a line"}],
        schema=pa.schema([*IDENTITY, ("text", pa.string())], metadata={DESCRIPTOR_METADATA_KEY: json.dumps(DESCRIPTOR).encode()}),
    )
    annotations = pa.schema([*IDENTITY, *EMPTY_SCHEMA]).empty_table()
    for name, table in (("chunks", chunks), ("annotations", annotations)):
        table_id = f"{tenant.bronze}${name}"
        _create_table(tenant.admin, table_id, table)
        tenant.tables.append(table_id)


def _grant(admin: Identity, namespace: str, user: str, relation: str) -> None:
    ok(admin.http.post(f"{CATALOG}/management/v1/namespace/{namespace}/access/grant", json={"user": user, "relation": relation}))


def _annotator(identity: Identity) -> requests.Session:
    session = requests.Session()
    session.headers["authorization"] = f"Bearer {identity.bearer}"
    return session


def _answer(response: requests.Response, expected: int = 200) -> dict[str, Any]:
    assert response.status_code == expected, f"{response.request.method} {response.request.url} -> {response.status_code}: {response.text[:500]}"
    return response.json()


def _commit(identity: Identity, table_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A table's rows and the `rask.*` properties of its newest commit, read through the catalog by ``identity``."""
    dataset = lance.dataset(namespace_client=identity.namespace(), table_id=table_id.split("$"))
    transaction = dataset.read_transaction(dataset.version)
    assert transaction is not None, f"{table_id} version {dataset.version} has no transaction to read"
    return dataset.to_table().to_pylist(), rask_properties(transaction.transaction_properties or {})


def _published(admin: requests.Session, project_id: str) -> dict[str, Any]:
    """The project once its publish ended, read until it is published or failed."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        project = _answer(admin.get(f"{ANNOTATOR}/projects/{project_id}", timeout=30))["project"]
        if project["state"] in {"published", "publish_failed"}:
            return project
        assert time.monotonic() < deadline, f"project {project_id} is still {project['state']} after {SETTLE_SECONDS:.0f} s"
        time.sleep(3)


def _told(admin: Identity, table_id: str, *, since: datetime) -> list[tuple[str, str]]:
    """The admin's inbox rows since ``since``, read until one names ``table_id`` or the settle window is spent."""
    reader = byo_scenario.Identity(admin.bearer)
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        rows = [(str(row["reason"]), str(row["object_id"])) for row in reader.inbox(since=since)]
        if any(object_id == table_id for _, object_id in rows) or time.monotonic() >= deadline:
            return rows
        time.sleep(3)


def _authors(reader: byo_scenario.Identity, table_id: str, *, since: int, after: int) -> list[str]:
    """Who the catalog announced each write to ``table_id`` past version ``after`` as, read off lineage's feed from row
    ``since``. By version, since the feed lags the commit: the table's own create can arrive after ``since`` was read.

    A write that names no run carries no `rask.*` transaction property, so its commit announcement is where the catalog
    records who made it: the verified caller, as the author facet.
    """

    def past(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if int(byo_scenario.lance_facet(row).get("version") or 0) > after]

    rows, _ = byo_scenario.follow(reader, since=since, table=table_id, until=lambda rows: bool(past(rows)))
    # The author rides the run's facets on a RunEvent and the dataset's on a DatasetEvent.
    carriers = [row["event"].get("run") if "run" in row["event"] else row["event"].get("dataset") for row in past(rows)]
    return [str(((carrier or {}).get("facets") or {}).get("author", {}).get("sub")) for carrier in carriers]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from scratch_tenant.provisioned("annot", grants=("writer",))


@pytest.fixture(scope="module")
def drive(tenant: Tenant) -> Iterator[Drive]:
    """The drive, and the project archived after it: its state is the annotator's, which the tenant's teardown never reaches."""
    started = datetime.now(UTC)
    admin, person = tenant.admin, tenant.job
    silver = f"{tenant.project}-silver"
    ok(admin.http.post(f"{CATALOG}/management/v1/warehouses/{tenant.project}/namespaces", json={"namespace": silver, "properties": {"rask.tier": "silver"}}))
    _seed_corpus(tenant)
    # A project admin hands the annotator's account what its publish needs, and nothing more.
    _grant(admin, silver, ANNOTATOR_ACCOUNT, "writer")
    _grant(admin, tenant.bronze, ANNOTATOR_ACCOUNT, "reader")

    as_admin, as_person = _annotator(admin), _annotator(person)
    project_id = str(_answer(as_admin.post(f"{ANNOTATOR}/projects", json={"tenant": tenant.project, "slug": "seals"}, timeout=30), 201)["project_id"])
    refused = as_person.get(f"{ANNOTATOR}/projects/{project_id}", timeout=30).status_code
    _answer(as_admin.put(f"{ANNOTATOR}/projects/{project_id}/members", json={"user": person.principal, "relation": "annotator"}, timeout=30))
    admitted = as_person.get(f"{ANNOTATOR}/projects/{project_id}", timeout=30).status_code

    # The person annotates the corpus itself: the annotator resolves the dataset through the catalog and writes the
    # annotations table on the person's own token.
    unit = f"{ANNOTATOR}/api/annotations/{UNIT[0]}/{UNIT[1]}/{UNIT[2]}"
    reader = byo_scenario.Identity(admin.bearer)
    feed = byo_scenario.feed_start(reader)
    before = lance.dataset(namespace_client=person.namespace(), table_id=[tenant.bronze, "annotations"]).version
    loaded = as_person.get(unit, params={"dataset": tenant.bronze}, timeout=60)
    assert loaded.status_code == 200, f"GET {unit} -> {loaded.status_code}: {loaded.text[:300]}"
    save = {"inserts": [{"id": "a1", "shape_type": "rectangle", "x": 10, "y": 10, "width": 20, "height": 20, "label": LABEL}]}
    save["base_version"] = int(loaded.headers["X-Annotations-Version"])
    _answer(as_person.post(unit, params={"dataset": tenant.bronze}, json=save, timeout=60))

    # The task plane: send, assign, draft, submit, accept, freeze, publish. The item carries the version of the corpus's
    # row table the person selected from, as the zone captures it, which the publish names as its input.
    selected = lance.dataset(namespace_client=person.namespace(), table_id=[tenant.bronze, "chunks"]).version
    source = {"kind": "chunks", "keys": ["/".join(map(str, UNIT))], "where": tenant.bronze, "dataset_version": selected}
    item = {"source": source, "media": {"kind": "image"}}
    [task_id] = _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/items", json={"items": [item]}, timeout=60), 201)["task_ids"]
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "open"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/tasks/{task_id}/events", json={"event": "assign", "assignee": person.principal}, timeout=30))
    shape = {"shape_type": "bbox", "x": 1, "y": 2, "width": 3, "height": 4, "label": LABEL}
    _answer(as_person.put(f"{ANNOTATOR}/tasks/{task_id}/draft", json={"shapes": [shape]}, timeout=30))
    _answer(as_person.post(f"{ANNOTATOR}/tasks/{task_id}/events", json={"event": "submit"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/tasks/{task_id}/events", json={"event": "accept"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "freeze"}, timeout=30))
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "publish"}, timeout=30))
    project = _published(as_admin, project_id)
    assert project["state"] == "published", f"the publish failed: {project.get('publish_error') or project}"
    published_table = str(project["published"]["table_id"])
    tenant.tables.append(published_table)

    annotations, _ = _commit(person, f"{tenant.bronze}$annotations")
    published, published_commit = _commit(admin, published_table)
    yield Drive(
        project_id=project_id,
        refused=refused,
        admitted=admitted,
        annotations=annotations,
        annotation_authors=_authors(reader, f"{tenant.bronze}$annotations", since=feed, after=before),
        selected=selected,
        published_table=published_table,
        published=published,
        published_commit=published_commit,
        told=_told(admin, published_table, since=started),
    )
    _answer(as_admin.post(f"{ANNOTATOR}/projects/{project_id}/events", json={"event": "archive"}, timeout=30))


def test_the_annotator_works_the_lakehouse_through_its_public_doors(drive: Drive, tenant: Tenant) -> None:
    """The closes-when of XC-167, read off the one drive.

    The annotator decides its own project roles: a person of no tenant is refused the project until the admin grants them
    a role on it. It holds no store credential: the person's annotation is written through the catalog on their own
    token, so the catalog announces the write as theirs. Its publish is its own account's commit in the tenant's silver,
    the row names who annotated and who reviewed from the task's own record, and the admin who published is told it is
    theirs.
    """
    assert (drive.refused, drive.admitted) == (403, 200)
    assert [(row["id"], row["label"], row["reviewer"]) for row in drive.annotations] == [("a1", LABEL, tenant.job.principal)]
    assert drive.annotation_authors and set(drive.annotation_authors) == {tenant.job.principal}, drive.annotation_authors
    rows = [(row["project_id"], row["label"], row["annotated_by"], row["reviewed_by"]) for row in drive.published]
    assert rows == [(drive.project_id, LABEL, tenant.job.principal, tenant.admin.principal)]
    assert drive.published_table.startswith(f"{tenant.project}-silver$")
    assert drive.published_commit.get("rask.author") == ANNOTATOR_ACCOUNT
    assert drive.published_commit.get("rask.inputs") == [{"table": [tenant.bronze, "chunks"], "version": drive.selected}]
    assert ("originator", drive.published_table) in drive.told, f"the admin was not told the publish was theirs: {drive.told}"
