# 0160. The tier judge owns the write rung, and a tier is declared once (2026-10-13)

**Status:** accepted. Source: owner, 2026-10-13, two grilling rounds while taking LH-470 (#595). Refines ADR 0156 C8.

## Context

ADR 0156 C8 makes a table's tier the reserved namespace property `rask.tier`, set only by a project admin, and has every
write door ask one `tier_policy.judge(op, table, caller, run)`. It does not say which permission "the tier's permission"
is, how a tier changes, which namespace governs a nested one, or which routes are write doors. Neither the Lance
Namespace spec nor pylance 13's `DirectoryNamespace` has an operation that alters a namespace's properties (measured
2026-10-13). The `dir` backend refuses every create mode on an existing namespace, so the only mutation is the catalog's
own `CreateNamespace` with `mode=Overwrite`, which drops an empty namespace and creates it again with the new properties.
The `dir` backend keeps those properties as JSON in the `__manifest` row's `metadata` column (`lance_docs/namespace.md`),
which no table-scoped vend reaches. Lakekeeper was read as the reference: it has one authorizer trait asked inside each
handler, a contract verifier that never sees the caller, no hold outcome, and no test that every write route is
authorized.

## Decision

- **The tier's permission is the writer rung on the tier's namespace.** The judge asks the rung the route declares, so it
  owns the write decision rather than adding to the router's. Writing silver or gold does not need `can_promote`:
  approval under the tag gate is the `can_promote` act (ADR 0156 C1), and a writer that needed it could approve its own
  held version.
- **Every non-read route on a table or materialized view reaches the judge**, owner-rung doors included, along with the
  table creates (create, declare, register), and it does so with FGA off too. A rename is judged on its destination's
  tier. The contract test classifies the live route table with the same resolver the router uses, so no list is kept by
  hand.
- **A tier is declared once, at namespace create.** A create that names `rask.tier`, or an Overwrite that would add,
  change or drop one, needs `can_administer` on the namespace's project, or the estate admin where no project owns the
  namespace. Changing the tier of a populated namespace means a new namespace and a rename. No backend record is
  rewritten.
- **A table's tier is its nearest enclosing namespace's declaration.** A deeper namespace may not declare a different
  tier from an ancestor's (400). A table with no declaration above it is untiered: the judge asks only the rung.
- **Maintenance stops sizing fragments per tier.** The sizes were read from dataset paths. Lance's own sizing applies
  unless a maintenance policy sets `target_rows`, and the rewrite leaves maintenance with LH-467 anyway.
- **The annotator's name-derived `can_promote` door is deleted.** The catalog's judge decides the tier. `namespace_for`
  stays as a default target NAME and claims nothing about a tier.

## Considered options

- Requiring `can_promote` for silver/gold writes (the `model.fga` header's wording), or new per-tier relations: rejected
  for the self-approval hole and for a grant surface no existing grant carries.
- A management door that alters a tier: rejected, because it would have to rewrite the `dir` backend's `__manifest` row,
  which no Lance operation does.
- Maintenance reading `rask.tier` per dataset per tick: rejected, as a catalog dependency added for code LH-467 deletes.
