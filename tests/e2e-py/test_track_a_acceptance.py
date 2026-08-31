"""Track A, DRIVEN — the lakehouse guarantees fixed this session, asserted against a running catalog.

Every guarantee below already has a RED-first unit test. None of them had a drive. That gap is the
whole reason this file exists: the defects it pins were all found by RUNNING the estate, and a unit
test that constructs a schema in memory cannot tell you whether the door on the other end of the wire
honours the field it was handed. The three branch-isolation defects are the sharpest example — each
one accepted `branch`, answered 200, and wrote to main.

WHAT IS ASSERTED

1. THE TIER CONTRACT (`publication.refuse_a_tier_without_provenance`, owner ruling D1). Opt-in by
   CLAIM: a table carrying ANY of `stage`/`lineage`/`source_rowid` is claiming to be a governed tier
   and must carry all three, `source_rowid` at uint64, on a dataset created with stable row ids. Four
   drives: the partial claim, the wrong width, the dataset whose row ids are not stable, and the
   conforming tier that must still publish. A fifth proves a plain table is untouched.

2. GATE/PUBLISH SYMMETRY. `gate_only=true` is the ASK and a bare publish is the ACT. Both doors are
   driven against ONE non-conforming table and must agree on status AND on the refusal text — a gate
   that under-reports sends the promotion review an approval the act will refuse, and the reviewer has
   no way to see it coming.

3. BRANCH ISOLATION. `update`, `delete` and `schema_metadata/update` each take a branch. Each must
   leave MAIN at the version and content it had, and each must actually reach the branch. Both halves
   matter and only the pair is a proof: a route that silently no-ops passes "main is untouched" while
   losing the caller's write.

4. THE READ SIDE OF THE SAME PROPERTY. `count_rows` declares `branch` too, and a door that ignores it
   answers main's number with a 200. This section exists because driving section 3 made it visible:
   while a branch-scoped write lands on main the two refs hold identical rows, so no read can be shown
   to be wrong. Only a genuinely diverged branch separates a read that honours the ref from one that
   does not.

WHY MAIN AND THE BRANCH ARE READ THROUGH LANCE, NOT THROUGH `/query`

The catalog's `/query` accepts `branch` and answers with main's rows regardless, so using it as the
oracle here would compare main against main and call every write isolated. The ground truth is the
object store: `lance.dataset(uri)` opens main and `checkout_version((branch, None))` opens the branch,
which is the same reference `catalog.core.namespace.open_dataset` uses to serve a branch-scoped write.
Reading the storage directly also keeps the assertion independent of the read path — a write defect
and a read defect cannot cancel out into a green.

WHAT IS DELIBERATELY NOT ASSERTED: READ-ONLY MAINTENANCE MODE

`LANCE_MAINTENANCE_READ_ONLY` is read from `app.state.settings` at request time, so flipping it on a
deployed catalog means an env change and a rollout — the suite would have to take the estate read-only,
restart the deployment, drive it, and restart it back, with a failed run leaving the owner's catalog
refusing every write. That is a bigger blast radius than the guarantee is worth in an acceptance
suite. The property (the maintenance middleware runs BEFORE auth, so during a window a POST read
answers 401 and a POST write answers 503) stays covered by `services/catalog/tests` and by the hand
drive recorded with the fix.

ISOLATION AND CLEANUP. Each run mints its OWN tenant — project, warehouse (its own bucket) and
namespace, all uuid-suffixed — so it shares nothing with bronze/silver/gold or any existing tenant, and
a mid-run failure strands artefacts under one obviously-scratch name. Teardown drops the tables, then
deletes the warehouse with its bucket, then the project. A fresh project also means NO declared
`GateSpec`, so the gate under test is the one the request names rather than a tenant's policy.

Run (the catalog and RustFS both answer on their ClusterIPs from a k3s host), or `make e2e-track-a`:

    LANCE_E2E_CATALOG_URL=http://10.43.220.241:2333 \
    LANCE_E2E_TOKEN=$(dex id_token for a project-creating user) \
    LANCE_E2E_S3_ENDPOINT=http://10.43.156.125:9000 \
    uv run pytest tests/e2e-py/test_track_a_acceptance.py -v
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Iterator
from typing import Any

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
import requests


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
TOKEN = os.environ.get("LANCE_E2E_TOKEN", "")
S3_ENDPOINT = os.environ.get("LANCE_E2E_S3_ENDPOINT", "")
S3_ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "rustfsadmin")
S3_SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "rustfsadmin")
S3_REGION = os.environ.get("LANCE_E2E_S3_REGION", "us-east-1")
DELIM = os.environ.get("LANCE_E2E_DELIM", "$")

ARROW_STREAM = "application/vnd.apache.arrow.stream"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.track_a,
    pytest.mark.skipif(
        not (CATALOG and TOKEN and S3_ENDPOINT),
        reason="set LANCE_E2E_CATALOG_URL + LANCE_E2E_TOKEN + LANCE_E2E_S3_ENDPOINT (a deployed catalog and its object store)",
    ),
]


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _storage_options() -> dict[str, str]:
    """The object-store connection the CATALOG itself writes through, so this suite reads the same bytes.

    Mirrors `catalog.core.config.Settings.storage_options`. `allow_http` and the path-style flag are not
    optional against RustFS/MinIO: without them the object-store client upgrades to HTTPS and rewrites the
    bucket into the hostname, and every open fails in a way that reads as a missing dataset.
    """
    return {
        "aws_access_key_id": S3_ACCESS_KEY,
        "aws_secret_access_key": S3_SECRET_KEY,
        "aws_endpoint": S3_ENDPOINT,
        "aws_region": S3_REGION,
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


def _arrow_ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


class Estate:
    """One run's own tenant, and the four calls every test makes against it."""

    def __init__(self, project: str, warehouse: str, namespace: str) -> None:
        self.project = project
        self.warehouse = warehouse
        self.namespace = namespace

    def table_id(self, name: str) -> str:
        return f"{self.namespace}{DELIM}{name}"

    def create(self, name: str, table: pa.Table) -> str:
        """Create a table through the catalog's Arrow-IPC door; returns its dataset URI."""
        r = requests.post(
            f"{CATALOG}/v1/table/{self.table_id(name)}/create",
            data=_arrow_ipc(table),
            headers={**_auth(), "content-type": ARROW_STREAM},
            timeout=120,
        )
        assert r.status_code == 200, r.text
        return str(r.json()["location"])

    def publish(self, name: str, *, version: int = 1, key_column: str = "id", gate_only: bool = False) -> requests.Response:
        body: dict[str, Any] = {"version": version, "key_column": key_column}
        if gate_only:
            body["gate_only"] = True
        return requests.post(f"{CATALOG}/v1/table/{self.table_id(name)}/publish", json=body, headers=_auth(), timeout=120)

    def branch(self, name: str, branch: str) -> None:
        r = requests.post(f"{CATALOG}/v1/table/{self.table_id(name)}/branches/create", json={"name": branch}, headers=_auth(), timeout=60)
        assert r.status_code == 200, r.text


