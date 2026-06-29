"""Kubernetes access for controlplane — read-only listing of Project CRs.

The protocol is the seam: routes depend on `ProjectReader`, tests inject a fake,
production injects `K8sProjectReader`. Keeping the real client construction lazy
(in `__init__`, only built when the dependency is actually resolved) means unit
tests that override the dependency never touch the kubernetes client."""

from typing import Any, Protocol


PROJECT_GROUP = "platform.rask.io"
PROJECT_VERSION = "v1alpha1"
PROJECT_PLURAL = "projects"


class ProjectReader(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...


class K8sProjectReader:
    """Lists Project CRs via the cluster API (in-cluster config, kubeconfig fallback)."""

    def __init__(self) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._api = client.CustomObjectsApi()

    def list_projects(self) -> list[dict[str, Any]]:
        resp = self._api.list_cluster_custom_object(
            group=PROJECT_GROUP, version=PROJECT_VERSION, plural=PROJECT_PLURAL
        )
        items: list[dict[str, Any]] = resp.get("items", [])
        return items
