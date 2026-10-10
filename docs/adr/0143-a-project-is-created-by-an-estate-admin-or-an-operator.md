# 0143. A project is created by an estate admin or an operator (2026-10-10)

Source: owner decision, 2026-10-10 grilling session (D2, LH-076).

## Context

`POST /v1/projects` checks `can_observe_events` (estate `owner`) at
`services/catalog/src/catalog/api/v1/endpoints/projects.py:184`. The model already defines
`can_create_project: admin or operator` (`packages/service-kit/src/service_kit/governed/auth/model.fga:145`), and
`model.fga.yaml:1590-1615` asserts that an operator can create a project and cannot observe events, but no door checks
it. A project's creator becomes its `admin`, and therefore `warehouse#owner` of everything under it. A warehouse create
provisions a bucket.

## Decision

- Project creation checks `can_create_project`: a person holding estate admin, or a provisioning machine identity
  holding `operator`.
- This is Lakekeeper's `server.can_create_project`. An operator gains no event feed and no storage browsing from it.

## Consequences

- The door repoints from `can_observe_events` to `can_create_project`, and the rest of the work is LH-076's.
- Self-service tenant creation is not offered.