def _tier(rows: int = 2, *, source_rowid_type: pa.DataType | None = None) -> pa.Table:
    """A conforming governed tier: the identity column plus the three provenance columns.

    `source_rowid` defaults to the width Lance's own stable row id uses; the wrong-width drive names
    int64 explicitly, because that is the whole content of the case it asserts.
    """
    return pa.table(
        {
            "id": pa.array([f"row-{i}" for i in range(rows)], pa.string()),
            "payload": pa.array([f"payload-{i}" for i in range(rows)], pa.string()),
            "stage": pa.array(["silver"] * rows, pa.string()),
            "lineage": pa.array(["run-track-a"] * rows, pa.string()),
            "source_rowid": pa.array(list(range(rows)), source_rowid_type or pa.uint64()),
        }
    )


def _open_main(uri: str) -> lance.LanceDataset:
    return lance.dataset(uri, storage_options=_storage_options())


def _open_branch(uri: str, branch: str) -> lance.LanceDataset:
    """The branch ref, opened the way the catalog's own `open_dataset` opens one.

    A branch is a parallel dataset under `tree/{branch}/` with its own version numbering, and a plain
    `lance.dataset(uri)` reaches main only — which is precisely the confusion the three branch defects
    lived in.
    """
    return lance.dataset(uri, storage_options=_storage_options()).checkout_version((branch, None))


def _content(dataset: lance.LanceDataset) -> dict[str, str]:
    table = dataset.to_table(columns=["id", "text"])
    return dict(zip(table["id"].to_pylist(), table["text"].to_pylist(), strict=True))


def _user_metadata(dataset: lance.LanceDataset) -> dict[str, str]:
    """Schema metadata minus the `lineage.*` keys the catalog stamps on every create."""
    raw = dataset.schema.metadata or {}
    decoded = {k.decode(): v.decode() for k, v in raw.items()}
    return {k: v for k, v in decoded.items() if not k.startswith("lineage.")}


def _count(estate: Estate, name: str, *, predicate: str = "id >= 0", branch: str | None = None) -> int:
    """The catalog's own row count, through `count_rows` — the door under test in section 4, never an oracle."""
    body: dict[str, Any] = {"predicate": predicate}
    if branch is not None:
        body["branch"] = branch
    response = requests.post(f"{CATALOG}/v1/table/{estate.table_id(name)}/count_rows", json=body, headers=_auth(), timeout=60)
    assert response.status_code == 200, response.text
    return int(response.text)


def _rowed(*pairs: tuple[str, str]) -> pa.Table:
    return pa.table({"id": pa.array([p[0] for p in pairs], pa.string()), "text": pa.array([p[1] for p in pairs], pa.string())})


