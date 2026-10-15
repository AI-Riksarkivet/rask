<script lang="ts">
	// #73 index management — list + drop. The catalog builds no index: `create_index` and
	// `create_scalar_index` answer 406 naming the bring-your-own path, so an index is built (or
	// rebuilt) by the caller's own compute against the table, and appears here once committed. Drop
	// stays a catalog door, gated at the writer tier (can_write_data).
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation.
	import { dropTableIndex } from '../remote/catalog.remote';

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

	let ixBusy = $state(false);
	let ixError = $state<string | null>(null);

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
	<p class="mut">
		Indexes are built on your own compute against this table; the catalog does not build them.
	</p>
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
</style>
