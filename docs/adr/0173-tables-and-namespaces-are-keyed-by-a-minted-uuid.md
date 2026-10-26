# 0173. Every table and namespace is keyed by a uuid the catalog mints; no name reaches a storage path, a credential's policy or an FGA object (2026-10-21)

**Status:** accepted. Source: owner, 2026-10-21, a grilling round (`/grill-with-docs #684`, LH-512) after LH-319 closed.
Refines ADR 0097 and ADR 0172 (a location's claim also carries its table's uuid).

## Context

A table's name was copied into three strings, each with its own escaping rules, and each copy broke for a name holding a
non-ASCII character, `%` or a space:

- **The storage path.** pylance 13.0.0's `dir` backend places a V2 table at `<hash>_<object_id>` with the name verbatim
  (measured: `8e9edc48_ns1$räksmörgås`, `fb22d9c8_ns1$a b`; a `/` in a name nests a directory). The catalog records the
  location `describe_table` answers, which percent-encodes the name, while the store keeps the decoded key, and pylance
  writes the `.lance-reserved` marker under the encoded spelling. Every reader that compared or listed one spelling
  misread the other: LH-435, LH-510, LH-318 and LH-319 each fixed some readers, and LH-517 found the undrop re-registering
  such a table at its encoded spelling.
- **The credential's policy.** The vend scopes its STS session to the recorded (encoded) prefix, so every vended
  credential for such a table covered none of its bytes: maintenance's open was refused `403 AccessDenied` on helm rev 20
  (LH-517). A write credential's 2,048-byte session policy also fits a table prefix of at most 89 characters (measured on
  MinIO, LH-490), so a long name could not be vended at all.
- **The FGA object.** `table:<name>` reaches OpenFGA, which refuses an object id holding whitespace: on rev 20 the create
  of `sp ace` and a describe of it both answered 503 code 17.

The owner asked first whether such names matter at all. Refusing them at the door was weighed (lancedb 0.40.0, the pinned
version, refuses `räksmörgås`, `growth%` and `sp ace`: "may contain only alphanumeric characters, underscores, hyphens
and periods", measured 2026-10-21), and the owner chose to keep names free and take them out of every key instead.

What `lance_docs/` settles, read in full for this decision:

- The namespace spec identifies an object by its names, unique within the parent (`ns_catalog/namespace/object-relationship.md:40-68`),
  and leaves a created table's location to the implementation: CreateTable takes none (`lance_docs/ns_catalog/spec.yaml:3748-3750`),
  and DeclareTable's is optional, "the namespace implementation should determine the table location"
  (`spec.yaml:3831-3835`). Create is meant as a declaration plus the Lance SDK writing the data
  (`ns_catalog/namespace/operations/index.md:133-159`).
- A table's own business properties are "stored and managed by the namespace implementation outside Lance context"
  (`spec.yaml:3769-3773`); DescribeTable keeps them apart from the metadata that "requires loading the table"
  (`spec.yaml:2902-2912`). The docs disagree on where the `dir` catalog keeps them: in Lance table metadata
  (`supported-catalogs/lance-dir.md:47`), in `__manifest.metadata` for namespaces only (`catalog/dir/index.md:110`);
  pylance 13 writes them to `__manifest.metadata` and drops them on RegisterTable, which the spec allows ("if supported
  by the implementation", `spec.yaml:2719-2724`).
- A Lance table carries no identity of its own. It is anchored at its dataset root, "the location where the dataset was
  initially created" (`lance_docs/lance/format/table/layout.md:14-18`): the external manifest store keys commits by `base_uri`
  (`lance_docs/lance/format/table/transaction.md:752`), and shallow clones and branches reference other roots by path (`lance_docs/lance/format/table/layout.md:141-176`,
  `branch_tag.md:49-85`). A rename that moved bytes would break those references.
- Lance keeps special characters out of its own paths: partition namespaces take random names "to avoid issues with
  special characters" and keep the real values in `__manifest` columns (`ns_catalog/partitioning-spec.md:171-184`,
  `:203-226`); branch and tag names are ASCII-safe (`lance_docs/lance/format/table/branch_tag.md:10-20`, `:89-99`); data files take uuid names
  for store throughput (`lance_docs/lance/format/table/layout.md:195-199`).

Measured on pylance 13.0.0 (`dir` backend, compatibility mode, as the catalog runs it), 2026-10-21:

- DeclareTable refuses any location but the backend's own random `<hash>_<object_id>` (lance `v13.0.0`,
  `rust/lance-namespace-impls/src/dir/manifest.rs:3503-3515`); RegisterTable accepts a relative location under the root.