@pytest.fixture(scope="module")
def estate() -> Iterator[Estate]:
    """This run's own project + warehouse + namespace, torn down at the end.

    The tenant is minted rather than borrowed for two reasons, and both are load-bearing. It keeps the
    suite off every existing tenant's objects; and a fresh project has declared no `GateSpec`, so the
    publish gate under test is the one the REQUEST names. Driving inside a tenant that declares one would
    silently substitute its policy for the assertion's key column and required columns.
    """
    try:
        requests.get(f"{CATALOG}/livez", timeout=10).raise_for_status()
    except Exception:  # noqa: BLE001 — an unreachable estate is a skip, not a failure
        pytest.skip(f"catalog not reachable at {CATALOG}")

    suffix = uuid.uuid4().hex[:8]
    live = Estate(project=f"tracka{suffix}", warehouse=f"tracka{suffix}-wh", namespace=f"trackans{suffix}")

    r = requests.post(f"{CATALOG}/v1/projects", json={"id": live.project}, headers=_auth(), timeout=60)
    assert r.status_code == 200, f"could not mint a scratch project (is this token a project creator?): {r.text}"
    r = requests.post(f"{CATALOG}/v1/warehouses", json={"id": live.warehouse, "project": live.project}, headers=_auth(), timeout=120)
    assert r.status_code == 200, r.text
    # The binding is what makes the namespace resolvable at all: a top-level namespace with no warehouse
    # is refused 400 by the hierarchy guard, so this call is the namespace's creation, not a decoration.
    r = requests.post(f"{CATALOG}/v1/warehouses/{live.warehouse}/namespaces", json={"namespace": live.namespace}, headers=_auth(), timeout=60)
    assert r.status_code == 200, r.text

    try:
        yield live
    finally:
        listed = requests.get(f"{CATALOG}/v1/namespace/{live.namespace}/table/list", headers=_auth(), timeout=60)
        if listed.status_code == 200:
            for name in listed.json().get("tables", []):
                requests.post(f"{CATALOG}/v1/table/{live.namespace}{DELIM}{name}/drop", json={}, headers=_auth(), timeout=120)
        # `purge_bucket` is safe HERE and nowhere else in this file: the bucket was minted by this run's
        # own warehouse a few seconds ago and holds nothing else.
        requests.delete(
            f"{CATALOG}/v1/warehouses/{live.warehouse}",
            params={"cascade": "true", "purge_bucket": "true", "force": "true"},
            headers=_auth(),
            timeout=180,
        )
        requests.delete(f"{CATALOG}/v1/projects/{live.project}", headers=_auth(), timeout=60)


# --- 1. the tier contract ----------------------------------------------------------------------


def test_a_tier_claiming_provenance_it_does_not_carry_cannot_publish(estate: Estate) -> None:
    """`source_rowid` alone is a CLAIM to be governed, and the door must refuse the claim it cannot honour.

    This is the shape that shipped: a table with a parent-row column and neither `stage` nor `lineage`.
    The job-side check cannot see it — it counts parentless rows only when the column is in the schema,
    so a table that drops the column reports zero and passes the check meant to catch it.
    """
    estate.create("partial_claim", pa.table({"id": pa.array(["a"], pa.string()), "source_rowid": pa.array([0], pa.uint64())}))

    response = estate.publish("partial_claim")

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert "missing 'lineage'" in detail and "missing 'stage'" in detail, detail


def test_a_tier_whose_source_rowid_is_the_wrong_width_cannot_publish(estate: Estate) -> None:
    """An int64 `source_rowid` is a DIFFERENT column wearing the right name.

    The failure a reader is least likely to see: present, non-null, and passing every count-based check,
    while Lance's own stable row id is uint64 — so the values are not the ids they claim to be.
    """
    estate.create("wrong_width", _tier(source_rowid_type=pa.int64()))

    response = estate.publish("wrong_width")

    assert response.status_code == 400, response.text
    assert "not uint64" in response.json()["detail"], response.text


def test_a_tier_whose_dataset_has_no_stable_row_ids_cannot_publish(estate: Estate) -> None:
    """The half the columns cannot show, and the only leg that needs a hand-written dataset.

    `enable_stable_row_ids` is CREATE-TIME ONLY, so a dataset made without it carries a perfectly
    well-typed `source_rowid` whose values do not survive compaction. The catalog's own create door always
    sets the flag, which is exactly why this must be driven through `register` — the door that attaches
    bytes somebody else wrote, and the one a hand-built tier actually arrives through.
    """
    name = f"unstable_{uuid.uuid4().hex[:6]}"
    lance.write_dataset(_tier(), f"s3://{estate.warehouse}/{name}", storage_options=_storage_options(), enable_stable_row_ids=False)
    # A RELATIVE location: `register_table` refuses an absolute URI, because a registration that could name
    # any bucket would attach storage the namespace does not own.
    registered = requests.post(f"{CATALOG}/v1/table/{estate.table_id(name)}/register", json={"location": name}, headers=_auth(), timeout=120)
    assert registered.status_code == 200, registered.text

    response = estate.publish(name)

    assert response.status_code == 400, response.text
    assert "without stable row ids" in response.json()["detail"], response.text


def test_a_conforming_tier_publishes(estate: Estate) -> None:
    """The contract must not refuse the shape it exists to require — the half that makes the other three honest."""
    estate.create("conforming", _tier())

    response = estate.publish("conforming")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["published"] is True, body
    assert body["to_version"] == 1, body
    # The gate the REQUEST named governed, not a tenant's declaration: the scratch project declares none.
    assert body["gate_source"] == "request", body


def test_a_plain_table_is_untouched_by_the_tier_contract(estate: Estate) -> None:
    """A table claiming NOTHING is not a governed tier, and must publish exactly as it did before D1.

    The rule is opt-in by claim, and this is what makes it safe to enforce at a door every writer passes:
    a registered external dataset or a user's own table carries none of the three columns and is left alone.
    """
    estate.create("plain", pa.table({"id": pa.array(["a", "b"], pa.string()), "text": pa.array(["one", "two"], pa.string())}))

    response = estate.publish("plain")

    assert response.status_code == 200, response.text
    assert response.json()["published"] is True, response.text


# --- 2. gate/publish symmetry ------------------------------------------------------------------


