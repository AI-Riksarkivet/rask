# 0153. The app chart is the product; operators, compute and observability are prerequisites (2026-10-11)

Source: owner, 2026-10-11, in a `/grill-with-docs` session over XC-085 (#216) with XC-049 (#198) folded in. Supersedes
the no-split half of ADR 0038.

## Context

rask's chart (`chart/`) installs rask and the platform it runs on: nine subcharts (nvidia-device-plugin,
kuberay-operator, NATS, Dapr, Kueue, OpenFGA, CloudNative-PG, GreptimeDB, Perses), rask's own RayCluster and
RayService, and 65 templates. Helm stores every revision in one Secret capped at 1,048,576 bytes; revision 297 is
stored at about 98% of it. Measured on that render: Kueue's templated CRDs and rask's template source fill most of it,
and 376 KB of the 887 KB of template source is `{{/* */}}` prose.

How peer projects ship, read 2026-10-11:

- Helm's chart best practices, `custom_resource_definitions.md` "Method 2: Separate Charts": the CRDs in one chart,
  the resources that use them in another, each installed separately, "more useful for cluster operators who have admin
  access to a cluster".
- Lakekeeper (`lakekeeper/lakekeeper-charts`): one chart, 16 templates (44 KB), two optional subcharts, Postgres and
  OpenFGA, whose README calls the bundled database "not production ready" and points at an external one. It
  recommends the CloudNativePG operator for production Postgres and does not ship it.
- Apache Polaris (`apache/polaris`, `helm/polaris`): one chart, no subcharts, 18 templates (69 KB); production lists "a
  PostgreSQL or MongoDB database" as a prerequisite; development gets throwaway databases from
  `helm/polaris/ci/fixtures/` ("for testing purposes only").
- Apache Gravitino (`dev/charts`): one chart per deployable; its lance REST server chart has no dependencies, the main
  chart only optional databases.
- KubeRay (`ray-project/kuberay`, `helm-chart/`): the operator chart (CRDs in `crds/`, installable separately, or the
  operator alone with `--skip-crds`) apart from the `ray-cluster` chart, whose prerequisite is the operator.
- Dapr installs its control plane once per cluster into `dapr-system` with its own chart; Kueue is installed once per
  cluster by an administrator and admits batch Jobs, Kubeflow jobs, RayJobs, RayClusters, JobSets and Pods alike.
- Lakekeeper and KubeRay document chart settings as `# --` comments in `values.yaml` and generate the README with
  helm-docs; Polaris uses the same comment syntax.

## Decision

- **The product chart holds rask and nothing else**: its services, zones, their configuration and RBAC, installed with
  `helm install rask` and a values file. rask's own custom resources of a prerequisite stay in it (Dapr Components,
  Configurations and Resiliency, NATS stream definitions, the OpenFGA store and model), and the chart refuses to render
  when the prerequisite's API is absent rather than installing it.
- **Compute is bring-your-own and not in the chart**: no KubeRay, no Kueue, no nvidia-device-plugin, no RayCluster or
  RayService template. The lakehouse names an engine endpoint through its settings (`ray.dashboardUrl` and its token
  reference) and knows nothing of how that engine runs.
- **Operators and shared servers are documented prerequisites**: Dapr, NATS JetStream, and the observability chain
  (an OTLP endpoint; GreptimeDB, Perses, vmalert and Alertmanager are the platform's). CloudNative-PG leaves with them.
- **The lakehouse's own stores are optional subcharts**, the Lakekeeper model: OpenFGA and the Postgres behind AGE and
  OpenFGA, enabled for development and pointed at external instances in production through references.
- **Development and CI get prerequisites from fixtures**, kept out of the product chart and labelled not for
  production, as Polaris's `ci/fixtures` are. A make target may wrap them.
- **Templates carry no essays.** Each setting is described by a one-line `# --` comment in `values.yaml`, helm-docs
  generates the chart README, and longer rationale lives under `docs/chart/`. The move is proved by a render that is
  byte-identical before and after.

Idea taken from: Lakekeeper's and Polaris's charts, and Helm's separate-charts method for CRDs.

## Consequences

- The release stops approaching the Secret ceiling: on revision 297, Kueue out alone reaches about 74%, the prose move
  alone about 72%, and those two with `alerting/rules.yml` and the `values-*.yaml` files out of `chart.files` about 41%.
  The rest of the split takes it lower.
- Removing a CRD-owning subchart deletes its CRDs and every object of their kinds, so each operator's CRDs are annotated
  `helm.sh/resource-policy=keep` and handed to their new release before the subchart leaves. Kueue's CRDs are also used
  by another team's workloads on the shared cluster; rask stops owning them, it does not delete them.
- `make k3s-up` installs fixtures, then the product chart, as separate releases.
- How an engine runs Ray (a standing cluster, a RayJob per run, Kueue admission) is the engine owner's decision; ADR 0064
  and CP-053 belong to the engine side, not to this chart.
- XC-090's install-from-empty proof runs against this shape, so the split lands before it.
