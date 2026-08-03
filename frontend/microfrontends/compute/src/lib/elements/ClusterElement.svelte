<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/** `<rask-compute-cluster>` — Ray nodes + capacity, served by the compute zone to the global
	 *  workbench. Light DOM; scoped styles over tokens (see JobsElement for the contract). */
	import { rayCluster, type RayClusterPayload } from '@rask/api';
	import { RayPoll } from './ray-poll.svelte';

	let { pollms = 5000 }: { pollms?: number } = $props();
	const poll = new RayPoll<RayClusterPayload>((f) => rayCluster(f));
	$effect(() => poll.start(pollms));

	const nodes = $derived(poll.data?.nodes ?? []);

	function pct(used: number | null, total: number | null): string {
		if (used === null || total === null || total === 0) return '—';
		return `${Math.round((used / total) * 100)}%`;
	}
</script>

<div class="ce-panel">
	<p class="meta">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="error">Ray unreachable: {poll.error}</p>
	{:else if nodes.length === 0}
		<p class="empty">No nodes reported.</p>
	{:else}
		<table>
			<thead>
				<tr><th>Node</th><th>Role</th><th>State</th><th>CPU</th><th>Mem</th><th>GPUs</th></tr>
			</thead>
			<tbody>
				{#each nodes as node (node.node_id)}
					<tr>
						<td class="mono">{node.hostname ?? node.node_ip ?? '—'}</td>
						<td>{node.is_head ? 'head' : 'worker'}</td>
						<td><span class="pill" data-ok={node.alive}>{node.alive ? 'alive' : 'dead'}</span></td>
						<td>{node.host_cpu_percent === null ? '—' : `${Math.round(node.host_cpu_percent)}%`}</td>
						<td>{pct(node.host_mem_used, node.host_mem_total)}</td>
						<td>{node.gpus.length}</td>
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
	.mono {
		font-family: var(--font-mono, monospace);
	}
	.pill {
		border: 1px solid var(--color-border);
		border-radius: 0.25rem;
		padding: 0 0.375rem;
		font-size: 0.6875rem;
	}
	.pill[data-ok='true'] {
		color: var(--color-primary);
	}
	.pill[data-ok='false'] {
		color: var(--color-destructive);
	}
</style>
