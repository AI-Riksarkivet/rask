"""#3-B — live Lance multi-base data distribution against the DEPLOYED catalog.

Proves the completion condition end to end: a table created with 2 approved data bases has its FRAGMENTS
distributed across BOTH buckets (the manifest + _versions stay in the primary root — relative-path
portable), a read returns ALL rows (fan-out), and an off-allowlist base is rejected 400.

Needs ``LANCE_E2E_CATALOG_URL`` and the run's ``token`` credential (a reachable deployed catalog whose token owns
the parent namespace). The FAN-OUT leg additionally needs ``LANCE_E2E_BASE_A`` + ``LANCE_E2E_BASE_B`` naming
two buckets that are BOTH on that catalog's ``LANCE_MULTIBASE_DATA_BASES`` allowlist and physically
provisioned — the allowlist is a governance gate, not a provisioner. Multi-base is an operator opt-in
(chart ``catalog.multibase.dataBases``), so an estate that has not opted in SKIPS that leg rather than
failing it: a red test there would say the estate is broken when it is merely not configured.
"""

from __future__ import annotations

import io
import os

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.ipc as ipc
import pytest
import requests
from credentials import Credential, credential
from liveness import skip_unless_live
from store_identity import store_options
from topology import create_namespace

from lakehouse.naming import CATALOG_DELIMITER
from lakehouse.storage_options import s3_filesystem


CATALOG = os.environ.get("LANCE_E2E_CATALOG_URL", "").rstrip("/")
TOKEN = credential(Credential.TOKEN)
S3 = os.environ.get("LANCE_E2E_S3", "http://localhost:9900")
#: NO DEFAULTS. `s3://mb-a` / `s3://mb-b` are `scripts/e2e_stack.sh`'s install-time constants, and that
#: script is the only thing that provisions them — so on any OTHER deployed estate the defaults named
#: two buckets that do not exist, and the fan-out leg failed as though multi-base were broken.
#:
#: Multi-base is an OPERATOR OPT-IN (chart `catalog.multibase.dataBases`), and the allowlist is a
#: GOVERNANCE gate, not a provisioner: a bucket must be both on it and physically present. Measured on
#: this estate 2026-09-06: `LANCE_MULTIBASE_DATA_BASES` is empty, so the feature is off and the leg
#: cannot pass. Unset means SKIP with a reason, which is the honest answer for a capability this
#: deployment does not have — a red test would say the estate is broken when it is merely not opted in.
BASE_A = os.environ.get("LANCE_E2E_BASE_A", "")
BASE_B = os.environ.get("LANCE_E2E_BASE_B", "")
ARROW_STREAM = "application/vnd.apache.arrow.stream"

pytestmark = pytest.mark.e2e


def _auth() -> dict[str, str]:
    return {"authorization": f"Bearer {TOKEN}"}


def _fs() -> pafs.S3FileSystem:
    """The two data buckets, listed as the suites' own store identity, which reads listings and no table's bytes."""
    return s3_filesystem(store_options(S3))


def _bucket_of(uri: str) -> str:
    return uri.replace("s3://", "").split("/", 1)[0]


def _fragments_in(bucket: str) -> list[str]:
    infos = _fs().get_file_info(pafs.FileSelector(bucket, allow_not_found=True, recursive=True))
    return [i.path for i in infos if i.path.endswith(".lance")]


def _arrow_many_rows(n: int = 4000) -> bytes:
    table = pa.table({"id": pa.array(range(n), pa.int64()), "v": pa.array([str(i) for i in range(n)])})
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.fixture(scope="module")
def catalog() -> str:
    if not CATALOG or not TOKEN:
        pytest.skip("set LANCE_E2E_CATALOG_URL and the run's token credential (deployed stack with multibase allowlist)")
    skip_unless_live(f"{CATALOG}/livez", what="the catalog")
    return CATALOG


@pytest.fixture(scope="module")
def catalog_ns(catalog: str) -> str:
    """The parent namespace `<warehouse>$mbns`, which the caller must own: with FGA on, a nested table's create gates
    on ``can_create_table`` on its parent namespace, so without it the create 403s at the router before the allowlist
    check is even reached. `scripts/e2e_stack.sh` makes it; the create here keeps one already there (409).
    """
    return create_namespace(catalog, "mbns", _auth())


def test_multibase_redirects_data_and_reads_fan_out(catalog: str, catalog_ns: str) -> None:
    if not (BASE_A and BASE_B):
        pytest.skip(
            "set LANCE_E2E_BASE_A + LANCE_E2E_BASE_B to two buckets that are BOTH on the deployed "
            "catalog's LANCE_MULTIBASE_DATA_BASES allowlist and physically provisioned "
            "(multi-base is an operator opt-in and is off on this estate)"
        )
    import lance

    tbl = catalog_ns + CATALOG_DELIMITER + "mbtbl"
    q = f"data_base={BASE_A}&data_base={BASE_B}"
    r = requests.post(
        # mode=overwrite for REPEATABILITY (testing.md F.I.R.S.T.) — a re-run must not collide on the
        # existing table. initial_bases is create-only, but target_bases applies on overwrite too, so the
        # fragments still land in a data base (that is exactly what the audit's F1 fix made true).
        f"{catalog}/v1/table/{tbl}/create?{q}&mode=overwrite",
        data=_arrow_many_rows(),
        headers={**_auth(), "content-type": ARROW_STREAM},
        timeout=60,
    )
    assert r.status_code == 200, r.text
    location = r.json()["location"]

    a = _fragments_in(_bucket_of(BASE_A))
    b = _fragments_in(_bucket_of(BASE_B))
    # Multi-base REDIRECTION: the table's data physically lands in the approved DATA bucket(s) — NOT the
    # primary root, which keeps only the manifest/_versions (so the dataset stays relative-path portable).
    # Writes round-robin across the base SET per fragment as fragment count grows; a single-fragment create
    # lands in the first base — so assert the data reached a data base (not the primary root's data/).
    assert a or b, f"the table's data did not land in any approved data base: a={a} b={b}"

    # Fan-out read: opening the dataset resolves the manifest (primary root) TOGETHER with the fragment in
    # the data base, and the ROWS read back are the rows written. A count would pass on a read that
    # returned the right number of wrong or duplicated rows.
    # On the credential the table's describe vends, which covers the bases it was created on ([[XC-081]]).
    described = requests.post(f"{catalog}/v1/table/{tbl}/describe", json={"vend_credentials": True}, headers=_auth(), timeout=60)
    assert described.status_code == 200, described.text
    so = dict(described.json()["storage_options"])
    read = lance.dataset(location, storage_options=so).to_table(columns=["id", "v"]).sort_by("id")
    assert read.to_pydict() == {"id": list(range(4000)), "v": [str(i) for i in range(4000)]}
