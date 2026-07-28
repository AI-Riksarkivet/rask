<script lang="ts" module>
	// Kicked off at MODULE EVALUATION — while the route chunk is still hydrating — rather than
	// inside onMount. The ~100 KB dock chunk then downloads in PARALLEL with hydration instead of
	// after it, which is the other half of removing the blink. One promise per module, so a second
	// visit in the same session reuses it instead of re-importing.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * The lineage workbench — the estate's first dock.
	 *
	 * One `LineageState` is polled here and shared by every panel through the dock's context map, so
	 * the graph, the run board and the event feed can never be a poll apart. Layout is per-subject on
	 * the Dapr state store via the catalog (`$lib/dock/layout-store`), so a user's arrangement follows
	 * them to another machine instead of living in that browser's localStorage.
	 */
	import { onMount, type Component } from 'svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import { LineageState } from '$lib/lineage/store.svelte';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';
	import { setLineageState } from '$lib/dock/lineage-context';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { dockLayout } from '$lib/dock/layout.remote';
	import { base } from '$app/paths';
	import GraphPanel from '$lib/dock/panels/GraphPanel.svelte';
	import RunsPanel from '$lib/dock/panels/RunsPanel.svelte';
	import EventsPanel from '$lib/dock/panels/EventsPanel.svelte';

	const store = new LineageState();
	liveRead(lineageTick, () => store.poll());

	/** Keys are the stable contract between a SAVED layout and this code — renaming one orphans every
	 *  stored layout that used it, which `MissingPanelRenderer` then shows rather than crashing on. */
	const panels = { graph: GraphPanel, runs: RunsPanel, events: EventsPanel };

	// Ordinary Svelte context. `<Dock>` forwards this component's whole context tree into every panel
	// mount (getAllContexts), so panels reach it with the matching getter and no key is involved.
	setLineageState(store);
	const WORKBENCH_ID = 'lineage';
	/** ONE implementation for the whole estate — see `@rask/api/dock-layout`. */
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout`,
	});
	/** Read on the SERVER, so the saved layout is the FIRST paint rather than a replacement. */
	const initial = $derived(await dockLayout(WORKBENCH_ID));

	/**
	 * Deferred on purpose. dockview is ~152 KB gzipped standalone (~96 KB measured in this bundle,
	 * where it shares a gzip stream and the unused splitview/gridview exports tree-shake), and this is ONE route out
	 * of roughly twenty-five in the zone. Imported statically it would land in the entry graph every
	 * lakehouse page pays for, against 110 KB of remaining static headroom (`budget.json`); imported
	 * here it lands in the deferred half, which only a visitor to this route loads.
	 */
	let Dock = $state<Component | null>(null);
	onMount(async () => {
		const mod = await dockModule;
		Dock = mod.Dock as unknown as Component;
	});

	/** Seed the default arrangement — ONLY when the user had no saved layout of their own. */
	function seed(api: DockviewApi, restored: boolean): void {
		if (restored) return;
		api.addPanel({ id: 'graph', component: 'graph', title: 'Graph' });
		api.addPanel({
			id: 'runs',
			component: 'runs',
			title: 'Runs',
			position: { referencePanel: 'graph', direction: 'right' },
		});
		api.addPanel({
			id: 'events',
			component: 'events',
			title: 'Events',
			position: { referencePanel: 'runs', direction: 'below' },
		});
	}
</script>

<svelte:head><title>Lineage workbench · lance</title></svelte:head>

<div class="workbench">
	{#if Dock}
		<Dock {panels} {initial} store={layoutStore} onready={seed} />
	{/if}
</div>

<style>
	/* A dock needs a DEFINITE height, not an auto one: it measures its container and lays the grid out
	   in pixels. The lineage area's layout wrapper is already a sized flex column (`.zone-scroll` in
	   the zone's +layout.svelte), so filling it is enough — but `min-height: 0` is load-bearing, or the
	   flex item refuses to shrink below its content and the dock grows without bound. */
	/* Sized identically whether or not the dock has mounted, so there is no reflow when it arrives —
	   and deliberately EMPTY rather than holding a "Loading…" line, which would paint, be measured,
	   and then be replaced. An empty box of the right size is the one thing that cannot flash. */
	.workbench {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		width: 100%;
	}
</style>