def test_the_gate_refuses_exactly_what_the_publish_refuses(estate: Estate) -> None:
    """One non-conforming table, both doors, and the answers must be identical.

    The ASK and the ACT are separate endpoints so the promotion review can decide before promoting. That
    only works if they answer the same question: a gate that reported a pass on a table the publish will
    refuse hands the reviewer an approval that cannot be executed, and the reviewer cannot see it coming.
    The refusal TEXT is compared too, not merely the status — two 400s for different reasons would still
    be a disagreement.
    """
    estate.create("symmetry", pa.table({"id": pa.array(["a"], pa.string()), "stage": pa.array(["silver"], pa.string())}))

    ask = estate.publish("symmetry", gate_only=True)
    act = estate.publish("symmetry")

    assert ask.status_code == act.status_code == 400, (ask.text, act.text)
    assert ask.json()["detail"] == act.json()["detail"], (ask.text, act.text)
    assert "missing 'lineage'" in ask.json()["detail"], ask.text
    # And the ASK stayed an ask: a refused gate must not have moved the pointer on its way to the 400.
    tag = requests.post(f"{CATALOG}/v1/table/{estate.table_id('symmetry')}/tags/list", json={}, headers=_auth(), timeout=60)
    assert tag.status_code == 200, tag.text
    assert "published" not in tag.json().get("tags", {}), tag.text


def test_the_gate_agrees_with_the_publish_on_a_conforming_tier(estate: Estate) -> None:
    """Symmetry has to hold in the PASSING direction too, or the ask is useless rather than dangerous.

    A gate that refused what the publish accepts would stall every promotion review on a table that is
    fine — the same disagreement, costing availability instead of correctness.
    """
    estate.create("symmetry_ok", _tier())

    ask = estate.publish("symmetry_ok", gate_only=True)

    assert ask.status_code == 200, ask.text
    body = ask.json()
    # A verdict, never a write: `published` is false on this path whatever the assertions say.
    assert body["published"] is False, body
    assert body["reason"] is None, body
    assert all(a["success"] for a in body["assertions"]), body

    act = estate.publish("symmetry_ok")
    assert act.status_code == 200, act.text
    assert act.json()["published"] is True, act.text


# --- 3. branch isolation -----------------------------------------------------------------------


def test_a_branch_scoped_update_leaves_main_untouched(estate: Estate) -> None:
    """`POST /update` with `branch` must edit the branch and only the branch.

    Both halves are asserted because either alone is satisfiable by a broken route: a write that reaches
    main passes "the branch changed" if the branch inherits it, and a write that reaches nothing at all
    passes "main is untouched".
    """
    uri = estate.create("branch_update", _rowed(("a", "one"), ("b", "two"), ("c", "three")))
    estate.branch("branch_update", "work")
    main_before = _open_main(uri)

    response = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('branch_update')}/update",
        json={"predicate": "id = 'a'", "updates": [["text", "'BRANCHED'"]], "branch": "work"},
        headers=_auth(),
        timeout=120,
    )
    assert response.status_code == 200, response.text
    assert response.json()["updated_rows"] == 1, response.text

    main_after = _open_main(uri)
    assert main_after.version == main_before.version, "a branch-scoped update committed a new version on MAIN"
    assert _content(main_after) == {"a": "one", "b": "two", "c": "three"}
    assert _content(_open_branch(uri, "work")) == {"a": "BRANCHED", "b": "two", "c": "three"}


def test_a_branch_scoped_delete_leaves_main_untouched(estate: Estate) -> None:
    """`POST /delete` with `branch` must remove the row from the branch and only the branch."""
    uri = estate.create("branch_delete", _rowed(("a", "one"), ("b", "two"), ("c", "three")))
    estate.branch("branch_delete", "work")
    main_before = _open_main(uri)

    response = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('branch_delete')}/delete",
        json={"predicate": "id = 'b'", "branch": "work"},
        headers=_auth(),
        timeout=120,
    )
    assert response.status_code == 200, response.text

    main_after = _open_main(uri)
    assert main_after.version == main_before.version, "a branch-scoped delete committed a new version on MAIN"
    assert _content(main_after) == {"a": "one", "b": "two", "c": "three"}
    assert _content(_open_branch(uri, "work")) == {"a": "one", "c": "three"}


def test_a_branch_scoped_schema_metadata_update_leaves_main_untouched(estate: Estate) -> None:
    """`POST /schema_metadata/update` with `branch` in the ENVELOPE must rewrite the branch's properties, not main's.

    The envelope is where the spec puts `branch` for an operation with a JSON body, and this route already
    reads `id` out of that same dict. A body with no null values takes the native spec op; a body carrying
    one takes the catalog's own delete-dialect. Both are driven, because the two paths reach different
    code and a branch honoured on one of them is not the guarantee.
    """
    uri = estate.create("branch_meta", _rowed(("a", "one")))
    estate.branch("branch_meta", "work")
    seeded = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('branch_meta')}/schema_metadata/update",
        json={"metadata": {"seed": "main"}},
        headers=_auth(),
        timeout=60,
    )
    assert seeded.status_code == 200, seeded.text
    main_before = _open_main(uri)

    native_path = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('branch_meta')}/schema_metadata/update",
        json={"metadata": {"only_on_branch": "1"}, "branch": "work"},
        headers=_auth(),
        timeout=60,
    )
    assert native_path.status_code == 200, native_path.text

    main_after = _open_main(uri)
    assert main_after.version == main_before.version, "a branch-scoped schema-metadata update committed a new version on MAIN"
    assert _user_metadata(main_after) == {"seed": "main"}, "the branch's property landed on MAIN"
    assert _user_metadata(_open_branch(uri, "work")).get("only_on_branch") == "1", "the branch-scoped write did not reach the branch"

    # The delete-dialect path: a null value removes the key, and it must remove it from the BRANCH.
    delete_dialect = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('branch_meta')}/schema_metadata/update",
        json={"metadata": {"only_on_branch": None, "second": "2"}, "branch": "work"},
        headers=_auth(),
        timeout=60,
    )
    assert delete_dialect.status_code == 200, delete_dialect.text

    assert _user_metadata(_open_main(uri)) == {"seed": "main"}, "the delete-dialect branch write reached MAIN"
    branch_meta = _user_metadata(_open_branch(uri, "work"))
    assert "only_on_branch" not in branch_meta, branch_meta
    assert branch_meta.get("second") == "2", branch_meta


