<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/** `<rask-lakehouse-datasets>` — the governed datasets, served by the lakehouse zone to the
	 *  global workbench. Reads through the shared client; rows dispatch rask:select. */
	import type { Datasets } from '@rask/api/lineage';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';
	import { client } from './store';

	let { pollms = 30000 }: { pollms?: number } = $props();
	const poll = new ElementPoll<Datasets>(async () => {
		const res = await client.listDatasets();
		if (!res.ok)
			throw new Error(
				`datasets: ${res.status === 401 ? 'session expired or no access' : `HTTP ${res.status}`}`,
			);
		return res.data;
	});
	$effect(() => poll.start(pollms));

	const datasets = $derived(poll.data?.datasets ?? []);

	function select(node: HTMLElement, name: string) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-datasets',
					kind: 'dataset',
					id: name,
					label: name,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="ce-panel">
	<p class="meta">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="error">Catalog unavailable: {poll.error}</p>
	{:else if datasets.length === 0}
		<p class="empty">No datasets yet.</p>
	{:else}
		<table>
			<thead>
				<tr><th>Dataset</th><th>Namespace</th><th>Tags</th></tr>
			</thead>
			<tbody>
				{#each datasets as ds (ds.name)}
					<tr onclick={(e) => select(e.currentTarget, ds.name)}>
						<td class="mono">{ds.name}</td>
						<td>{ds.namespace ?? '—'}</td>
						<td class="dim">{(ds.tags ?? []).join(', ') || '—'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{/if}
</div>

<style>
	.ce-panel {
		display: block;
		height: 100%;
		overflow: auto;
		padding: 0.75rem;
		color: var(--color-foreground);
		font-size: 0.8125rem;
	}
	.meta {
		margin: 0 0 0.5rem;
		color: var(--color-muted-foreground);
		font-size: 0.6875rem;
	}
	.error {
		color: var(--color-destructive);
	}
	.empty {
		color: var(--color-muted-foreground);
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	th {
		text-align: left;
		font-weight: 500;
		color: var(--color-muted-foreground);
		border-bottom: 1px solid var(--color-border);
		padding: 0.25rem 0.5rem;
	}
	td {
		border-bottom: 1px solid var(--color-border);
		padding: 0.375rem 0.5rem;
	}
	tbody tr {
		cursor: pointer;
	}
	tbody tr:hover {
		background: var(--color-muted);
	}
	.mono {
		font-family: var(--font-mono, monospace);
	}
	.dim {
		color: var(--color-muted-foreground);
	}
</style>
