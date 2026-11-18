# 0220. Pod-to-pod transport encryption is the platform's; rask serves no TLS between its own pods (2026-11-18)

**Status:** accepted. Source: LH-580 (#807), finding H5 of the 2026-11-18 whole-lakehouse audit, decided by the owner
on 2026-11-18: "Yes: the platform's, recorded in an ADR".

## Context

The audit reported that a pod presents its projected ServiceAccount token over `http://`, in two places.

1. **To OpenFGA** (`RASK_FGA_API_URL`). On a real deployment OpenFGA is the platform's, named by `auth.fgaApiUrl`,
   and the render already refuses an address that is not `https://`: `chart/templates/prod-credentials.yaml` lists
   every hop that leaves the release (OpenFGA, the object store and its STS, the vended endpoint, the OTLP gateway,
   the lineage forward target, the secret store, and the bus over `tls://`) and fails the render on a plaintext one,
   pinned by `tests/chart/test_no_credential_is_carried_by_the_render.py`. The `http://` the audit read is the
   address of the bundled development OpenFGA, which the same template refuses on a real deployment.
2. **Between the release's own pods.** Lineage and maintenance present their `rask-catalog` token to the catalog at
   `http://<release>-catalog:<port>` (`RASK_FGA_DECIDER_URL`, `MAINTENANCE_CATALOG_URL`), in every deployment.

The second is the question. A bearer token that crosses the pod network in clear can be read by whoever can observe
that network. Two answers both meet the four facts: rask serves TLS on its own Services, with certificates from the
platform's issuer, or pod-to-pod encryption is the platform's, as its network is.

Read in source for comparison (lakekeeper `b205a41`, lakekeeper-charts `20a2131`): Lakekeeper's server binds plain
TCP and has no TLS setting (`crates/lakekeeper-bin/src/main.rs:487`), its chart terminates TLS at the Ingress only
(`charts/lakekeeper/values.yaml:344-348`), addresses its in-cluster OpenFGA at `http://…svc…`
(`templates/config/secret-config-envs.yaml:105`) and renders no NetworkPolicy. `lance_docs/` says nothing on
transport: it is not Lance's or the namespace spec's subject.

## Decision

**Encryption of traffic between pods is the platform's**: the CNI's transparent encryption (Cilium's WireGuard or
IPsec) or a service mesh with mTLS (Istio, Linkerd). rask renders no certificate for its own Services, its servers
bind plain HTTP inside the cluster, and its first-party clients dial `http://` Service names.

What rask itself holds on that hop, and keeps:

- **The token is bound to its audience and short-lived.** Each first-party caller presents a projected
  ServiceAccount token for the one audience its callee checks (`rask-catalog` for the catalog's doors), which the
  kubelet rotates and the chart requests at the 600-second minimum. A token read off the wire is useless at any other
  service, and at the catalog for at most its remaining life.
- **The callee authenticates every request itself** (ADR 0165): position on the network admits nothing.
- **Under `networkPolicy.enabled`, ingress is denied by default** and a first-party port admits the pods of the
  release's own namespace and what the chart names beside them (`chart/templates/network-policy.yaml`).
- **Every hop that leaves the release is `https://` or `tls://`**, refused at render on a real deployment.

A deployment whose platform encrypts nothing between pods accepts that a party able to read the pod network can
replay a first-party token against its one audience for up to ten minutes. `docs/architecture/deployment.md` states
the prerequisite.

## Consequences

- No chart value, certificate, mount or uvicorn TLS flag is added, and no certificate rotation becomes rask's.
- The audit's H5 is closed as measured: its OpenFGA half was already refused at render; its first-party half is
  this decision.
- A platform that wants rask to verify its peers by certificate gets that from its mesh's mTLS without a change here.
- If a deployment must run on a cluster with neither CNI encryption nor a mesh, in-cluster TLS on the catalog's and
  lineage's Services is the change to make, as a row of its own: this ADR is reopened then.