# --- 4. the read side of branch isolation ------------------------------------------------------
#
# Found by driving this suite, not by reading it: with the write half correct, main and the branch
# genuinely differ, and only then can a read that ignores `branch` be told apart from one that honours it.


def test_a_count_on_a_branch_reads_that_branch(estate: Estate) -> None:
    """`count_rows` declares `branch` and must answer for the ref it was given.

    The write fix has a twin on the read side, and it is invisible while the writes are wrong: if a
    branch-scoped delete lands on main, both refs hold the same rows and any count agrees with any other.
    With the branch genuinely diverged, a door that answers main's number for a branch is unmistakable —
    and it is the same failure shape as the write bug for the same reason: nothing errors, nothing logs,
    and the number returned is a perfectly plausible number.

    Ground truth comes from the object store, so the assertion is about the DOOR and not about a second
    read path that might be wrong in the same direction.
    """
    uri = estate.create("read_branch", pa.table({"id": pa.array([0, 1, 2], pa.int64()), "text": pa.array(["a", "b", "c"], pa.string())}))
    estate.branch("read_branch", "dev")
    deleted = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('read_branch')}/delete",
        json={"predicate": "id > 0", "branch": "dev"},
        headers=_auth(),
        timeout=120,
    )
    assert deleted.status_code == 200, deleted.text
    # The premise: this test can only speak about the READ path once the WRITE path is isolating.
    assert _open_main(uri).count_rows() == 3, "main lost rows to a branch-scoped delete — that is the write defect, not this one"
    assert _open_branch(uri, "dev").count_rows() == 1, "the branch-scoped delete did not reach the branch"

    on_main = _count(estate, "read_branch")
    on_branch = _count(estate, "read_branch", branch="dev")

    assert on_main == 3, on_main
    assert on_branch == 1, f"a count naming branch 'dev' returned {on_branch}; the branch holds 1 row and main holds 3, so this is MAIN's answer"


def test_a_count_on_a_branch_that_does_not_exist_is_refused(estate: Estate) -> None:
    """A ref that was never created has no answer, so any 200 here proves the parameter was not read.

    The sharper of the two assertions: it does not depend on the two refs holding different row counts,
    so it stays meaningful even where main and a branch happen to agree.
    """
    estate.create("read_ghost", pa.table({"id": pa.array([0, 1, 2], pa.int64()), "text": pa.array(["a", "b", "c"], pa.string())}))

    response = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('read_ghost')}/count_rows",
        json={"predicate": "id >= 0", "branch": "no-such-branch-exists"},
        headers=_auth(),
        timeout=60,
    )

    assert response.status_code == 404, (
        f"counting on a branch that does not exist answered {response.status_code} with {response.text[:80]!r}. "
        "An absent ref must be an error, not a silent fallback to a different dataset."
    )


@pytest.mark.parametrize("door", ["query", "explain_plan", "analyze_plan"])
def test_a_read_door_that_cannot_scope_to_a_branch_refuses_instead_of_answering(estate: Estate, door: str) -> None:
    """The other three branch-declaring read doors must REFUSE, not answer from main.

    `query`, `explain_plan` and `analyze_plan` hand the whole request to an implementation that
    disregards `branch`. Measured live before the fix: with two of three rows deleted on `work`,
    `/query` returned three rows for `branch=work` AND three for a branch that had never been created
    — main's answer, twice, under the caller's branch name.

    501 `Unsupported` is the fix at this door rather than a deferral of one. The defect was never
    "branch queries are missing"; it was that the parameter is accepted and disregarded, so a caller
    staging work on a branch silently reads main. Saying "this backend does not do that" is a complete
    and honest answer. Serving it properly means re-deriving vector search, full-text search, prefilter,
    nprobes, refine_factor and distance_type against a branch handle — real work with its own tests,
    named in `open_backlog.md`, and not something to smuggle in behind a door that currently lies.

    Parametrised so a fix applied to one door and not its siblings cannot leave this file green. Five
    of this session's defects were exactly that shape.
    """
    name = f"refuse_{door}"
    estate.create(name, pa.table({"id": pa.array([0], pa.int64()), "text": pa.array(["a"], pa.string())}))
    # Each door validates a different body, and an invalid one 422s BEFORE the guard is reached — which
    # is a test that proves nothing while looking like it failed for the right reason. `explain_plan`
    # takes a `query`; the other two take a vector search.
    body: dict[str, Any] = {"branch": "work"}
    search: dict[str, Any] = {"vector": {"single_vector": [1.0, 0.0]}, "k": 10}
    body |= {"query": search} if door == "explain_plan" else search
    response = requests.post(f"{CATALOG}/v1/table/{estate.table_id(name)}/{door}", json=body, headers=_auth(), timeout=60)
    assert response.status_code == 501, (
        f"/{door} answered a branch-scoped read with {response.status_code} instead of 501 Unsupported: "
        f"{response.text[:200]}. Returning main's rows under the caller's branch name is worse than not "
        "supporting branches at all, because nothing in the response says which dataset was read."
    )


