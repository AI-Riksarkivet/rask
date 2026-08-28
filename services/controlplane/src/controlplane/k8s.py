"""Kubernetes access for controlplane — read-only listing of Project CRs.

The protocol is the seam: routes depend on `ProjectReader`, tests inject a fake,
production injects `K8sProjectReader`. Client construction — including the kube
config load — is deferred out of `__init__` into `_ensure_clients()`, invoked by
the listing methods. That keeps a config-load failure inside the reader's call
(where the route's try/except can turn it into a 503), not in dependency
resolution (where it would escape as a 500), and still lets unit tests that
override the dependency avoid touching the kubernetes client at all."""

from typing import TYPE_CHECKING, Any, Protocol


if TYPE_CHECKING:
    from kubernetes.client import CustomObjectsApi, NetworkingV1Api


PROJECT_GROUP = "platform.rask.io"
PROJECT_VERSION = "v1alpha1"
PROJECT_PLURAL = "projects"
PROJECT_LABEL = "platform.rask.io/project"


class ProjectReader(Protocol):
    def list_projects(self) -> list[dict[str, Any]]: ...
    def ingress_hosts(self) -> dict[str, str]: ...


class K8sProjectReader:
    """Lists Project CRs via the cluster API (in-cluster config, kubeconfig fallback)."""

    def __init__(self) -> None:
        self._api: CustomObjectsApi | None = None
        self._net: NetworkingV1Api | None = None

    def _ensure_clients(self) -> tuple["CustomObjectsApi", "NetworkingV1Api"]:
        if self._api is None or self._net is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            self._api = client.CustomObjectsApi()
            self._net = client.NetworkingV1Api()
        return self._api, self._net

    def list_projects(self) -> list[dict[str, Any]]:
        api, _ = self._ensure_clients()
        resp = api.list_cluster_custom_object(group=PROJECT_GROUP, version=PROJECT_VERSION, plural=PROJECT_PLURAL)
        items: list[dict[str, Any]] = resp.get("items", [])
        return items

    def ingress_hosts(self) -> dict[str, str]:
        # One cluster-wide list resolves every project's host, so listing N projects costs one
        # ingress call rather than N. Requires cluster-wide `list ingresses` (chart ClusterRole).
        _, net = self._ensure_clients()
        resp = net.list_ingress_for_all_namespaces(label_selector=PROJECT_LABEL)
        hosts: dict[str, str] = {}
        for ing in resp.items:
            ns = ing.metadata.namespace
            if ns in hosts:
                continue
            for rule in ing.spec.rules or []:
                if rule.host:
                    hosts[ns] = rule.host
                    break
        return hosts
