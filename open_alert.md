# open_alert.md — the estate does not page, and what it would take

**Working plan, 2026-08-17.** Delete this file when the decision below is made and executed.

**The one-line state:** `chart/values.yaml:2176` says `alerting.enabled: false`. Twenty-three alert
rules exist, are valid, are proven to fire against synthetic series — and are evaluated by nothing.
Nobody is paged for anything, ever.

> Three figures below were restated 2026-08-23 by the dapr/otel work, which moved them: the rule count
> (15 → **23**, eight added — outbox age, workflow lanes, bus health), the values.yaml line, and the
> telemetry-health row. **The decision this file asks for is untouched and still open.**
>
> **Restated AGAIN later on 2026-08-23, and this time §6 was wrong rather than stale.** The rule count
> is now **29**. More importantly, two of the rules this file leans on **could never have fired**:
> `MaintenanceSweepNotCompleting` and `NotificationsReconcilerStalled` were written as
> `X == 0 or absent(X)`, which promtool accepts and GreptimeDB refuses outright — HTTP 500 and HTTP 400
> respectively, measured against the live store. Both are split and fixed; see §1 and §6. The estate's
> telemetry backend is also now scraped and covered by four rules of its own. **None of that changes the
> decision this file asks for.** Turning on a chain whose rules are now genuinely sound is a better
> proposition than it was, and it is still the same decision.

That is not a gap in the sense of "missing work". Everything except the decision is already built.

---

## 1. What already exists (verified, not remembered)

| Piece | State |
| --- | --- |
| `chart/alerting/rules.yml` | **29 alerts**, all with a `for:` window (23 + the two absence-halves split out + four for the telemetry backend) |
| `chart/alerting/rules_test.yml` | synthetic-series tests — the rules are proven to FIRE against PROMETHEUS. Every alert now has a case, gated by `test_every_alert_rule_has_a_promtool_case`; six did not until that gate was added |
| `make alert-rules-check` | `promtool check rules` + `promtool test rules`, wired and green — and **not sufficient on its own**, see §6 |
| `make alert-rules-drill` | **NEW.** Replays every rule against a REAL GreptimeDB and asserts each evaluates. This is the half promtool cannot do: it checks the LANGUAGE the production engine accepts, not the logic. Skips with no reachable datasource |
| `chart/templates/alerting.yaml` | renders vmalert + Alertmanager + both ConfigMaps |
| `alerting.vmalertImage` / `alertmanagerImage` | pinned (`vmalert:v1.106.1`, `alertmanager:v0.28.0`) |
| Telemetry itself | **NOT healthy — corrected 2026-08-23.** Collector and Perses are up, but `rask-greptimedb-standalone-0` is at **restartCount 13, OOMKilled**, still looping at the raised 8Gi ceiling. This row previously read "running 19 days", which was uptime measured on a pod that had been restarting throughout. It sharpens the case rather than weakening it: the one store that would hold the evidence for any alert is itself the thing failing unwatched. **Four rules now cover it** (`GreptimeDBRestarting`, `GreptimeDBMemoryHigh`, `GreptimeDBDown`, `GreptimeDBMetricsMissing`), fed by a new `greptimedb` scrape job in the collector — it was scraped by NOTHING before, and no `kube_*`/`container_*` series exist anywhere in the estate, so all 13 OOMKills were invisible by construction. Those rules still evaluate nowhere until the decision below is made |

So the rules are written, tested, templated and imaged. The estate collects telemetry and draws
dashboards. The only thing absent is the component that turns a series into a page.

## 2. Why the OTel Collector cannot be that component

Raised repeatedly as the obvious answer, so here is the measurement rather than the argument. Of the
15 rules:

- **all carry a `for:` window** (0m–1h; every one but `GreptimeDBRestarting`, which is deliberately
  `for: 0m` because one restart is already the event) — "sustained", which needs memory of prior evaluations.
- **most use `rate()` or `increase()` over a range** — which needs stored history to query.
- **eight fire on ABSENCE** (`== 0` over a window, or `absent()`) — up from four, because the two
  composites named below were each split into a condition rule and an absence rule.

That last group is decisive and is not a configuration problem. A Collector pipeline is driven *by
arriving data*: no data means no pipeline execution means no alert. The rules that matter most —
`NotificationsReconcilerMetricsMissing`, `MaintenanceSweepMetricsMissing`, `DaprSchedulerServingNoSidecars`,
`DaprSchedulerMetricsMissing`, `GreptimeDBMetricsMissing` — all fire precisely when data STOPS. A
streaming processor cannot notice its own silence.

