<script lang="ts">
	// #73 index management — build (scalar / vector) + drop. Vector indexes (IVF/HNSW) are the Lance
	// differentiator neither Iceberg nor Unity has; a rebuild is a create with the same column (Lance
	// replaces). The catalog gates all three at the writer tier (can_write_data).
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — a column/type typed on table A cannot build against B.
	import { Select } from '@rask/ui/select';
	import { createTableIndex, dropTableIndex } from '../remote/catalog.remote';

	interface IndexRow {
		index_name?: string;
		columns?: string[];
		index_type?: string | null;
	}

	let {
		table,
		indexes,
		onchanged,
	}: { table: string; indexes: IndexRow[]; onchanged: () => Promise<void> } = $props();

	const SCALAR_TYPES = ['BTREE', 'BITMAP', 'INVERTED', 'NGRAM'];
	const VECTOR_TYPES = ['IVF_PQ', 'IVF_HNSW_SQ'];
	let ixColumn = $state('');
	let ixType = $state('BTREE');
	let ixDistance = $state('cosine');
	let ixBusy = $state(false);
	let ixError = $state<string | null>(null);
	const ixScalar = $derived(SCALAR_TYPES.includes(ixType));

	async function runCreateIndex(): Promise<void> {
		const column = ixColumn.trim();
		if (ixBusy || !column || !ixType) return;
		ixBusy = true;
		ixError = null;
		try {
			const body = ixScalar
				? { column, index_type: ixType }
				: { column, index_type: ixType, distance_type: ixDistance };
			const res = await createTableIndex({ table, body, scalar: ixScalar });
			if (res.ok) {
				ixColumn = '';
				await onchanged(); // pull the new index into the list (the build bumps the version too)
			} else if (res.status === 401) {
				ixError = 'Sign in to build an index.';
			} else if (res.status === 403) {
				ixError = 'Denied: building an index needs writer access (can_write_data).';
			} else {
				ixError = res.detail;
			}
		} finally {
			ixBusy = false;
		}
	}

	async function runDropIndex(name: string | undefined): Promise<void> {
		if (ixBusy || !name) return;
		ixBusy = true;
		ixError = null;
		try {
			const res = await dropTableIndex({ table, name });
			if (res.ok) {
				await onchanged();
			} else if (res.status === 401) {
				ixError = 'Sign in to drop an index.';
			} else if (res.status === 403) {
				ixError = 'Denied: dropping an index needs writer access (can_write_data).';
			} else {
				ixError = res.detail;
			}
		} finally {
			ixBusy = false;
		}
	}
</script>

<section>
	<h2>Indexes</h2>
	{#if indexes.length === 0}
		<p class="mut">No indexes on this table.</p>
	{:else}
		<div class="refs">
			{#each indexes as ix (ix.index_name)}
				<span class="chip mono"
					>{ix.index_name}<span class="mut">
						· {(ix.columns ?? []).join(', ')}{ix.index_type ? ` · ${ix.index_type}` : ''}</span>
					<button
						class="chip-x"
						title="drop index"
						aria-label="drop index {ix.index_name}"
						disabled={ixBusy}
						onclick={() => runDropIndex(ix.index_name)}>×</button
					></span
				>
			{/each}
		</div>
	{/if}
	<!-- #73 build an index — scalar (BTREE/BITMAP/INVERTED/NGRAM) or vector (IVF/HNSW). Writer-gated. -->
	<form
		class="row"
		onsubmit={(e) => {
	e.preventDefault();
	runCreateIndex();
}}
	>
		<input class="mono" bind:value={ixColumn} placeholder="column" aria-label="Index column" />
		<Select
			bind:value={ixType}
			ariaLabel="Index type"
			options={[
	...SCALAR_TYPES.map((t) => ({ value: t, label: `scalar · ${t}` })),
	...VECTOR_TYPES.map((t) => ({ value: t, label: `vector · ${t}` })),
]}
		/>
		{#if !ixScalar}
			<Select
				bind:value={ixDistance}
				ariaLabel="Distance type"
				options={[
	{ value: 'cosine', label: 'cosine' },
	{ value: 'l2', label: 'l2' },
	{ value: 'dot', label: 'dot' },
]}
			/>
		{/if}
		<button class="btn" type="submit" disabled={ixBusy || !ixColumn.trim()}>
			{ixBusy ? '…' : 'Build index'}
		</button>
	</form>
	{#if ixError}<p class="error">{ixError}</p>{/if}
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
	.refs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin-bottom: 6px;
		font-size: 12px;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 0 7px;
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
	.row {
		display: flex;
		gap: 8px;
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
</style>
