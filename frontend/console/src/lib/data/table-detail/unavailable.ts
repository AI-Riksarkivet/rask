// CAPABILITY STATEMENTS — the words the UI uses instead of offering a door that can only fail.
//
// The catalog wires all 54 Lance Namespace ops, but six of them are `NotImplementedError` stubs in
// the native Rust `DirectoryNamespace` the chart pins, so they answer 501 for every caller on every
// table (`docs/COVERAGE.md`). `alter_table_backfill_columns` is one of them.
//
// A control wired to such an op is a dead end wearing an affordance: it is not refused for a wrong
// argument or a missing grant, it is refused always, and the user only finds out after committing
// to the action. The estate's ruling (#143) is that a refused action stays VISIBLE and carries its
// reason — so the reason is written once, here, quoted at the control by `SchemaSection.svelte` and
// pinned by `stubbed-ops.test.ts`.

/**
 * Why the per-column backfill control can never run — and the door that DOES do the job.
 *
 * `add_columns` is not that door, despite taking the same SQL expressions: it CREATES a column
 * (the "Add column" form at the foot of the same section) and cannot write into an existing one,
 * nor bound the write to a subset of rows. `update` can do both — `[[column, expression]]` with an
 * optional predicate — which is exactly what a bounded backfill is, and it is backed (200) through
 * the dataplane.
 */
export const BACKFILL_UNAVAILABLE =
	'Backfilling a column is not implemented by the catalog namespace backend — it answers 501 for every table, so this is a capability statement, not a transient failure. The "Update / delete rows" section below does the same job: SET this column to a SQL expression, optionally bounded by a predicate.';
