<script lang="ts">
	// #85 row ops — update/delete rows by SQL predicate (writer-gated can_write_data). Update carries
	// the wire's [[column, expression], …] SET list; delete is destructive → two-click confirm (the
	// restore/GC idiom). The affected-row count comes from the update response; the delete wire has
	// no count (only the new version), stated honestly.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — a predicate (or an armed delete confirm) typed on
	// table A cannot fire against B.
	import { deleteRows, updateRows } from '../remote/catalog.remote';

	let { table, onchanged }: { table: string; onchanged: () => Promise<void> } = $props();

	let rowPredicate = $state('');
	let rowSets = $state<{ column: string; expression: string }[]>([{ column: '', expression: '' }]);
	let rowBusy = $state(false);
	let rowMsg = $state<{ ok: boolean; text: string } | null>(null);
	let rowDeleteConfirm = $state(false);
	const rowSetPairs = $derived(
		rowSets
			.map((s) => [s.column.trim(), s.expression.trim()] as [string, string])
			.filter(([column, expression]) => column && expression),
	);
	// A HALF-filled pair (column without expression, or vice versa) must BLOCK the update — silently
	// dropping it would apply a different write than the one on screen (audit 2026-07-23). Fully-empty
	// extra pairs stay ignorable (the "+ add SET pair" affordance always leaves one around).
	const rowSetPartial = $derived(
		rowSets.some((s) => (s.column.trim() === '') !== (s.expression.trim() === '')),
	);

	function rowFail(status: number, detail: string): void {
		if (status === 401) rowMsg = { ok: false, text: 'Sign in to change rows.' };
		else if (status === 403)
			rowMsg = { ok: false, text: 'Denied: row changes need writer access (can_write_data).' };
		else rowMsg = { ok: false, text: detail };
	}

	async function runUpdateRows(): Promise<void> {
		const sets = rowSetPairs;
		if (rowBusy || sets.length === 0 || rowSetPartial) return;
		rowBusy = true;
		rowMsg = null;
		try {
			const res = await updateRows({
				table,
				predicate: rowPredicate.trim() || null,
				updates: sets,
			});
			if (res.ok) {
				rowMsg = {
					ok: true,
					text: `Updated ${res.data.updated_rows} row${res.data.updated_rows === 1 ? '' : 's'} → v${res.data.version}.`,
				};
				await onchanged(); // the update bumped the version — refresh stats + versions
			} else rowFail(res.status, res.detail);
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			rowMsg = { ok: false, text: `update response drifted from the contract: ${String(err)}` };
		} finally {
			rowBusy = false;
		}
	}

	async function runDeleteRows(): Promise<void> {
		const predicate = rowPredicate.trim();
		if (rowBusy || !predicate) return;
		rowBusy = true;
		rowMsg = null;
		try {
			const res = await deleteRows({ table, predicate });
			if (res.ok) {
				// the delete wire carries no row count — surface the new version honestly instead
				rowMsg = {
					ok: true,
					text: `Deleted rows matching the predicate${res.data.version != null ? ` → v${res.data.version}` : ''}.`,
				};
				await onchanged();
			} else rowFail(res.status, res.detail);
		} catch (err) {
			rowMsg = { ok: false, text: `delete response drifted from the contract: ${String(err)}` };
		} finally {
			rowBusy = false;
			rowDeleteConfirm = false; // ALWAYS disarm — success or failure, the confirm must not stay armed
		}
	}
</script>

<section>
	<h2>Update / delete rows</h2>
	<p class="mut">
		SQL predicate over the table's columns (e.g. <span class="mono">id &gt; 3</span>). Update applies
		the SET pairs to matching rows (empty predicate = all rows); delete removes them (predicate
		required). Both are writer-gated.
	</p>
	<input
		class="mono pred"
		bind:value={rowPredicate}
		placeholder="predicate (e.g. id > 3)"
		aria-label="Row predicate"
	/>
	{#each rowSets as s, i (i)}
		<div class="row setpair">
			<input class="mono" bind:value={s.column} placeholder="column" aria-label="SET column {i + 1}" />
			<input
				class="mono"
				bind:value={s.expression}
				placeholder="SQL expression (e.g. price * 2)"
				aria-label="SET expression {i + 1}"
			/>
		</div>
	{/each}
	<button
		class="btn ghost"
		onclick={() => (rowSets = [...rowSets, { column: '', expression: '' }])}
	>
		+ add SET pair
	</button>
	{#if rowSetPartial}
		<p class="mut" role="status">
			A SET pair is only half-filled — complete (or clear) both its column and expression; partial
			pairs are never silently dropped.
		</p>
	{/if}
	<div class="ins-row">
		<button
			class="btn"
			disabled={rowBusy || rowSetPairs.length === 0 || rowSetPartial}
			onclick={runUpdateRows}
		>
			{rowBusy ? '…' : 'Update rows'}
		</button>
		{#if rowDeleteConfirm}
			<button class="btn danger" disabled={rowBusy || !rowPredicate.trim()} onclick={runDeleteRows}>
				confirm delete
			</button>
			<button class="btn ghost" onclick={() => (rowDeleteConfirm = false)}>cancel</button>
		{:else}
			<button
				class="btn danger"
				disabled={rowBusy || !rowPredicate.trim()}
				onclick={() => (rowDeleteConfirm = true)}
			>
				Delete rows
			</button>
		{/if}
		{#if rowMsg}<span class="ins-msg" class:okmsg={rowMsg.ok} class:error={!rowMsg.ok}
		  >{rowMsg.text}</span
		>{/if}
	</div>
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
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
	}
	.pred {
		width: 100%;
		margin-bottom: 6px;
	}
	.setpair {
		margin-bottom: 6px;
	}
	.row {
		display: flex;
		gap: 8px;
	}
	.ins-row {
		display: flex;
		align-items: center;
		gap: 10px;
		margin-top: 6px;
	}
	.ins-msg {
		font-size: 12px;
	}
	.okmsg {
		color: var(--ok);
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
	.btn.danger {
		color: var(--fail);
	}
</style>