def _insert(estate: Estate, name: str, table: pa.Table, *, branch: str | None = None) -> requests.Response:
    query = f"?branch={branch}" if branch else ""
    return requests.post(
        f"{CATALOG}/v1/table/{estate.table_id(name)}/insert{query}",
        data=_arrow_ipc(table),
        headers={**_auth(), "content-type": ARROW_STREAM},
        timeout=120,
    )


def test_a_branch_scoped_insert_appends_to_the_branch_and_not_to_main(estate: Estate) -> None:
    """The most damaging member of the family, because it is a WRITE that succeeds.

    Measured live before the fix: `POST /insert?branch=work` answered 200, MAIN gained the row and the
    branch did not. `insert` carries `branch` as a spec-0.9 query parameter (Arrow-IPC-body operations
    cannot put it in the envelope), the route read it, filled it into the request — and the upstream
    implementation disregarded it, so the row landed in the dataset the caller was deliberately staying
    out of.

    Both halves are asserted from the object store, because either alone is satisfiable by a broken
    route: a write that reaches main passes "the branch has the row" if the branch inherits it, and a
    write that reaches nothing passes "main is untouched".
    """
    uri = estate.create("insert_branch", _rowed(("a", "one"), ("b", "two")))
    estate.branch("insert_branch", "work")
    main_before = _open_main(uri)

    response = _insert(estate, "insert_branch", _rowed(("z", "ONBRANCH")), branch="work")
    assert response.status_code == 200, response.text

    main_after = _open_main(uri)
    assert main_after.version == main_before.version, "a branch-scoped insert committed a new version on MAIN"
    assert _content(main_after) == {"a": "one", "b": "two"}, "the branch's row landed on MAIN"
    assert _content(_open_branch(uri, "work")) == {"a": "one", "b": "two", "z": "ONBRANCH"}, "the branch-scoped insert did not reach the branch"


def test_a_branch_scoped_merge_insert_applies_to_the_branch_and_not_to_main(estate: Estate) -> None:
    """`merge_insert` with a branch must merge into the branch.

    Verified live before the fix: a merge naming `work` applied its update to MAIN, left the branch
    untouched, and reported `num_updated_rows: 1` — a truthful count of a change made to a dataset the
    caller had not named.
    """
    uri = estate.create("merge_branch", _rowed(("a", "one"), ("b", "two")))
    estate.branch("merge_branch", "work")
    main_before = _open_main(uri)

    response = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('merge_branch')}/merge_insert?on=id&when_matched_update_all=true&branch=work",
        data=_arrow_ipc(_rowed(("a", "MERGED"))),
        headers={**_auth(), "content-type": ARROW_STREAM},
        timeout=120,
    )
    assert response.status_code == 200, response.text

    main_after = _open_main(uri)
    assert main_after.version == main_before.version, "a branch-scoped merge_insert committed a new version on MAIN"
    assert _content(main_after) == {"a": "one", "b": "two"}, "the branch's merge landed on MAIN"
    assert _content(_open_branch(uri, "work")) == {"a": "MERGED", "b": "two"}, "the branch-scoped merge did not reach the branch"


def test_a_write_to_a_branch_that_does_not_exist_is_refused(estate: Estate) -> None:
    """A ref nobody created cannot be written to, and the alternative is not "nothing happens".

    Before the fix this answered 200 and appended to MAIN — so a typo in a branch name silently
    polluted the dataset the branch existed to protect. Spec error 22 `TableBranchNotFound`.
    """
    uri = estate.create("insert_ghost", _rowed(("a", "one")))
    before = _open_main(uri).count_rows()

    response = _insert(estate, "insert_ghost", _rowed(("z", "GHOST")), branch="no-such-branch-was-ever-created")

    assert response.status_code == 404, f"inserting on a nonexistent branch answered {response.status_code}: {response.text[:200]}"
    assert _open_main(uri).count_rows() == before, (
        "a write to a nonexistent branch was absorbed by MAIN — the typo case, and the reason this is a 404 rather than a no-op"
    )


def test_the_merge_key_index_built_after_a_branch_merge_lands_on_the_branch(estate: Estate) -> None:
    """The accelerator must follow the write, and it did not.

    `/merge_insert` builds a BTREE on the merge key afterwards so later upserts stop full-scanning. That
    build went through the native op, which accepts `branch` and builds on MAIN — so a branch-scoped
    merge was isolated and then committed an index version to main immediately after. The source
    carried a note saying this was unverified at pylance 8.0.0 and forwarded the parameter anyway; this
    test is the verification, and the answer was no.

    Asserted as MAIN's VERSION rather than as the index listing, because the version is what a
    concurrent reader of main sees move. An index is a legitimate thing to build on main — what is not
    legitimate is main advancing as a side effect of a write that named a branch.
    """
    uri = estate.create("index_branch", _rowed(("a", "one"), ("b", "two")))
    estate.branch("index_branch", "work")
    main_before = _open_main(uri)

    response = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('index_branch')}/merge_insert?on=id&when_matched_update_all=true&branch=work",
        data=_arrow_ipc(_rowed(("a", "MERGED"))),
        headers={**_auth(), "content-type": ARROW_STREAM},
        timeout=120,
    )
    assert response.status_code == 200, response.text

    assert _open_main(uri).version == main_before.version, (
        "MAIN advanced a version after a branch-scoped merge_insert. The merge itself is isolated, so "
        "this is the index build that follows it — the accelerator going to a dataset the caller did "
        "not name."
    )


