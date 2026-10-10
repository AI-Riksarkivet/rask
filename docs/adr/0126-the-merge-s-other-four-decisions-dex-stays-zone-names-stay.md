# 0126. The merge's other four decisions: Dex stays, zone names stay, e2e is extended, NATS HA is parked (2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:561-564` (at `44b354f3`) (proposed decisions 2-5 of the merge plan, 2026-07-24, restated with survey evidence "not relitigated"), with the defaults at `:469`.

These four are recorded together because each is a short hold-the-line decision with no later ruling
attached. Decision 1 is [0125](0125-age-on-cnpg-via-imagevolume-behind-its-own-gate-merge.md). Decision 4 here
(extend the e2e suite) is NOT the "decision 4" that chart comments cite for the gateway; that one is naming
rule 4, recorded in [0117](0117-nginx-is-retired-and-the-fastapi-gateway-is-the-in-cluster.md).

## Context

The merge plan wrote five decisions with status PROPOSED and restated them after surveying both repositories.

## Decision

2. **Dex stays; a Keycloak-to-FGA seam comes later.** rask contained no Keycloak, OIDC or auth code; Dex plus
   the sealed-cookie BFF was the only working auth in either repository. The seam is env-parameterised end to
   end (`makeOidcConfig(env)` is issuer-agnostic; `frontend.oidc.publicIssuer` is the single knob), so a
   later Keycloak is a new issuer value, a subject-sync job into the same FGA tuple space and callback URIs.
3. **Zone names stay as they are.** The two zone sets are disjoint except both homes (rask's home absorbs
   lance's auth and landing) and the `/data`-as-project catch-all trap (a reserved-segment guard). The chart
   corollary: the `web-` object prefix becomes universal for zones.
4. **Extend rask's `tests/e2e`, do not replace it.** Purely additive: the Playwright package gains the merged
   zones' specs, the Python live suites land at `tests/e2e-py`, and the Dagger module plus the Makefile are
   the execution vehicle.
5. **NATS HA and the nack operator stay parked (#20).** One NATS subchart with lance-ns's richer values;
   lance-ns's stream job and Dapr pub/sub are its first real consumers.

## Consequences

- `frontend.oidc.publicIssuer` remains the issuer knob (`chart/values.yaml:884`); Dex is a bundled dev
  stand-in for a platform-run IdP in production.
- Decision 3 was later overtaken for specific zones by the zone rulings
  ([0113](0113-the-zone-set-r8-r9-r15-r16-r17-r18-2026-07-27.md)) and the explorer rename; the reserved-segment
  guard and the `web-` prefix are what it still governs.
- Whether NATS HA has since been taken up was not checked for this record.
