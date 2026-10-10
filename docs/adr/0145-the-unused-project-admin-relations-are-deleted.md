# 0145. The unused project admin relations are deleted (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (CTL-019; `docs/audits/2026-09-25/lakekeeper-deep-read/authz.md` §8 item 1).

## Context

`security_admin`, `data_admin` and `role_creator` are defined on `project` (`model.fga:68-73`). Nothing outside the
model's own assertions references them (`model.fga.yaml:51-53,210-224`), and no door can grant them: `members.py`
grants only admin and member, and ADR 0024 keeps identity administration WONTFIX until an IdP sync.

## Decision

- The three relations are deleted, with their assertions, and `model.json` is regenerated.
- The model stops claiming a separation of duties it does not enforce.

## Consequences

- A check against a deleted relation errors, and one RED test pins that.
- A real admin split, if wanted later, returns as its own row with a door that grants it.
