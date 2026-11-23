<script lang="ts">
	// #81 one hop of the authorization graph around a namespace, as a compact LIST card: the namespace
	// page has no inline grant form to prefill, so a list states the same edges as the table's canvas
	// more cheaply. Owner-gated by the catalog (can_delete). The page holds the read, issued each time the
	// card is opened, so a tab change does not read it again.
	import { Network, ShieldAlert } from '@lucide/svelte';
	import { Subject } from '#lib/access/identity/index.ts';
	import type { fetchAccessGraph } from './remote/access-objects.api';

	let { ns, read }: { ns: string; read: ReturnType<typeof fetchAccessGraph> } = $props();

	const answer = $derived(read.current);
	const graph = $derived(answer?.ok ? answer.data : null);
	const status = $derived.by((): 'loading' | 'ok' | 'denied' | 'offline' => {
		if (answer === undefined) return read.error === undefined ? 'loading' : 'offline';
		if (answer.ok) return 'ok';
		return answer.status === 401 || answer.status === 403 ? 'denied' : 'offline';
	});

	// Inbound edges are grants (a subject holds a rung ON the namespace); outbound is the container edge
	// (namespace → parent or project). Labels come from the graph's nodes.
	const nodeLabel = (id: string): string => graph?.nodes.find((n) => n.id === id)?.label ?? id;
	const grantEdges = $derived(
		graph === null ? [] : graph.edges.filter((e) => e.target === graph.object),
	);
	const containerEdges = $derived(
		graph === null ? [] : graph.edges.filter((e) => e.source === graph.object),
	);
</script>

<div class="graphcard">
	<header class="graphhead">
		<Network size={14} />
		<h3>Authorization graph</h3>
		<span class="mut mono">who holds which rung · one hop around {ns}</span>
	</header>
	{#if status === 'denied'}
		<p class="mut">
			<ShieldAlert size={13} /> Owner access is required to view the graph.
		</p>
	{:else if status === 'offline'}
		<p class="mut">Graph unavailable right now — reopen to retry.</p>
	{:else if status === 'loading'}
		<p class="mut">Loading the authorization graph…</p>
	{:else if graph}
		{#if grantEdges.length === 0}
			<p class="mut">No direct grants on this namespace.</p>
		{:else}
			<ul class="edges">
				{#each grantEdges as e (`${e.source}:${e.relation}`)}
					<li class="mono">
						<Subject value={nodeLabel(e.source)} class="text-foreground" />
						<span class="chip">{e.relation}</span>
						<span class="mut">on {nodeLabel(e.target)}</span>
					</li>
				{/each}
			</ul>
		{/if}
		{#if containerEdges.length > 0}
			<ul class="edges">
				{#each containerEdges as e (`${e.relation}:${e.target}`)}
					<li class="mono">
						<span class="mut">{e.relation} →</span>
						<span class="text-foreground">{nodeLabel(e.target)}</span>
					</li>
				{/each}
			</ul>
		{/if}
	{/if}
</div>

<style>
	.graphcard {
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--muted);
		padding: 10px 12px;
	}
	.graphhead {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 6px;
		color: var(--muted-foreground);
	}
	.graphhead h3 {
		font-size: 13px;
		margin: 0;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 4px 0;
	}
	.edges {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 12px;
	}
	.edges li {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 3px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
	}
	.edges li:last-child {
		border-bottom: none;
	}
	.chip {
		background: var(--muted);
		border: 1px solid color-mix(in srgb, var(--success) 45%, var(--border));
		border-radius: var(--radius-sm);
		color: var(--muted-foreground);
		font-size: 12px;
		padding: 1px 8px;
	}
</style>