- The backend has no RenameTable (`NotImplementedError`). rask's rename is its own: `dataplane.rename_table` retires the
  source row and registers the destination at the same location, under the location's claim (ADR 0097).
- `__manifest` "may include additional columns … implementations should preserve unrecognized columns"
  (`catalog/dir/index.md:115`), but the backend's own writes (create, declare, register, deregister) rewrote
  `__manifest` without an added `rask_uuid` column, its values included. A row written directly with a merge on
  `object_id` is seen at once by the backend.
- pylance's Rust `RestNamespace` sends a space in an identifier as `+` and a `+` as `%2B`; the generated urllib3 client
  sends a space as `%20`.

## Decision

1. **Terms** (`CONTEXT.md`): a **Name** is the spec's identifier; a **Table uuid** and a **Namespace uuid** are the keys
   the catalog mints; a **Location** is where a table's bytes live; a **Location claim** records which table holds a
   location, and that table's uuid.
2. **Lifetime.** A table's uuid is minted when it is created, declared or registered, a namespace's when it is created.
   A rename, a recoverable drop followed by undrop, and a table's `create?mode=Overwrite` (the same table, LH-242) keep
   it. Re-creating a dropped name, or registering a location again after its table was destroyed or deregistered, mints
   a new one, so nothing granted to the old object reaches the new one.
3. **Scope.** Tables and namespaces only. Projects and warehouses keep their ids under the control-id rule
   (`CONTROL_ID_PATTERN`); branch and tag names keep Lance's own rules.
4. **Names.** Two names are the same only when they are equal exactly, case included (as Lance's own branch names are,
   `lance_docs/lance/format/table/branch_tag.md:22`); a name not in Unicode NFC is refused 400, never rewritten. A segment is NFC text of 1 to 255
   characters holding no `$` (the delimiter), `+`, `/`, control character, or leading or trailing whitespace; `*` and `?`
   are allowed again, since no name reaches a credential's policy. The catalog reads a `+` in the `{id}` path as a space,
   as pylance's Rust client sends one, and no name can hold a literal `+`. A top-level namespace name keeps the
   control-id rule: it is the routing key that finds the namespace's warehouse through its binding. The delimiter is
   the spec's `$` (`spec.yaml:2314-2318`), which no setting changes, and a request naming another is refused.
5. **Locations.** A table the catalog creates or declares lives at `<warehouse root>/<table uuid>`, a random v4 uuid with
   no suffix: declare writes the `.lance-reserved` marker there and records the row through RegisterTable's relative
   location, the `dir` catalog's own DeclareTable steps with the catalog choosing the location
   (`supported-catalogs/lance-dir.md:132-149`), and create is that declaration plus the Lance SDK writing the data. A
   registered table keeps the location its registrar named, with the register door's spelling rules. The register door
   refuses a location directly under a warehouse root whose name has the shape of a uuid, reserving the catalog's own
   layout; the catalog's own re-registers (rename, undrop) are exempt.
6. **Where a uuid lives.** A table's uuid is in its location's claim (ADR 0097), which create, declare and register now
   take and whose life already matches a uuid's: handed to the new name on rename, kept through a recoverable drop,
   released at destroy (ADR 0172). A namespace's uuid is its `rask.uuid` property, in the namespace's own `__manifest`
   row (`catalog/dir/index.md:110`). A listing reads a catalog-made table's uuid from its location and a registered
   table's from its claim.
7. **Resolution and races.** Every door resolves a name to its uuid and location once, checks FGA on the uuid and acts
   on the location it resolved, never resolving the name again. A door that changes `__manifest` (drop, deregister,
   rename) proceeds only while the claim it read is unchanged (its ETag), and otherwise answers 409.
8. **Keys.** FGA objects are `table:<uuid>` and `namespace:<uuid>`, and the parent and child edges join uuids. Every
   control-root record kept for an object is keyed by its uuid: protection, maintenance policy, trash, promotion holds,
   due records, erasure obligations and releases, cadence stamps. A rename moves no grant and no record.
