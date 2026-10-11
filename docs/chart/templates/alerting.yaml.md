# chart/templates/alerting.yaml

Rationale for `chart/templates/alerting.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $al := .Values.observability.alerting | default dict }}`

Nil-safe gate: under `helm upgrade --reuse-values` from a release whose stored values predate the
alerting key, a bare .Values.observability.alerting.enabled nil-pointers the whole render — the default-dict
guard makes the missing key mean "off" instead (the reuse-values landmine).

## `{{- $fullname := include "lance.fullname" . }}`

The alerting ENGINE (docs/adr/0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md "P3b"): vmalert evaluates the proven rules (chart/alerting/rules.yml, proven to fire by
`make alert-rules-check`) against GreptimeDB's PromQL endpoint every evaluationInterval, and hands firing
alerts to Alertmanager, which groups + routes them. Gated on observability.alerting.enabled (prod). The one
piece render-checking can't prove is the live vmalert->GreptimeDB query round-trip + a real receiver route —
that's a prod drill (docs/adr/0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md "P3b"). webhookUrl empty => Alertmanager groups alerts but
pages nowhere (black-hole); set it to a Slack/PagerDuty webhook to actually page.
