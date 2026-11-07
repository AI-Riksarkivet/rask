# 0189. The product chart carries no engine seam: a bring-your-own engine is a client like any other (2026-11-01)

**Status:** accepted, amended by [ADR 0192](0192-a-services-roles-are-granted-at-the-catalogs-doors-scoped-to-the-tenants-it-serves.md) (neither extension point remains: a client's roles are grants made at the catalog's access doors). Source: XC-182 (#694), widened, un-parked and counted by the owner on 2026-11-01 through the
multi-question tool, after that day's boundary map of what the lakehouse still renders for its clients. Amends
[ADR 0153](0153-the-app-chart-is-the-product.md), whose decision had the lakehouse name an engine endpoint through its
settings (`ray.dashboardUrl` and its token reference).

## Context

ADR 0153 took the engine out of the chart and kept one seam to it: an address and a token reference. ADR 0155 then
took the stage runner and compute out of the lakehouse, and no lakehouse service has read either value since. Read off
the chart on 2026-11-01, the seam rendered:

- a Ray auth token the dev store generated and seeded, and an ExternalSecret that only the engine and the compute
  fixture read;
- `rask.rayDashboardUrl`, whose one reader was the install notes;
- a Hugging Face token ExternalSecret and its seed, a third party's credential that only runners use;
- an object-store NetworkPolicy peer admitting any pod labelled `ray.io/is-ray-node: "yes"`, on by default. A pod can
  stamp that label on itself, so the peer trusted a pod by its position;
- the Collector's `ray-pods` scrape job, with no guard at all;
- the `ray` alert group's five rules, one of which (RayMetricsMissing) fires on an estate that runs no engine;
- `RASK_LINEAGE_ENDPOINT` and `RASK_LINEAGE_ENDPOINT_PATH` on the catalog and maintenance, which neither reads:
  lineage-kit's HTTP emitter reads them, and only runners build one.

Lance describes an engine's relation to a catalog as a client's: "compute engines interact with catalogs through the
Lance Namespace interface", and Ray, Spark and DataFusion "can interact with Lance tables through this interface ...
while remaining agnostic to the underlying catalog deployment" (`lance_docs/ns_catalog/namespace/index.md` § Engine
Interoperability). Nothing in that relation needs the catalog to know the engine's address, hold its token, scrape it
or admit it by label.

## Decision

- **The product chart names no part of a bring-your-own engine**: no address, token, scrape job, alert, network peer or
  engine-only credential. Its `ray` and `secrets` values are gone, and the render gate
  `tests/unit/test_the_product_chart_renders_no_client.py` refuses the engine, compute, Kueue, the compaction job and
  a model-download token wherever the chart renders a string.
- **An engine reaches governed tables as every client does**: through the catalog's namespace doors, on the
  credentials the catalog vends. A deployment whose engine workers read the object store directly admits them through
  `networkPolicy.objectStoreExtraFrom`, by a selector the deployment states, never by a label the chart trusts.
- **A client is admitted by its ServiceAccount at one of two extension points**: `auth.externalDeciders` for a
  companion that asks the catalog's decision door (compute), `services.lineage.provenanceMaintainers` for a job that
  compacts the provenance tables. They are the only values that may name a client, and admitting one changes only the
  objects they feed.
- **What an engine and its companions need of their own is theirs to provide**, in their own releases: the Ray auth
  token the engine enforces and compute presents, a runner's model-download token, a scrape of the engine and alerts
  on it. In this repository that is the compute and ray-cluster fixtures; after the split, the compute repository.
- **A lineage producer outside the lakehouse configures its own transport.** lineage-kit's HTTP emitter takes its
  endpoint from the pod that builds it, and the lakehouse's pods carry no lineage endpoint, since they announce on the
  bus.

## Consequences

- The dev store generates and seeds no Ray token, and its ESO policy no longer grants one. A live estate's
  `<release>-dev-credentials` Secret keeps the key it was created with (`refreshPolicy: CreatedOnce`), and nothing
  reads it; a fresh install generates three keys.
- Ray auth in development stays off, as it was. Turning it on takes a Secret the engine's owner creates, which the
  ray-cluster fixture's `authSecretName` and the compute fixture's `rayAuth` name.
- Nothing scrapes the engine, so its own series no longer reach the telemetry store, and the alerts and dashboard
  panels on them are gone. compute's own series (`ray_control_*`, which it exports over OTLP) still arrive; their
  alerts and panels go with the `ray` group and the Ray dashboard, to be rebuilt in compute's release. Observing the
  engine is the engine owner's.
- The prod-credentials guard no longer refuses `ray.auth.token` or `secrets.hfToken`: no template reads either, so a
  value under either reaches no pod. A value under a key no template reads is still stored in the release, as any
  unknown key is, until `values.schema.json` refuses unknown keys (XC-086 #289). A release a value file or an earlier
  `-f $LIVE` gave these values keeps them as live-only state: the upgrade that removes them passes
  `--set-json 'ray=null' --set-json 'secrets=null'` once, so the release converges to what git declares, and an install
  from git never carries them.
- The openbao fixture mounts no operator-supplied credentials: the lakehouse uses no third-party credential. The
  mount's removal changes the dev OpenBao's pod spec, so the fixture's next apply rolls it by surge, which carries the
  signing pairs and the bus trust root over (docs/OPERATORS.md § 6-8); its pod is never deleted to apply it.
