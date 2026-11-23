<script lang="ts">
	// #74 schema evolution — the schema table plus add (name + SQL expr) / rename / re-type / drop
	// columns. All writer-gated (can_write_data); the catalog's refusal is surfaced verbatim.
	//
	// BACKFILL IS STATED, NOT OFFERED. This section used to wire a fourth per-column action to
	// `alter_table_backfill_columns`, which is one of the six ops the pinned native `dir` backend
	// stubs — a 501 for every caller on every table, never a wrong argument or a
	// missing grant. The control stays on screen carrying its reason (#143: show disabled, never
	// hide) and the work itself is done by the "Update / delete rows" section, which SETs an existing
	// column to a SQL expression under an optional predicate. See `./unavailable`.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — an editor opened on table A cannot leak into B, by
	// construction rather than by a hand-written reset list.
	import { GatedAction } from '#lib/access/gated-action/index.ts';
	import { AlertDialog } from '@riksarkivet/ui/alert-dialog';
	import { Button, buttonVariants } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { Select } from '@riksarkivet/ui/select';
	import { RETYPE_TYPES } from '../catalog';
	import { addColumn, dropColumn, renameColumn, retypeColumn } from '../remote/catalog.api';
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

	// A drop asks first, as every destructive action here does: the column's data is gone from the
	// version the drop writes onward.
	let dropping = $state<string | null>(null);
	let dropOpen = $state(false);

	function askDropColumn(name: string): void {
		dropping = name;
		dropOpen = true;
	}

	async function runDropColumn(): Promise<void> {
		const name = dropping;
		if (colBusy || name === null) return;
		colBusy = true;
		colError = null;
		try {
			const res = await dropColumn({ table, name });
			if (res.ok) await onchanged();
			else colFail(res.status, res.detail);
		} finally {
			// Always close and disarm: the dialog's action does not close it by itself, and an open
			// one would keep the drop armed for a second, unconfirmed click.
			colBusy = false;
			dropOpen = false;
			dropping = null;
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
								<Input
									class="inline-flex h-7 w-28 font-mono text-xs"
									bind:value={renameTo}
									placeholder="new name"
									aria-label="rename {f.name} to"
									onkeydown={(e) => e.key === 'Enter' && runRenameColumn()}
								/>
								<Button
									variant="ghost"
									size="xs"
									disabled={colBusy || !renameTo.trim()}
									onclick={runRenameColumn}>save</Button
								>
								<Button variant="ghost" size="icon-xs" onclick={() => (renaming = null)}>×</Button>
							{:else if retyping === f.name}
								<!-- #74 tail — re-type via alter_columns; the target is a scalar type spelled as
							     DescribeTable spells it. An impossible cast 400s and surfaces. -->
								<div class="retype">
									<Select
										bind:value={retypeTo}
										ariaLabel="re-type {f.name} to"
										placeholder="new type"
										options={RETYPE_TYPES.map((t) => ({ value: t, label: t }))}
									/>
									<Button
										variant="ghost"
										size="xs"
										disabled={colBusy || !retypeTo}
										onclick={runRetypeColumn}>save</Button
									>
									<Button variant="ghost" size="icon-xs" onclick={() => (retyping = null)}>×</Button
									>
								</div>
							{:else}
								<Button
									variant="ghost"
									size="icon-xs"
									title="rename column"
									aria-label="rename {f.name}"
									disabled={colBusy}
									onclick={() => {
										renaming = f.name;
										renameTo = '';
									}}>✎</Button
								>
								<Button
									variant="ghost"
									size="icon-xs"
									title="re-type column"
									aria-label="re-type {f.name}"
									disabled={colBusy}
									onclick={() => {
										retyping = f.name;
										retypeTo = '';
									}}>⇄</Button
								>
								<!-- Never `allowed`, and never natively `disabled`: GatedAction needs the control to
								     keep receiving hover and focus, or the reason it carries can never be read
								     (see gated-action.svelte). It dims and blocks the click itself. -->
								<GatedAction allowed={false} action="Backfill column" reason={BACKFILL_UNAVAILABLE}>
									<Button variant="ghost" size="icon-xs" aria-label="backfill {f.name}">⤵</Button>
								</GatedAction>
								<Button
									variant="ghost"
									size="icon-xs"
									title="drop column"
									aria-label="drop {f.name}"
									disabled={colBusy}
									onclick={() => askDropColumn(f.name)}>×</Button
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
		<Input
			class="h-7 w-44 font-mono text-xs"
			bind:value={addColName}
			placeholder="new column"
			aria-label="New column name"
		/>
		<Input
			class="h-7 flex-1 font-mono text-xs"
			bind:value={addColExpr}
			placeholder="SQL expression (e.g. cast(null as int))"
			aria-label="Column SQL expression"
		/>
		<Button size="sm" type="submit" disabled={colBusy || !addColName.trim() || !addColExpr.trim()}>
			Add column
		</Button>
	</form>
	{#if colError}<p class="error">{colError}</p>{/if}
</section>

<AlertDialog.Root bind:open={dropOpen}>
	<AlertDialog.Content>
		<AlertDialog.Title>Drop column {dropping} from {table}</AlertDialog.Title>
		<AlertDialog.Description>
			This writes a new version of <span class="mono">{table}</span> without the column
			<span class="mono">{dropping}</span>: its data is gone from that version on. Earlier versions
			keep it until they are cleaned up. Needs writer access (can_write_data).
		</AlertDialog.Description>
		<div class="dialog-actions">
			<AlertDialog.Cancel disabled={colBusy}>Cancel</AlertDialog.Cancel>
			<AlertDialog.Action
				class={buttonVariants({ variant: 'destructive' })}
				disabled={colBusy}
				onclick={runDropColumn}
			>
				Drop column
			</AlertDialog.Action>
		</div>
	</AlertDialog.Content>
</AlertDialog.Root>

<style>
	section {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
		margin: 0 0 8px;
	}
	table {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	th {
		text-align: left;
		color: var(--muted-foreground);
		font-weight: 500;
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid var(--border);
	}
	td {
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
	}
	td.actions {
		text-align: right;
		white-space: nowrap;
	}
	.addcol {
		margin-top: 10px;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.error {
		color: var(--destructive);
		font-size: 12px;
	}
	.row {
		display: flex;
		gap: 8px;
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
	}
</style>
