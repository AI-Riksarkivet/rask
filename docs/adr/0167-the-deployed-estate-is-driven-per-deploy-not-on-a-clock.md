# 0167. The deployed estate is driven per deploy, not on a clock; the e2e lane runs every module it can serve (2026-10-18)

**Status:** accepted. Source: owner, 2026-10-18, two decisions on XC-033 (#208) through the multi-question tool: record
the deployed-estate cadence as dropped rather than schedule a host timer, and run the maintenance module on push with
no telemetry backend in the lane ("OpenTelemetry is fixed, but not Perses or GreptimeDB").

## Context

ADR 0001 made `scripts/e2e_stack.sh` the live proof: CI installs the governed stack on kind, runs the e2e suites and
fails on any skip. Its first session named its suites as a list of files, and nothing held the list to `tests/e2e-py`:
on 2026-10-17, 10 of the 26 modules ran in no CI lane, six of them lakehouse modules (a stock client's branch commits,
the erasure drill, the registry's conditional create, the container tier's deletes, the deployed authorization model,
the deployed maintenance sweep). The three Dagger lanes that run an e2e module ran it with skips allowed. The deployed
estate was driven by hand (`scripts/e2e_live.sh`), and XC-033 asked for that to be scheduled or recorded as dropped.

## Decision

1. **The e2e-stack lane runs every `tests/e2e-py` module it can serve, chosen by the markers a module does not
   carry.** A module that needs what the lane does not stand up carries a marker the lane leaves out, each with its
   reason beside it in the script: `phase1` (Phase 1's acceptance, a session of its own), `nats_auth` (a lane of its
   own), `observability`, `edge`, `media_catalog` and `user_state`. A new module runs on push the day it lands, and one
   the lane cannot serve fails the lane under `--require-live` instead of running nowhere. The Dagger lanes that run
   an e2e module (`governance-chain`, `rustfs-lifecycle`, `nats-auth`) run it under `--require-live` as well.
2. **The deployed estate is driven per deploy, not on a clock.** Each row's read-back drives it after the deploy, and
   Phase 1's soak reads a kept drive a day later (XC-090). No timer drives it.
3. **The lane runs no telemetry chain and asserts nothing about telemetry.** OpenTelemetry is rask's boundary, and
   the store and dashboards behind the Collector (GreptimeDB and Perses in development) are the platform's, so the
   lane installs neither, deploys no Collector (`observability.enabled=false`), and no module it runs reads a store.
   Maintenance is on in the lane and its deployed sweep runs on push; no test asserts the sweep's metric once it
   leaves the service. That a service's metric crosses rask's Collector is `test_observability_e2e.py`'s claim, made
   for lineage's metric against a deployed estate.

## Considered options

- **A nightly host timer running `scripts/e2e_live.sh --require-live`.** It would see drift between deploys: a
  rotation that breaks a door, a filling store, a parked event. Rejected: a systemd timer on a developer's host is not
  cloud native, its failures land in a log nobody reads, it writes scratch tenants into the live estate every night,
  and a canary against production belongs to the platform that runs it. Every lakehouse module running on push
  against a fresh install is the stronger proof of the code, and the deploy's read-back is the proof of the release.
- **Installing the observability fixture in the lane, so the maintenance module's GreptimeDB assertion runs as
  written.** Rejected: it makes the development stand-ins for the platform's store part of rask's CI, and ties a
  lakehouse claim to a backend the platform can swap.
- **Adding the six modules to the lane's list of files.** The smallest change, and the shape that left them out: a
  list nothing checks drifts from the directory it names.

## Consequences

- `test_maintenance_e2e.py` asserts the deployed sweep and no longer reads its counter in a store; no test reads that
  counter in a store.
- `scripts/stack_values.sh` turns maintenance on, so the install-from-empty lane, which shares those values, proves
  the maintenance service starts from empty too.
- A drift on the deployed estate between deploys is seen at the next deploy's read-back or the next soak, not sooner.
