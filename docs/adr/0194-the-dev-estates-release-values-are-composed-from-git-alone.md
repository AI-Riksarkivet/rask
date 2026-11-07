# 0194. The dev estate's release values are composed from git alone (2026-11-07)

**Status:** accepted. Source: XC-194 (#772), shipped with XC-195 (#773) as one coherent change. Taken after LH-563
(ADR 0192), so the client grants the live values carried had moved to the catalog's access doors before the values
holding them were dropped. Relates to XC-164 (#658): an install from git that does not converge under a reconciler.

## Context

Measured 2026-11-06 at main `0faa294c3` against the live release, rask helm revision 12 (`helm get values rask`, keys
only), by the parity audit against Lakekeeper `9124ea8`:

- **`make k3s-up` fed the live release back into every upgrade.** It passed `helm get values rask` as `-f $LIVE`, so a
  value that existed only in the release was carried forward by every upgrade, and the estate was not what git
  declared.
- **The estate admin came from no file.** `auth.bootstrapAdmin` was set in the release, but git set it only for the e2e
  lane (`scripts/stack_values.sh`). An install from git seeded no estate admin, so nobody held `admin` on `estate:rask`
  and nobody could mint a project.
- **Client admissions no file at HEAD named:** `auth.externalDeciders["default:rask-sa-compute"]` and
  `services.lineage.provenanceMaintainers: ["default:rask-sa-table-maintenance"]`. Dropping them by an install from git
  would have removed compute's decider and stopped provenance compaction, after which lineage's feed tables gain a
  fragment per event (the LH-529 cost drift).
- **Dead keys the chart no longer read:** `explorer`, `frontend`, `route.edgeUrl`, and `image.tags` for `flows`,
  `gateway` and `media`. Nothing refused them: `chart/values.schema.json` set no top-level `additionalProperties`.

GitOps is the first of the four facts every design is judged by: the estate is what git declares, and a reconciler
(Argo CD, Flux, the planned rask operator) re-applying the repository converges it. A release that remembers values no
file holds cannot converge that way, and a schema that admits any key cannot tell a typo or a removed seam from a
setting.

## Decision

- **`make k3s-up` composes the release's values from git files and derived flags, and reads nothing back from the
  release.** Every upgrade passes `--reset-values`, `-f deploy/values-local.yaml`, where its images come from
  (`--set-string image.repository=$(K3S_REGISTRY)`, the dev registry `localhost:5000` by default, or
  `image.localImages=true` with `K3S_REGISTRY=` for side-loaded images), the pins file, and three derived values: the
  issuer (`DEV_ISSUER`), the vended store endpoint (`DEV_VENDED_ENDPOINT`, from the node's address and the RustFS
  fixture's node port), and the estate admin. There is no `-f $LIVE`.
- **Image tags come from the pins file alone.** `deploy/values-live-pins.yaml`, gitignored per-host state, is written
  by `make k3s-pins`: captured from what the cluster runs, or set for a new build with `make k3s-pins TAGS="<stem>=<tag>
  …"`, which reads no cluster. While `K3S_REGISTRY` is set, `K3S_PINS_CHECK` refuses a pins file that lacks a tag or
  digest for any image in `K3S_IMAGES` (`web-console`, `lance-rest-catalog`), naming it, before anything else in
  `make k3s-up` and `make k3s-values`: the release would otherwise fall back to the chart's `dev` and wait out `--wait`
  in ImagePullBackOff. `make k3s-converge STEM=<stem> TAG=<tag>` writes the tag into the pins file, runs the same check,
  and upgrades with the same composition, naming the pins file unconditionally, so the next `make k3s-up` deploys it.
- **The estate admin is derived**, as the live lanes derive theirs: the Dex fixture's user `DEV_ADMIN_EMAIL`
  (`alice@example.com`) resolved to its principal by `scripts/dex_principal.py` from the fixture's own values, passed as
  `auth.bootstrapAdmin`. `k3s-values-check` refuses to compose when that principal or the vended endpoint comes out
  empty.
- **`make k3s-values` prints the composition** through a server dry run, which writes nothing, as
  `helm get values rask -o json` must read after the upgrade.
- **The values schema refuses an undeclared top-level key.** `scripts/chart_docs.sh` generates
  `chart/values.schema.json` with helm-schema's `--schema-root.additional-properties=false`, so a values file naming a
  key the chart does not declare fails `helm template`. `image.tags` and `image.digests` admit only the two images
  `rask.image` renders (`lance-rest-catalog`, `web-console`, as `patternProperties`), so a pin for a departed image fails
  too.

## Considered options

- **Keep `-f $LIVE` and prune the dead keys by hand:** rejected. A hand-run prune is a step an install from git would
  miss, and the next value set by hand would drift the same way.
- **`--reuse-values`:** rejected for the same reason: it is `-f $LIVE` under another name, and it keeps an old key
  while a renamed one deploys at its default.
- **Commit the pins file:** not taken. Tags are per host and per build, and a committed file would be rewritten by every
  deploy ([[XC-152]]); the composition reads it as the one per-host input.

## Consequences

- After an upgrade, `helm get values rask -o json` equals what `make k3s-values` prints, key for key. A value set by
  hand on the release is dropped by the next `make k3s-up`.
- An install from git seeds the estate admin, who reads `estate_admin: true` from `GET /management/v1/me`.
- A client's admissions reach the dev estate only as values its own repository supplies with `-f`, or as grants made at
  the catalog's access doors (ADR 0192); none survives in the release by being remembered.
- A values file carrying a removed or misspelt top-level key fails the render instead of passing silently, so removing a
  seam from the chart is enforced on every install.
- A deploy from the dev registry needs the build's tag named first (`make k3s-pins TAGS=…`); a side-loaded deploy runs
  with `K3S_REGISTRY=` at the `dev` tag when no pins file exists.
