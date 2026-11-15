---
name: rask-dapr
description: "The ruling that the lakehouse runs no Dapr, and where each need a Dapr building block could meet is met in rask instead: the bus (lakehouse.bus), secrets (lakehouse.governed.secrets), fixed estate timers (lakehouse.timers), per-object schedules (NATS message schedules), workflows (none). Use when tempted to add Dapr, a sidecar, a dapr.io annotation or a Component; when a queue, schedule, lock, config or secret seam seems to need a framework; when adding a periodic tick; or when a client asks how its own sidecar reaches the lakehouse."
---

# rask × Dapr — the lakehouse runs none

**The ruling** ([[XC-199]], #787, ADR 0199): the lakehouse runs no Dapr. No lakehouse pod runs a sidecar, the product
chart renders no `dapr.io` object and no `dapr.io/` annotation or label in any mode, the repository installs no Dapr
control plane, and no lakehouse package imports `dapr`. `tests/unit/test_invariants.py::test_no_lakehouse_pod_runs_a_sidecar_and_the_chart_renders_no_dapr_object`
holds the render to it in the default, local and production modes.

## Where each need is met

| Need | Met by | Where |
| --- | --- | --- |
| Publishing an event, taking a delivery | each service's own NATS JetStream connection as its own bus user, presenting its pod's ServiceAccount token over TLS (no stored bus credential; the bus's signer grants a session of at most `nats.auth.sessionSeconds`) | `lakehouse.bus` (`Bus.consume`, `Bus.follow`), settled by `lakehouse.bus_delivery.judged`; the permission table is `nats.auth.users`, rendered for the signer as ConfigMap `<release>-nats-permissions` |
| Reading a secret | each service's own store login (Kubernetes auth as `<release>-<app>`, its pod's projected token) | `lakehouse.governed.secrets`; who reads what is `lance.storeReads` in `chart/templates/_helpers.tpl` |
| A fixed estate tick (once a window, whatever the replica count) | a loop in each replica that claims the window with one conditional write of `_timers/<timer>.json` | `lakehouse.timers` |
| A time per object (a table's next maintenance pass) | a NATS message schedule on the worker's own queue | ADR 0198 decisions 5 and 6, `maintenance/services/queue.py` |
| A workflow or saga | none: the lakehouse runs no workflow engine | ADR 0154, import contract `the-lakehouse-is-not-built-on-a-workflow-engine` |
| Service-to-service calls | plain HTTP with the caller's verified bearer; public traffic reaches each app port through the platform's Gateway | ADR 0165, `rask.apiRoutes` |
| Configuration | each service's settings rendered as `env` on its own container by the chart, read by pydantic-settings at boot | the render is the gate (`tests/unit`) |

A new capability that seems to need a framework is built on one of these seams, or on a library that solves it, never
on a sidecar.

## What keeps it out

- ruff `TID251`: `"dapr"` is a banned API in the root `pyproject.toml` (`[tool.ruff.lint.flake8-tidy-imports.banned-api]`).
- import-linter: `.importlinter` forbids `dapr` as an external package for every lakehouse package (`make lint-imports`).
- the render test above, over the chart's own templates.

## Clients

A client lives in a repository of its own (XC-192). Its release may run its own sidecar beside its own pods and bring
its own Components, Configurations and Resiliency; the product chart renders none for it and names no client. A client
that subscribes to the lakehouse's events through a sidecar takes the CloudEvent the lakehouse publishes in structured
mode (`application/cloudevents+json`, its `id` as the message's `Nats-Msg-Id`, `lakehouse.cloud_events`); a client
that reads the lakehouse's secret store does so under its own role, declared from its own values in
`openbao.externalReaders`. A client's bus user is a row of `nats.auth.users` from its own values: with a `serviceAccount`
it is a session user that presents that ServiceAccount's token (audience `nats.auth.audience`) to the bus and holds no
credential; without one it is a static user whose `nats-user-<app>` the client reads under its own store role.

Two things are the client's to ship, and the lakehouse's to keep stable:

- **Its own egress.** With `networkPolicy.enabled` the product's default-deny selects every pod in the namespace, a
  client's included, and the product chart admits no pod to a sidecar control plane. A client's release ships the
  NetworkPolicy that admits its pods to its control plane's namespace.
- **Its store login.** A client's secret-store Component logs in with Kubernetes auth as the role `<release>-<app>`,
  presenting a ServiceAccount token for the audience `openbao.identity.audience`: the role name is a published
  contract. Its policy reads exactly the secrets its `openbao.externalReaders` entry names, and may renew its token.
