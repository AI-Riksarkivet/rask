"""The release applies the catalog's database schema, and a catalog pod serves only once it is applied (ADR 0230 §11,
[[LH-613]]).

The migration is a plain Job named by the release revision, applied with the release so `--wait-for-jobs` waits for it
beside the pods (a Helm hook would deadlock that wait), and an Argo CD `Sync` hook. It runs the catalog's own image as
the migrator role, whose credential is a mounted file. Each catalog pod's init step waits for the schema as the
catalog's role, from the same kind of file. Each file is readable by its owner and the pod's group, which the pod names
as the image's user, and by no other account; no database credential rides any environment.
"""

from tests.chart.chart_render import DEFAULT_ARGS, render


_CREDENTIAL_DIR = "/var/run/secrets/rask/database"
#: `USER 10001` in .docker/rest-catalog.dockerfile.
_IMAGE_USER = 10001


def _workload(docs: tuple[dict, ...], kind: str, name: str) -> dict:
    found = [doc for doc in docs if doc.get("kind") == kind and doc["metadata"]["name"] == name]
    assert len(found) == 1, f"the release renders {len(found)} {kind} named {name}"
    return found[0]


def _credential(pod: dict, container: dict) -> tuple[str | None, int | None, int | None, list[str]]:
    """The Secret the pod's `database-credential` volume mounts, its files' mode, the pod's fsGroup, and where
    ``container`` mounts it."""
    secrets = [volume["secret"] for volume in pod.get("volumes") or [] if volume["name"] == "database-credential"]
    mounts = [mount["mountPath"] for mount in container.get("volumeMounts") or [] if mount["name"] == "database-credential"]
    fs_group = (pod.get("securityContext") or {}).get("fsGroup")
    if not secrets:
        return None, None, fs_group, mounts
    return secrets[0]["secretName"], secrets[0].get("defaultMode"), fs_group, mounts


def _secret_env(docs: tuple[dict, ...]) -> list[str]:
    """Every env entry of the catalog's pods and the migration Job that takes its value from a Secret."""
    found = []
    for doc in docs:
        if doc.get("kind") not in {"Deployment", "Job"} or not doc["metadata"]["name"].startswith(("rask-catalog", "rask-catalog-migrate")):
            continue
        pod = doc["spec"]["template"]["spec"]
        for container in [*(pod.get("initContainers") or []), *pod["containers"]]:
            found += [f"{doc['metadata']['name']}/{container['name']}: {entry['name']}" for entry in container.get("env") or [] if "valueFrom" in entry]
            found += [f"{doc['metadata']['name']}/{container['name']}: envFrom" for _ in container.get("envFrom") or []]
    return found


def test_the_release_migrates_the_catalogs_schema_and_each_catalog_pod_waits_for_it() -> None:
    docs = render(*DEFAULT_ARGS)

    job = _workload(docs, "Job", "rask-catalog-migrate-r1")
    catalog = _workload(docs, "Deployment", "rask-catalog")
    job_pod, catalog_pod = job["spec"]["template"]["spec"], catalog["spec"]["template"]["spec"]
    [migrate] = job_pod["containers"]
    assert [init["name"] for init in catalog_pod.get("initContainers") or []] == ["schema"]
    [schema] = catalog_pod["initContainers"]
    [server] = catalog_pod["containers"]

    assert "helm.sh/hook" not in job["metadata"].get("annotations", {})
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "catalog-migrate"
    assert (migrate["image"], migrate["args"], "command" in migrate) == (server["image"], ["python", "-m", "catalog.schema", "migrate"], False)
    assert _credential(job_pod, migrate) == ("rask-database-migrator", 0o440, _IMAGE_USER, [_CREDENTIAL_DIR])
    assert (schema["image"], schema["args"]) == (server["image"], ["python", "-m", "catalog.schema", "wait"])
    assert schema["securityContext"] == server["securityContext"]
    assert _credential(catalog_pod, schema) == ("rask-database-catalog", 0o440, _IMAGE_USER, [_CREDENTIAL_DIR])
    assert _credential(catalog_pod, server) == ("rask-database-catalog", 0o440, _IMAGE_USER, [_CREDENTIAL_DIR])
    assert _secret_env(docs) == []
