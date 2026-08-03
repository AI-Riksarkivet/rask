<script lang="ts">
	import { Radio } from '@lucide/svelte';
	import LineageGraph from '$lib/lineage/LineageGraph.svelte';
	import { LineageState } from '$lib/lineage/store.svelte';
	import { createLineageClient } from '@rask/api/lineage';
	import { bff } from '$lib/http';
	import { countUp, stagger } from '@rask/ui/motion';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	// The canvas itself — layout maths, node building, the SvelteFlow chrome — lives in
	// `$lib/lineage/LineageGraph.svelte`, so the dock workbench can mount the same graph as a panel
	// without either copy drifting. This page keeps what is page-shaped: the header and the store.
	// The zone binds the client; the store takes it injected rather than importing $lib/api itself.
	const store = new LineageState(createLineageClient(bff));

	// One tick = /graph + /events + /runs, so a blind 5s timer here re-read the whole estate graph
	// whether or not anything had happened. The lineage cursor gates it: the canvas re-reads when the
	// estate actually changed, and an idle estate costs one 245-byte probe per tick, server-side.
	liveRead(lineageTick, () => store.poll());

	const datasetCount = $derived(store.nodes.length);
	// Honest status line (the old header could claim WAITING while the canvas showed datasets):
	// connecting → first poll still in flight; live → last poll succeeded; offline → last poll
	// failed, the canvas KEEPS the last good state and says so.
	const status = $derived(!store.settled ? 'connecting' : store.online ? 'live' : 'offline');

	/** Last layout-build cost (ms), reported up by the graph — shown in the header corner. */
	let buildMs = $state(0);
</script>

<svelte:head><title>Lineage graph · lance</title></svelte:head>

<div class="app">
	<header {@attach stagger({ each: 0.08 })}>
		<h1>Graph <span class="sub">dataset + job lineage</span></h1>
		<p class="explain">
			The derivation DAG the OpenLineage feed builds — click a node to open its detail page.
		</p>
		<div class="status">
			<Radio size={13} class={store.online ? 'live-ic on' : 'live-ic'} />
			<span class="state-word" class:live={store.online}>{status}</span>
			<span class="sep">·</span>
			<span class="num mono" {@attach countUp(datasetCount)}>0</span> datasets
			{#if store.capped}<span class="capped">first {store.nodes.length} of {store.total}</span>{/if}
			{#if store.lastUpdated}<span class="sep">·</span>
				<span class="ts">{store.lastUpdated}</span>{/if}
			<span class="sep">·</span>
			<span class="perf mono" title="last layout build">{buildMs}ms</span>
		</div>
	</header>

	<LineageGraph {store} bind:buildMs />
</div>

<style>
	.app {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 16px;
		flex-wrap: wrap;
		padding: 11px 18px;
		border-bottom: 1px solid var(--line);
		background: linear-gradient(180deg, var(--panel-2), transparent);
	}
	h1 {
		font-size: 16px;
		margin: 0;
		font-weight: 600;
	}
	.sub {
		color: var(--mut);
		font-size: 12px;
		font-weight: 400;
	}
	.explain {
		margin: 0;
		font-size: 12px;
		color: var(--mut);
		max-width: 720px;
	}
	.status {
		display: flex;
		align-items: center;
		gap: 6px;
		margin-left: auto;
		color: var(--mut);
		font-size: 12px;
		white-space: nowrap;
	}
	.sep {
		color: var(--line-2);
	}
	.num {
		color: var(--ink);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
	}
	.capped {
		color: var(--faint);
		font-size: 11px;
	}
	.state-word {
		text-transform: uppercase;
		letter-spacing: 0.4px;
		font-size: 11px;
		font-weight: 700;
		color: var(--mut);
	}
	.state-word.live {
		color: var(--ok);
	}
	.ts {
		color: var(--faint);
		font-variant-numeric: tabular-nums;
	}
	.perf {
		color: var(--faint);
		font-size: 10px;
	}
	:global(.live-ic) {
		color: var(--mut);
		transition: color 0.3s var(--ease);
	}
	:global(.live-ic.on) {
		color: var(--ok);
	}
</style>
