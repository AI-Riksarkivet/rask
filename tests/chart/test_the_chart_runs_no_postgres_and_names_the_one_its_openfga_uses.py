"""The chart runs no Postgres: it names the one the catalog keeps its database in and its OpenFGA its store in
([[LH-496]], ADR 0228, ADR 0230).

A Postgres is a platform service. The chart dials one at the host `postgres` names: the development fixture's
(`postgres.enabled`, deploy/fixtures/postgres) or the platform's (`postgres.externalHost`). The catalog always keeps its
database there, so a render naming neither is refused whichever OpenFGA it runs. OpenFGA's connection string is
assembled by the ExternalSecret from that host and the store's `postgres` secret, so no render carries the password.
The fixture is one stock PostgreSQL server, its image named by digest because a tag is mutable and the server holds
every grant of the estate it serves; its provisioning Job runs the same image.
"""

import re
import subprocess
from typing import Any

import pytest

from tests.chart.chart_render import DEFAULT_ARGS, PROD_ARGS, containers, render, render_fixture, render_text


_DATABASE_IMAGES = {"postgres", "age", "cloudnative-pg", "postgresql"}
#: A real deployment that runs the chart's OpenFGA on the platform's Postgres.
_PLATFORM_POSTGRES = (
    *PROD_ARGS,
    "--set", "openfga.enabled=true",
    "--set-string", "postgres.externalHost=pg.platform.example",
    "--set-string", r"openfga.podAnnotations.rask\.dev/openfga-datastore=pg.platform.example:5432/openfga",
)  # fmt: skip


def _datastore_uri(docs: tuple[dict[str, Any], ...]) -> str:
    [credentials] = [doc for doc in docs if doc.get("kind") == "ExternalSecret" and doc["metadata"]["name"] == "rask-infra-credentials"]
    return credentials["spec"]["target"]["template"]["data"]["openfga-config.yaml"]


@pytest.mark.parametrize(
    ("args", "host"),
    [
        pytest.param(DEFAULT_ARGS, "rask-postgres", id="the-development-fixture"),
        pytest.param(_PLATFORM_POSTGRES, "pg.platform.example", id="the-platforms"),
    ],
)
def test_the_chart_renders_no_database_server_and_its_openfga_dials_the_host_it_names(args: tuple[str, ...], host: str) -> None:
    docs = render(*args)

    servers = sorted(
        f"{workload}/{name} ({container['image']})"
        for workload, name, container in containers(docs)
        if re.split(r"[:@]", container["image"].rsplit("/", 1)[-1])[0] in _DATABASE_IMAGES
    )
    servers += sorted(f"{doc['kind']}/{doc['metadata']['name']}" for doc in docs if doc.get("kind") in {"StatefulSet", "Cluster"})
    [openfga] = [doc for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "rask-openfga"]
    assert servers == []
    assert openfga["spec"]["template"]["metadata"]["annotations"]["rask.dev/openfga-datastore"] == f"{host}:5432/openfga"
    assert f'uri: "postgres://lance:{{{{ .pgPassword }}}}@{host}:5432/openfga?sslmode=disable"' in _datastore_uri(docs)


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        pytest.param(
            ("--set-string", "postgres.externalHost=pg.platform.example"),
            "postgres.enabled says this estate uses the development Postgres (deploy/fixtures/postgres) and postgres.externalHost names the platform's: set one",
            id="both-named",
        ),
        pytest.param(
            ("--set", "postgres.port=6432"),
            'openfga.podAnnotations.rask.dev/openfga-datastore is "rask-postgres:5432/openfga", but OpenFGA\'s Postgres is "rask-postgres:6432/openfga"',
            id="an-address-the-running-openfga-would-not-follow",
        ),
        pytest.param(
            ("--set", "postgres.enabled=false"),
            "postgres.enabled is false, so the Postgres holding the catalog's database is the platform's: set postgres.externalHost to its host",
            id="neither-named",
        ),
        pytest.param(
            ("--set", "postgres.enabled=false", "--set", "openfga.enabled=false", "--set-string", "auth.fgaApiUrl=https://fga.example.com"),
            "postgres.enabled is false, so the Postgres holding the catalog's database is the platform's: set postgres.externalHost to its host",
            id="neither-named-beside-the-platforms-openfga",
        ),
    ],
)
def test_a_postgres_the_chart_cannot_name_or_a_running_openfga_would_not_follow_is_refused(settings: tuple[str, ...], reason: str) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*DEFAULT_ARGS, *settings)

    assert reason in refused.value.stderr


def test_a_real_deployment_is_refused_the_development_postgres() -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render_text(*PROD_ARGS, "--set", "postgres.enabled=true", "--set-string", "postgres.externalHost=")

    assert "postgres.enabled=true — the development Postgres (deploy/fixtures/postgres), one server on one volume with no backup" in refused.value.stderr


def test_the_development_postgres_is_one_stock_server_named_by_digest_whose_password_is_the_stores() -> None:
    docs = render_fixture("postgres")

    images = {container["image"] for _, _, container in containers(docs)}
    [auth] = [doc for doc in docs if doc.get("kind") == "ExternalSecret" and doc["metadata"]["name"] == "rask-postgres-auth"]
    [server] = [doc for doc in docs if doc.get("kind") == "StatefulSet"]
    mounted = [volume["secret"]["secretName"] for volume in server["spec"]["template"]["spec"]["volumes"] if "secret" in volume]
    assert [re.fullmatch(r"postgres:16\.\d+@sha256:[0-9a-f]{64}", image) is not None for image in images] == [True], images
    assert (auth["spec"]["secretStoreRef"]["name"], auth["spec"]["data"]) == (
        "rask-vault",
        [{"secretKey": "password", "remoteRef": {"key": "postgres", "property": "password"}}],
    )
    assert mounted == [auth["spec"]["target"]["name"]]