def test_a_version_listing_scoped_to_a_branch_lists_that_branch(estate: Estate) -> None:
    """`version/list` honours `branch`, and this test exists to keep it that way — and to pin the CHANNEL.

    It is here because of a mistake worth encoding. `branch` on this route is a QUERY parameter, not a
    body field; a probe that sent it in the JSON body was silently ignored by FastAPI, the door answered
    for main, and that is indistinguishable from the defect this file hunts. The door was correct all
    along. A regression that moved `branch` into the body, or dropped the query parameter, would
    reproduce that false reading — so asserting through the query string is the assertion.

    The sibling doors on the same module honour it too (`version/describe`, `version/delete`), while
    `count_rows` and `query` did not. Upstream is right or wrong PER OPERATION, so each door is pinned
    by driving it rather than by inheriting a verdict from its neighbour.
    """
    uri = estate.create("verlist", _rowed(("a", "one"), ("b", "two"), ("c", "three")))
    estate.branch("verlist", "work")
    for row in ("a", "b", "c"):
        r = requests.post(
            f"{CATALOG}/v1/table/{estate.table_id('verlist')}/delete",
            json={"predicate": f"id = '{row}'", "branch": "work"},
            headers=_auth(),
            timeout=120,
        )
        assert r.status_code == 200, r.text

    main_versions = _open_main(uri).version
    branch_versions = _open_branch(uri, "work").version
    assert branch_versions > main_versions, (
        f"the branch did not diverge (main={main_versions}, branch={branch_versions}); nothing here can tell the two refs apart"
    )

    on_main = requests.post(f"{CATALOG}/v1/table/{estate.table_id('verlist')}/version/list", headers=_auth(), timeout=60)
    on_branch = requests.post(f"{CATALOG}/v1/table/{estate.table_id('verlist')}/version/list?branch=work", headers=_auth(), timeout=60)
    assert on_main.status_code == 200, on_main.text
    assert on_branch.status_code == 200, on_branch.text

    assert len(on_main.json().get("versions", [])) == main_versions
    listed = on_branch.json().get("versions", [])
    assert len(listed) == branch_versions, f"?branch=work listed {len(listed)} versions; the branch has {branch_versions} and main has {main_versions}"
    for entry in listed:
        assert "/tree/work/_versions/" in (entry.get("manifest_path") or ""), (
            f"a branch version's manifest_path is {entry.get('manifest_path')!r} — not under `tree/work/`, so the row "
            "describes a MAIN version wearing a branch listing's shape."
        )

    ghost = requests.post(f"{CATALOG}/v1/table/{estate.table_id('verlist')}/version/list?branch=no-such-branch-was-ever-created", headers=_auth(), timeout=60)
    assert ghost.status_code == 404, f"listing versions of a branch that does not exist returned {ghost.status_code}: {ghost.text[:200]}"


def test_a_branch_scoped_index_build_does_not_land_on_main(estate: Estate) -> None:
    """The index doors are WRITES, and they were writing to the wrong dataset with a 200.

    Measured live 2026-08-31 before the fix: `create_scalar_index` with `branch=work` returned 200,
    MAIN advanced a version and took the index, and the branch got none. An index on the wrong dataset
    is not inert — it changes which plans the engine picks for every later reader of main.

    501 rather than served, because `CreateTableIndexRequest` carries a whole full-text option surface
    (`base_tokenizer`, `language`, `stem`, `ascii_folding`, `remove_stop_words`, `with_position`,
    `max_token_length`, `lower_case`) plus vector parameters, and building an index with a DEFAULT
    where the caller asked for a setting is the same quiet wrongness as building it on the wrong table.
    The merge-key BTREE that `/merge_insert` builds internally IS served on the branch — one fixed
    shape, no user options, nothing left to get wrong. The line is the option surface, not the verb.
    """
    uri = estate.create("idxbranch", _rowed(("a", "one"), ("b", "two")))
    estate.branch("idxbranch", "work")
    before = _open_main(uri).version

    for door in ("create_scalar_index", "create_index"):
        response = requests.post(
            f"{CATALOG}/v1/table/{estate.table_id('idxbranch')}/{door}",
            json={"column": "id", "index_type": "BTREE", "branch": "work"},
            headers=_auth(),
            timeout=120,
        )
        assert response.status_code == 501, f"/{door} accepted a branch-scoped build with {response.status_code}: {response.text[:200]}"

    assert _open_main(uri).version == before, "MAIN advanced a version for an index build that named a branch"
    assert not _open_main(uri).list_indices(), "MAIN took an index from a build that named a branch"


