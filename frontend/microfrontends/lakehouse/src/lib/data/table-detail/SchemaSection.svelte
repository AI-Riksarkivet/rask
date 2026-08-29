<script lang="ts">
	// #74 schema evolution — the schema table plus add (name + SQL expr) / rename / re-type / drop
	// columns. All writer-gated (can_write_data); the catalog's refusal is surfaced verbatim.
	//
	// BACKFILL IS STATED, NOT OFFERED. This section used to wire a fourth per-column action to
	// `alter_table_backfill_columns`, which is one of the six ops the pinned native `dir` backend
	// stubs — a 501 for every caller on every table (`docs/COVERAGE.md`), never a wrong argument or a
	// missing grant. The control stays on screen carrying its reason (#143: show disabled, never
	// hide) and the work itself is done by the "Update / delete rows" section, which SETs an existing
	// column to a SQL expression under an optional predicate. See `./unavailable`.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — an editor opened on table A cannot leak into B, by
	// construction rather than by a hand-written reset list.
	import { GatedAction } from '@rask/ui/gated-action';
	import { Select } from '@rask/ui/select';
	import { RETYPE_TYPES } from '../catalog';
	import { addColumn, dropColumn, renameColumn, retypeColumn } from '../remote/catalog.remote';
	import { typeName, type SchemaField } from './type-name';
	import { BACKFILL_UNAVAILABLE } from './unavailable';

	let {
		table,
		fields,
		onchanged,
	}: { table: string; fields: SchemaField[]; onchanged: () => Promise<void> } = $props();

	let colBusy = $state(false);
	let colError = $state<string | null>(null);
	let addColName = $state('');
	let addColExpr = $state('');
	let renaming = $state<string | null>(null); // the column currently being renamed
	let renameTo = $state('');
	let retyping = $state<string | null>(null); // the column currently being re-typed (#74 tail)
	let retypeTo = $state(''); // the target scalar Arrow type (bits-ui Select string)

	function colFail(status: number, detail: string): void {
		if (status === 401) colError = 'Sign in to change the schema.';
		else if (status === 403)
			colError = 'Denied: schema changes need writer access (can_write_data).';
		else colError = detail;
	}

	async function runAddColumn(): Promise<void> {
		const name = addColName.trim();
		const expr = addColExpr.trim();
		if (colBusy || !name || !expr) return;
		colBusy = true;
		colError = null;
		try {
			const res = await addColumn({ table, name, expression: expr });
			if (res.ok) {
				addColName = '';
				addColExpr = '';
				await onchanged();
			} else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	async function runDropColumn(name: string): Promise<void> {
		if (colBusy) return;
		colBusy = true;
		colError = null;
		try {
			const res = await dropColumn({ table, name });
			if (res.ok) await onchanged();
			else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	async function runRenameColumn(): Promise<void> {
		const from = renaming;
		const to = renameTo.trim();
		if (colBusy || !from || !to) return;
		colBusy = true;
		colError = null;
		try {
			const res = await renameColumn({ table, path: from, rename: to });
			if (res.ok) {
				renaming = null;
				renameTo = '';
				await onchanged();
			} else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}

	async function runRetypeColumn(): Promise<void> {
		const path = retyping;
		const type = retypeTo;
		if (colBusy || !path || !type) return;
		colBusy = true;
		colError = null;
		try {
			const res = await retypeColumn({ table, path, type });
			if (res.ok) {
				retyping = null;
				retypeTo = '';
				await onchanged();
			} else colFail(res.status, res.detail);
		} finally {
			colBusy = false;
		}
	}
</script>

<section>
	<h2>Schema</h2>
	{#if fields.length === 0}
		<p class="mut">Schema unavailable for this table.</p>
	{:else}
		<table>
			<thead><tr><th>field</th><th>type</th><th>nullable</th><th></th></tr></thead>
			<tbody>
				{#each fields as f (f.name)}
					<tr>
						<td class="mono">{f.name}</td>
						<td class="mono">{typeName(f.type)}</td>
						<td class="mono">{f.nullable ? 'yes' : 'no'}</td>
						<td class="actions">
							{#if renaming === f.name}
								<input
									class="mono rn"
									bind:value={renameTo}
									placeholder="new name"
									aria-label="rename {f.name} to"
									onkeydown={(e) => e.key === 'Enter' && runRenameColumn()}
								/>
								<button
									class="btn ghost"
									disabled={colBusy || !renameTo.trim()}
									onclick={runRenameColumn}>save</button
								>
								<button class="btn ghost" onclick={() => (renaming = null)}>×</button>
							{:else if retyping === f.name}
								<!-- #74 tail — re-type via alter_columns; the target is a scalar Arrow type the
							     catalog's _SCALAR_ARROW map accepts. An impossible cast 400s and surfaces. -->
								<div class="retype">
									<Select
										bind:value={retypeTo}
										ariaLabel="re-type {f.name} to"
										placeholder="new type"
										options={RETYPE_TYPES.map((t) => ({ value: t, label: t }))}
									/>
									<button
										class="btn ghost"
										disabled={colBusy || !retypeTo}
										onclick={runRetypeColumn}>save</button
									>
									<button class="btn ghost" onclick={() => (retyping = null)}>×</button>
								</div>
							{:else}
								<button
									class="chip-x"
									title="rename column"
									aria-label="rename {f.name}"
									disabled={colBusy}
									onclick={() => {
										renaming = f.name;
										renameTo = '';
									}}>✎</button
								>
								<button
									class="chip-x"
									title="re-type column"
									aria-label="re-type {f.name}"
									disabled={colBusy}
									onclick={() => {
										retyping = f.name;
										retypeTo = '';
									}}>⇄</button
								>
								<!-- Never `allowed`, and never natively `disabled`: GatedAction needs the control to
								     keep receiving hover and focus, or the reason it carries can never be read
								     (see gated-action.svelte). It dims and blocks the click itself. -->
								<GatedAction allowed={false} action="Backfill column" reason={BACKFILL_UNAVAILABLE}>
									<button class="chip-x" aria-label="backfill {f.name}">⤵</button>
								</GatedAction>
								<button
									class="chip-x"
									title="drop column"
									aria-label="drop {f.name}"
									disabled={colBusy}
									onclick={() => runDropColumn(f.name)}>×</button
								>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
	<!-- #74 add a SQL-expression column (e.g. price * 2, cast(null as int)). Writer-gated. -->
	<form
		class="row addcol"
		onsubmit={(e) => {
			e.preventDefault();
			runAddColumn();
		}}
	>
		<input
			class="mono"
			bind:value={addColName}
			placeholder="new column"
			aria-label="New column name"
		/>
		<input
			class="mono"
			bind:value={addColExpr}
			placeholder="SQL expression (e.g. cast(null as int))"
			aria-label="Column SQL expression"
		/>
		<button
			class="btn"
			type="submit"
			disabled={colBusy || !addColName.trim() || !addColExpr.trim()}
		>
			Add column
		</button>
	</form>
	{#if colError}<p class="error">{colError}</p>{/if}
</section>

<style>
	section {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	table {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	th {
		text-align: left;
		color: var(--faint);
		font-weight: 500;
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid var(--line);
	}
	td {
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	td.actions {
		text-align: right;
		white-space: nowrap;
	}
	.rn {
		width: 110px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 2px 6px;
	}
	.addcol {
		margin-top: 10px;
	}
	.chip-x {
		margin-left: 5px;
		background: none;
		border: none;
		padding: 0;
		color: var(--faint);
		font: inherit;
		cursor: pointer;
	}
	.chip-x:hover {
		color: var(--fail);
	}
	.chip-x:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.row {
		display: flex;
		gap: 8px;
	}
</style>
