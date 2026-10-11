# deploy/fixtures/observability/chart/templates/alerting.yaml

Rationale for the observability fixture's `templates/alerting.yaml`, kept beside the fixture rather than in its
templates. Each section is headed by the template line it explains.

## `{{- $rules := required "alerting.rules: ...`

The alerting ENGINE (docs/adr/0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md "P3b"): vmalert evaluates the proven rules (deploy/alerting/rules.yml, passed in with --set-file by scripts/fixtures.sh and
proven to fire by `make alert-rules-check`) against GreptimeDB's PromQL endpoint every evaluationInterval, and hands firing
alerts to Alertmanager, which groups + routes them. The one
piece render-checking can't prove is the live vmalert->GreptimeDB query round-trip + a real receiver route —
that's a prod drill (docs/adr/0032-p3b-alerting-rule-logic-proven-hermetically-the-live.md "P3b"). webhookUrl empty => Alertmanager groups alerts but
pages nowhere (black-hole); set it to a Slack/PagerDuty webhook to actually page.

## `- -datasource.headers=Content-Type:application/x-www-form-urlencoded`

vmalert v1.106.1 POSTs each query with its parameters in the URL and no Content-Type, and GreptimeDB v1.1.1 refuses
that with HTTP 415 ("Form requests must have `Content-Type: application/x-www-form-urlencoded`"), so every rule loads
and none evaluates. Measured 2026-10-11 against the fixture: the same POST with this header answers 200 (an empty
form body leaves the URL parameters in force), as do a GET and a form-bodied POST. `make alert-rules-drill` queries
with GET, which is why it never saw this.