def test_a_branch_nested_in_an_explain_query_is_refused_like_the_outer_one(estate: Estate) -> None:
    """`explain_plan` nests the whole query, so `branch` has TWO channels and the guard covered one.

    Measured live: with only `body.branch` guarded, a branch inside `query` returned 200 and a plan —
    for a real branch and for one that had never been created, identically. A guard that covers one of
    two channels reads as a guard while being none, which is worse than no guard: it is why this door
    looked settled.
    """
    estate.create("explnest", _rowed(("a", "one")))
    search: dict[str, Any] = {"vector": {"single_vector": [1.0, 0.0]}, "k": 5}

    outer = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('explnest')}/explain_plan",
        json={"branch": "work", "query": search},
        headers=_auth(),
        timeout=60,
    )
    nested = requests.post(
        f"{CATALOG}/v1/table/{estate.table_id('explnest')}/explain_plan",
        json={"query": {**search, "branch": "work"}},
        headers=_auth(),
        timeout=60,
    )
    assert outer.status_code == 501, f"top-level branch: {outer.status_code} {outer.text[:150]}"
    assert nested.status_code == 501, (
        f"a branch nested inside `query` answered {nested.status_code} — the outer channel is guarded and "
        f"the inner one is not, so the door serves main's plan under a branch name: {nested.text[:150]}"
    )


@pytest.mark.parametrize(
    ("door", "query"),
    [("stats", "branch=work"), ("index/list", "branch=work"), ("index/id_idx/stats", "branch=work")],
    ids=["table-stats", "index-list", "index-stats"],
)
def test_a_read_door_that_never_offered_a_branch_now_refuses_one(estate: Estate, door: str, query: str) -> None:
    """Three doors did not ACCEPT `branch` at all, which read as safe and was not.

    A route that declares no branch parameter still receives the request — it just ignores it — so a
    caller asking for a branch got 200 and MAIN's answer. Verified live 2026-08-31:

      * `/stats` reported num_rows 4 for a branch holding 1, and again for a branch never created
      * `/index/list` reported an empty list for a branch carrying a BTREE that main did not have

    Being told "4 rows" or "no indices" about the wrong dataset is worse than an error, because it is
    actionable. Declaring the parameter in order to refuse it is the smallest honest fix: the caller
    now learns the door cannot answer their question instead of receiving someone else's answer.

    Refused rather than served because the responses cannot be assembled honestly from a branch handle
    — `FragmentStats.lengths` and an index's `status`/`size_bytes` are not what `dataset_stats()` and
    `list_indices()` report, and filling a required field with a plausible value is the failure this
    file exists to catch.
    """
    # A NAME PER CASE. The estate is module-scoped, so a shared table name makes the second and third
    # parameters 409 on create — a failure that looks like the guard and is the fixture.
    name = f"readrefuse_{door.replace('/', '_')}"
    estate.create(name, _rowed(("a", "one"), ("b", "two")))
    estate.branch(name, "work")
    response = requests.post(f"{CATALOG}/v1/table/{estate.table_id(name)}/{door}?{query}", headers=_auth(), timeout=60)
    assert response.status_code == 501, f"/{door} answered a branch-scoped read with {response.status_code} instead of 501: {response.text[:200]}"


def test_describe_refuses_a_branch_on_both_channels_and_an_impossible_version(estate: Estate) -> None:
    """`describe` had a body-only refusal, which is a refusal on one of two channels.

    The route already refused `branch` in the request BODY, with a comment saying exactly why —
    "SILENTLY describing main for a caller who pinned a branch is the same class of wrong-but-plausible
    answer". It did not DECLARE `branch` as a query parameter, so `?branch=work` reached a route that
    does not take it, FastAPI dropped it, and the door answered for main with a 200 — for a real branch
    and for one that had never been created alike. The reasoning was right and covered half the door.

    `?version=9999` is the same shape on the other parameter: `describe_table` answers off the namespace
    manifest and does not resolve the pin, so every version number a caller tries is confirmed to exist.
    A caller probing for a version gets yes, always. Opening the pinned dataset mints spec error 11
    `TableVersionNotFound`, and it runs only when a version was named, so the unpinned describe every
    client makes is untouched.
    """
    estate.create("descchan", _rowed(("a", "one")))
    estate.branch("descchan", "work")
    table = estate.table_id("descchan")

    query_branch = requests.post(f"{CATALOG}/v1/table/{table}/describe?branch=work", headers=_auth(), timeout=60)
    assert query_branch.status_code == 400, f"?branch=work answered {query_branch.status_code}: {query_branch.text[:180]}"

    body_branch = requests.post(f"{CATALOG}/v1/table/{table}/describe", json={"branch": "work"}, headers=_auth(), timeout=60)
    assert body_branch.status_code == 400, f"the body channel regressed: {body_branch.status_code} {body_branch.text[:180]}"

    absent = requests.post(f"{CATALOG}/v1/table/{table}/describe?version=9999", headers=_auth(), timeout=60)
    assert absent.status_code == 404, (
        f"describing version 9999 of a table that has 1 answered {absent.status_code}: {absent.text[:180]}. "
        "A version that does not exist must not be confirmed."
    )

    # The unpinned and validly-pinned describes must still work — a validation that refuses everything
    # would satisfy the assertions above.
    assert requests.post(f"{CATALOG}/v1/table/{table}/describe", headers=_auth(), timeout=60).status_code == 200
    assert requests.post(f"{CATALOG}/v1/table/{table}/describe?version=1", headers=_auth(), timeout=60).status_code == 200