Two of those names changed on 2026-08-23 and the reason is worth keeping: the absence behaviour used
to live inside `NotificationsReconcilerStalled` and `MaintenanceSweepNotCompleting` as an
`or absent(...)` branch, and GreptimeDB rejects that whole expression. So the argument in this section
was sound and two of the four rules it rested on were dead. The argument is unchanged; the evidence
for it is only now true.

The Collector transports. Something else has to remember and ask.

## 3. GreptimeDB CAN do it — on an edition this estate does not run

**This is the correction that motivated the file.** GreptimeDB owns alerting as a product feature:
`TRIGGER` defines rules in SQL, runs a pending → firing → inactive state machine, and emits a webhook
payload that is **Alertmanager-compatible with no glue code**. Architecturally that is exactly the
right home — the database that already stores the series evaluates them, and one component disappears.

The blocker is the edition, not the capability:

- Deployed: `docker.io/greptime/greptimedb:v1.1.1` — the **open-source** image.
- Greptime's docs, on the Trigger reference page: *"This feature is only available in the GreptimeDB
  Enterprise database."*

So "delegate it to GreptimeDB" is a correct instinct that this deployment cannot act on today.

## 4. The decision — three options, none of them work-in-progress

**A. Turn on what is already built.** Set `alerting.enabled: true` and give `alerting.webhookUrl` a
real receiver. The black hole is no longer a warning, it is **measured**: rendering
`values-prod.yaml` with alerting on produces an Alertmanager whose entire receivers list is
`- name: default`, with **zero** `webhook_configs` — and `scripts/prod_render_check.sh` exits 0 on it,
printing "alerting on". A fully-Ready, fully-green paging chain that pages nobody. If option A is
taken, the template should `required` that value so an empty receiver fails the deploy loudly. Two values, one `helm upgrade`. Cost: two more components to run, and vmalert is
an extra moving piece the owner has twice said they do not want.

**B. GreptimeDB Enterprise.** Alerting collapses into the database already running; rules become SQL;
vmalert and its ConfigMap are deleted. Costs a licence, and the 15 PromQL rules must be rewritten as
SQL triggers — mechanical, but not free, and `rules_test.yml`'s synthetic-series proof does not carry
over.

**C. Decide the estate does not page.** Delete `chart/alerting/`, `chart/templates/alerting.yaml`,
the `alerting:` values block and `make alert-rules-check`. Record it in `docs/DECISIONS.md`.

**There is no fourth option where the Collector or Perses covers this.** Perses is dashboards only.

## 5. Why leaving it as-is is the worst of the three

Fifteen rules that evaluate nowhere is the same failure mode as every defect closed in this session:
**a thing that reads as coverage and is not.** Someone finding `chart/alerting/rules.yml` and
`make alert-rules-check` green will reasonably conclude the estate alerts on reconciler stalls and
dead-lettering. It does not. Either half of that should be made true — turn it on, or take it out.

## 6. What is NOT missing

Stated so a reader does not go looking:

- Metrics themselves. The fleet exports RED metrics and traces via `service_kit.setup_otel`;
  GreptimeDB answers PromQL; the notification plane's own counters
  (`notifications.feed.gaps`, `notifications.dlq.parked`) land beside them.
- ~~The rules' correctness. `promtool test rules` proves each one fires on the series it targets.~~
  **This was FALSE, and it is the one claim in this file that was not merely stale.** promtool is
  Prometheus; production is vmalert against GreptimeDB, and the two do not accept the same PromQL.
  Two rules were valid Prometheus, green under `promtool test rules`, and refused by GreptimeDB with
  HTTP 500/400 — so they could not have fired even with the chain switched on. Fixed, and
  `make alert-rules-drill` now proves the other half against the real engine. The lesson generalises
  beyond this file: **a gate that runs a different engine than production is not a proof, and its
  green is worse than no gate, because it is read as one.**
- Dashboards. Perses serves the "Fleet — RED" board.

The chain is complete from instrument to storage to display. It is broken only at *evaluate → notify*.

---

**Sources for §3:** <https://docs.greptime.com/reference/sql/trigger-syntax/> ·
<https://www.greptime.com/blogs/2025-12-23-trigger-quick-start>
