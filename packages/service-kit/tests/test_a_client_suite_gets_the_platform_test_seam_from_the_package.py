"""[[XC-190]] A client's suite gets the platform's test seam from `service-kit` alone, with no conftest of this repository.

A client pins `service-kit` by lakehouse tag, and its suite runs in its own repository. The state the platform keeps
across a process (the cache of secret-store reads, the token files its doors' callers read) and the respx default the
estate's suites rely on therefore have to arrive with the package, as a pytest plugin, rather than from this
repository's root `conftest.py`, which no client has. So the suite below runs in a directory outside this repository,
where no conftest of ours reaches it, and is answered by the plugin alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


#: A client's suite, one test per seam the plugin provides. The two reads run in this order (pytest keeps file order):
#: the second sees the store's second answer only when it is given a cache of its own.
_CLIENT_SUITE = """
import os
from pathlib import Path

import respx
from dapr.conf import settings as dapr_settings

from service_kit.governed import secret_cache

READ = secret_cache.StoreRead(store="a-store", secret="a-secret", fields=("k",))


def test_a_first_read_is_the_stores_answer():
    assert secret_cache.STORE_READS.get(READ, lambda: {"k": "first"}) == {"k": "first"}


def test_the_next_test_reads_the_store_again():
    assert secret_cache.STORE_READS.get(READ, lambda: {"k": "second"}) == {"k": "second"}


def test_each_door_has_a_projected_token_file():
    assert {door: Path(os.environ[f"RASK_{door}_IDENTITY_TOKEN_FILE"]).is_file() for door in ("CATALOG", "LINEAGE")} == {
        "CATALOG": True,
        "LINEAGE": True,
    }


def test_the_sidecar_handshake_is_bounded():
    assert dapr_settings.DAPR_HEALTH_TIMEOUT == 1.0


@respx.mock
def test_a_route_nobody_calls_fails_the_test():
    respx.get("http://upstream.invalid/never")


@respx.mock
def test_a_route_registered_to_prove_it_is_not_called_may_stay_uncalled(respx_allows_unused_routes):
    respx.get("http://upstream.invalid/never")
    assert not respx.calls
"""


def test_a_suite_outside_the_repository_is_served_the_seam_by_the_installed_package(tmp_path: Path) -> None:
    (tmp_path / "test_client.py").write_text(_CLIENT_SUITE)
    report = tmp_path / "report.xml"
    # The run inherits none of this suite's own setup: whatever a fixture of ours put into the environment is left out.
    environment = {name: value for name, value in os.environ.items() if not name.startswith(("RASK_", "PYTEST_"))}

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "test_client.py", "-p", "no:cacheprovider", f"--junitxml={report}", "-q"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert report.is_file(), f"the client suite wrote no report:\n{run.stdout}\n{run.stderr}"
    outcomes = {
        case.attrib["name"]: next((child.tag for child in case if child.tag in ("failure", "error", "skipped")), "passed")
        for case in ET.parse(report).getroot().iter("testcase")  # noqa: S314 -- the report is the child pytest's own, written just now
    }
    assert outcomes == {
        "test_a_first_read_is_the_stores_answer": "passed",
        "test_the_next_test_reads_the_store_again": "passed",
        "test_each_door_has_a_projected_token_file": "passed",
        "test_the_sidecar_handshake_is_bounded": "passed",
        "test_a_route_nobody_calls_fails_the_test": "failure",
        "test_a_route_registered_to_prove_it_is_not_called_may_stay_uncalled": "passed",
    }, f"the client suite ran:\n{run.stdout}"
