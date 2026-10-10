# 0024. Team/role administration — WONTFIX until the Keycloak sync (2026-07-23)

**Decision.** No UI or API surface for administering the *identity-shaped* tuples — `team:<t>#member`,
`role:<r>#assignee`, `project:<p>` `team`/`member` — is built. Per the gateway-checks entry above, these
tuples become **event-synced from the IdP** at rask-merge time (a Keycloak event listener writes
`team#member` / `role#assignee` tuples as group/role membership changes in the IdP); building a manual
admin surface now would create a second writer that fights the sync from day one. Resource-shaped tuples
(warehouse/namespace/table rungs) stay app-written and already have the GrantsPanel surface.

**Interim runbook** — until the sync lands, an operator administers identity tuples with the `.localbin/fga`
CLI directly (the same invocation `scripts/e2e_stack.sh` and `scripts/seed_medallion_fga.sh` use;
`SID` = the store id those scripts resolve, api-url = the port-forwarded OpenFGA):

```sh
# put a user on a team (model.fga: team.member accepts [user])
fga tuple write --api-url http://localhost:8081 --store-id "$SID" user:alice member team:eng
# assign a role to a user, a whole team, or another role (role.assignee: [user, team#member, role#assignee])
fga tuple write --api-url http://localhost:8081 --store-id "$SID" user:bob assignee role:validators
fga tuple write --api-url http://localhost:8081 --store-id "$SID" team:eng#member assignee role:validators
# make a team own a project (project.team: [team] — members inherit project admin)
fga tuple write --api-url http://localhost:8081 --store-id "$SID" team:eng team project:acme
# revoke = the same triple with `tuple delete`
fga tuple delete --api-url http://localhost:8081 --store-id "$SID" user:alice member team:eng
```

**Rationale.** The model deliberately routes team access through roles (resource rungs do not accept
`team#member` directly — `packages/service-kit/src/service_kit/governed/auth/model.fga`), so identity administration is a *membership*
concern, which is exactly what an IdP owns. Writing it twice (manual surface now, sync later) buys a
reconciliation problem for a capability the CLI already covers.