9. **Undrop.** Undrop and the tasks door stay addressed by name. When several trashed objects hold one name, the door
   answers 409 listing their uuids and deadlines, and the caller names one (`?uuid=`).
10. **Events.** Every lineage and control event carries the table's and its namespace's uuids beside their names.
    `provenance$edges`, lineage's checks and notifications' checks and inbox rows key on the uuid, and
    `lineage.dataset_id` holds it. OpenLineage dataset names stay names.
11. **API.** Every spec and management door stays addressed by name and resolves the uuid on the server
    (`catalog/rest/index.md:39-42` keeps the identifier in the route). DescribeTable and DescribeNamespace answer the uuid
    as a read-only `rask.uuid` property (no caller may set a `rask.*` key, LH-208); grant, review and event responses
    carry the uuid and the name; the estate admin's raw tuples door speaks `table:<uuid>`.
12. **Cutover.** The deploy that lands this rebuilds the estate from git and seeds it through the doors; no re-key code
    ships.
13. **Upstream.** The backend's gaps (a declared location it chose itself, RegisterTable properties dropped, no rename,
    extension columns dropped) are not reported to Lance.

## Considered options

- Refuse such names at every naming door (lancedb 0.40.0's rule): not taken; the owner keeps names free.
- Keep the uuid in a `__manifest` column, the documented extension point (`catalog/dir/index.md:115`) and the home
  Lance's partitioning spec uses for its own values: not possible today, since pylance 13.0.0's own writes drop the
  column (measured). The row's `metadata` properties: dropped by RegisterTable, which rask's rename and undrop use.
- Keep the uuid inside the dataset: refused by the spec, which keeps business properties outside Lance context.
- Keep the uuid in a registry of its own (`_tables/<uuid>.json` with a name index): a second name registry beside
  `__manifest`.
- `<root>/<hash>_<uuid>`, or a time-ordered v7 uuid: the hash adds nothing in front of a random uuid, and v7 uuids made
  together share a key prefix.
- Address the management doors by uuid, as Lakekeeper's management API does: two addressing schemes beside the spec's.
- Compare names case-insensitively (Lakekeeper's collation) or normalize them to NFC: the catalog would answer a name the
  client never sent, and Lance compares names exactly.
- Undrop the most recently dropped table of a name, or refuse a second recoverable drop of a name: the first can restore
  a table other than the caller meant; the second blocks dropping a re-created table.
- Keep names alone in events and resolve them at check time: history splits at a rename, and a check after a rename
  resolves the old name to nothing or to another table.
- Re-key the live estate with a one-shot job: migration code the pre-production rule forbids, writing tuples outside the
  doors.

Lakekeeper was read as a reference only (`b9da6b5`): tables at `<warehouse>/<uuid>`
(`crates/lakekeeper/src/service/storage/storage_layout.rs:205-224`), FGA objects keyed by uuids
(`crates/authz-openfga/src/entities.rs:287-310`), authorization and act keyed on the id resolved once
(`crates/lakekeeper/src/server/generic_tables/load.rs:63-66`), events carrying both id and name. It is not the
justification for any decision above.

## Consequences

- Two departures from the `dir` catalog's own conventions, both deliberate. A catalog-made table's directory is
  `<uuid>`, not `<hash>_<object_id>` (`catalog/dir/index.md:138-146`): it keeps that hash's throughput, re-create safety
  and uniqueness, and drops only "aids debugging", while every V2 reader still finds the table through its row's
  `location` (`supported-catalogs/lance-dir.md:55`). And the uuid lives outside `__manifest`, while pylance drops its
  extension columns.
- For every table the catalog makes, the defect class is gone: no reader compares spellings, the table part of every
  vend's prefix is a 36-character uuid, and no name reaches OpenFGA. LH-517 (#697) is folded into LH-512 (#684), and so is the
  whitespace 503.
- A registered table keeps its registrar's location, so the decoding LH-318 and LH-319 added stays for those.
- Maintenance names a catalog-made table only through `__manifest`: no name parses out of a uuid directory, and a
  dataset no manifest names is reported by its location.
- A namespace's Overwrite ends its subtree's grants and records, so the namespace it leaves gets a new uuid.
- `fga_deps.move_ownership` and `object_records.move_object_records` go: a rename changes the `__manifest` row and the
  claim's holder name, and nothing else.
