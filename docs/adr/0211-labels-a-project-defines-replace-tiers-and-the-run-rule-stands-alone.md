# 0211. Labels a project defines replace tiers, and the run rule stands alone (2026-11-15)

**Status:** accepted. Source: owner, 2026-11-15, through the question tool: "Labels you define + a run rule". Row LH-596
(#833). Supersedes [ADR 0160](0160-the-tier-judge-owns-the-write-rung-and-a-tier-is-declared-once.md) and amends
[ADR 0156](0156-the-audit-grilling-amends-0155.md) C8.

## Context

`rask.tier` (`bronze`, `silver` or `gold` on a namespace) was a vocabulary rask invented when the lakehouse ran its own
medallion stages. That vocabulary did two jobs:

- **Classifying.** A namespace said which layer it was.
- **One guarantee.** A write into a declared `silver` or `gold` namespace had to name its run and its inputs.

Neither format nor protocol has tiers:

- Lance's docs use "tier" only for hot/cold storage and the MemWAL (lance `main` a33c4682d).
- lance-namespace keeps free-form `properties` on namespaces and tables (`lance_docs/ns_catalog/spec.yaml`).

So the platform fixed one layering pattern on every user, and tied a real guarantee to it.

The reference answers:

- **Lakekeeper** keeps governance tags. A **definition** belongs to a project and has a name, a scope (the object kinds
  it applies to) and a value kind: a marker, free text, or enumerated with allowed values. An **attachment** applies a
  definition to an object, with a value. The scope and allowed values only grow, the kind never changes, and a
  definition is deleted only once nothing carries it. Its own tests define a tag named `tier` with the value `silver`,
  as a user's choice. Under OpenFGA, tags change no decision.
- **lance-namespace**: properties are a key/value map on the object, written at create.

## Decision

1. **`rask.tier` is deleted.** Removed with it:
   - the judge's tier reading (`TIERS`, `DERIVED_TIERS`);
   - the tier refusals of register, update, add_columns and rename;
   - `can_declare_unowned_tier`;
   - the console's tier badge, filter and Home counts;
   - every test and live suite that declared a tier;
   - `docs/TIER-GOVERNANCE.md`.

   A namespace create naming `rask.tier` is refused 400, like any other reserved key. Nothing reads a tier anywhere. No
   compatibility read is kept (the estate is pre-production).
2. **The run rule is its own opt-in namespace property.** Its key is `rask.requires_run`, its only value `"true"`.
   - **Who declares it.** The namespace's project admin declares it at create (`can_administer` on the project), or the
     estate admin where no project owns the namespace (`can_declare_unowned_run_rule` on the estate root). Dropping it
     takes the catalog's Overwrite and the same admin, as a tier did.
   - **Inheritance.** Every namespace beneath one that declares it is bound by it, whatever it declares itself.
   - **What a write must carry.** Under it, every write that adds data names its run and inputs. The rule reuses the
     tier's mechanism unchanged:
     - catalog doors: the spec Context headers `x-rask-run-id` and `x-rask-inputs`;
     - stock commits: the `rask.*` transaction properties at `version/create`.

     It also keeps the same refusals: `update`, `add_columns` and `register` are refused 406 under the rule, and so is a
     rename into a namespace under the rule from one that is not.
   - **The judge.** It is `catalog.core.write_judge` (it judges every write door, as `tier_policy` did). It audits
     `write_judge` with `requires_run`.

   Nothing reads the rule from a name.
3. **Labels a project defines.**
   - **Definitions.** A project's admin defines its labels at
     `POST /management/v1/projects/{project}/labels/define`. Each has a name, a scope (`namespace`, `table`), a kind
     (`marker`, `free_text`, `enumerated` with `allowed_values`) and a description.
     - The scope and allowed values only grow, and the kind never changes, so every attached label stays valid.
     - `POST .../labels/{name}/delete` refuses 409 (code 3) while any object of the project carries the label.
     - A project member or the estate admin reads the definitions (`GET .../labels`). Anyone else is answered the
       missing project's 404.
   - **Attachments.** An object's owner attaches and removes labels: `POST /management/v1/{table|namespace}/{id}/
     labels/{set,remove}`, at the new `can_label` relation (`owner_effective`, the protection rung's twin, under its own
     name). The value is judged against the definition. `labels/describe` reads them at `can_get_metadata`.
   - **Finding labelled objects.** `GET .../labels/objects?label=` lists the project's labelled objects the caller may
     see. Each is judged on its uuid and named as it is named now.
   - **Records.** Two control-root registries:
     - `_label_definitions/<project>/<blake2s(name)>.json`, one per name, so the store arbitrates a project's names;
     - `_labels/<record_name(kind, uuid)>`, one per object, keyed by its uuid.

     A rename keeps an object's labels, and the object's ending ends them (`lakehouse.object_records`). A project's
     delete ends its definitions, as it ends its maintenance policy.
   - **Announcements.** Each change is announced untargeted: `label_defined`, `label_deleted`, `table_labelled`,
     `table_unlabelled`, `namespace_labelled`, `namespace_unlabelled`.
   - **Labels grant and refuse nothing.** No door reads one to decide.
4. **The console shows and filters by labels where it showed tiers.** It lists the definitions on a project's page, shows
   and edits an object's labels on its Overview, and gives the Tables and Namespaces lists a label column and filter.

## Not taken, stated

- **Labels on warehouses and columns.** A project's warehouse is the project's own tenancy, and a column already carries
  `rask.classification`, which authorizes (LH-058). Neither is asked for here.
- **Inherited ("effective") labels.** A namespace's labels are not answered as its tables'. The listing finds what each
  object carries directly. A completeness sweep reads the namespaces too.
- **Per-definition delegation of who may apply a label.** The object's owner applies any label of its project.
- **A label read by a policy.** Labels authorize nothing. Contracts (LH-594, #832) are the governed rules a table carries.
- **A listing scoped by its key.** Every project's attachments share `_labels/`, keyed by the object's uuid alone so an
  object's ending finds them without knowing its project; a project's listing and a definition's delete therefore read
  every project's attachment records. Labels change at admin frequency, so the read is bounded by the estate's labelled
  objects, not its tables.
- **An attachment names the definition it was judged against.** Each label on an object records its definition's uuid.
  A read shows a label only while that definition stands, and only a standing object (not gone, not in the trash) holds
  a definition against its delete. So an attachment that outlives its definition, made while the delete read them or
  kept by a trashed object, classifies nothing, never blocks a delete, and is not revived when the name is defined
  again. An attach re-resolves its object's name after the write and takes the record back where the object ended
  meanwhile; a window of one read remains, in which an ended object's record stays, shown by no read.

## Consequences

- **The vocabulary is the user's.** A project that wants bronze, silver and gold defines an `enumerated` label for them.
  One that wants the run guarantee declares `rask.requires_run` on the namespaces its derived data lands in.
- **Four facts:**
  - GitOps: definitions and attachments are made at doors any producer's repository can call idempotently, and nothing
    is live-only.
  - Cloud native: no new workload.
  - Lance-namespace idiomatic: the run rule is a namespace property, labels are catalog records over the uuids of
    lance-namespace's objects, and no vocabulary Lance lacks is imposed.
  - Zero trust: the admin and owner rungs set them, and labels grant nothing.
