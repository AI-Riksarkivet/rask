<script lang="ts" module>
	// Kicked off at MODULE EVALUATION so the ~100 KB dock chunk downloads in parallel with hydration.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE LINEAGE WORKBENCH — a dock INSIDE the lakehouse zone.
	 *
	 * The panels are this zone's own components over ONE `LineageState`, polled once and shared
	 * through context: the medallion DAG, the run board and the event feed can never be a poll
	 * apart, and the graph is the SAME `LineageGraph` the /lineage page renders — not a mirror.
	 *
	 * Layout + named views ride the SAME per-subject machinery (`dock-layout`,
	 * `dock-layout-library` on this zone's user-state proxy) — the library is zone-agnostic.
	 */
	import { onMount, type Component } from 'svelte';
	import { Activity, List, Network } from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews, ViewSidebar } from '@rask/dockview/views';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { createBench, setBench } from '$lib/dock/bench.svelte';
	import { userStateFetcher } from '$lib/dock/user-state-fetch';
	import EventsPanel from '$lib/dock/panels/EventsPanel.svelte';
	import GraphPanel from '$lib/dock/panels/GraphPanel.svelte';
	import RunsPanel from '$lib/dock/panels/RunsPanel.svelte';

	const store = createBench();
	setBench(store);
	onMount(() => void store.poll());

	const panels: PanelRegistry = {
		graph: {
			component: GraphPanel,
			label: 'Lineage graph',
			icon: Network,
			keywords: ['dag', 'lineage', 'provenance', 'medallion', 'openlineage'],
		},
		runs: {
			component: RunsPanel,
			label: 'Runs',
			icon: Activity,
			keywords: ['runs', 'executions', 'failures', 'state', 'lineage'],
		},
		events: {
			component: EventsPanel,
			label: 'Events',
			icon: List,
			keywords: ['events', 'feed', 'log', 'stream', 'emissions'],
		},
	};

	const WORKBENCH_ID = 'lakehouse-lineage';
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: 'dock-layout',
		fetcher: userStateFetcher('dock-layout'),
		isAuthEnabled: () => page.data.authEnabled === true,
	});
	const viewsStore = makeDockViewsStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: 'dock-layout-library',
		fetcher: userStateFetcher('dock-layout-library'),
		isAuthEnabled: () => page.data.authEnabled === true,
	});

	let api = $state<DockviewApi | null>(null);
	const views = new DockViews<SerializedDockview>(viewsStore, () => api?.toJSON() ?? null);

	let Dock = $state<Component | null>(null);
	onMount(async () => {
		const mod = await dockModule;
		Dock = mod.Dock as unknown as Component;
		void views.refresh();
	});

	function ready(dockApi: DockviewApi, restored: boolean): void {
		api = dockApi;
		dockApi.onDidLayoutChange(() => views.touch());
		if (restored) return;
		dockApi.addPanel({ id: 'graph', component: 'graph', title: 'Lineage graph' });
		dockApi.addPanel({
			id: 'runs',
			component: 'runs',
			title: 'Runs',
			position: { referencePanel: 'graph', direction: 'right' },
		});
		dockApi.addPanel({
			id: 'events',
			component: 'events',
			title: 'Events',
			position: { referencePanel: 'runs', direction: 'below' },
		});
	}

	/** Apply first, activate only on success — a failed view restores the previous arrangement. */
	function applyView(id: string): void {
		if (api === null) return;
		const read = views.select(id);
		if (read.status !== 'ok') return;
		const previous = api.toJSON();
		try {
			api.fromJSON(read.layout as SerializedDockview, { reuseExistingPanels: true });
			views.activate(id);
		} catch (e) {
			console.warn('[lineage workbench] saved view failed to apply — restoring', e);
			try {
				api.fromJSON(previous, { reuseExistingPanels: true });
			} catch {
				// The previous layout came from this dock a moment ago; the seed is the only ground left.
			}
		}
	}
</script>

<svelte:head><title>Lineage workbench — RASK</title></svelte:head>

<div class="wrap">
	<ViewSidebar {views} onselect={applyView} />
	<div class="dock">
		{#if Dock}
			<Dock {panels} store={layoutStore} onready={ready} chrome={{ popout: false }} />
		{/if}
	</div>
</div>

<style>
	.wrap {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		width: 100%;
	}
	/* A dock lays its grid out in pixels, so it needs a DEFINITE height; `min-height: 0` is
	   load-bearing or the flex item refuses to shrink below its content. The stacking context keeps
	   dockview's positioned panel overlays from painting over the shared navbar's dropdowns — the
	   bug the global workbench hit, fixed here by construction. */
	.dock {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		position: relative;
		z-index: 0;
		isolation: isolate;
	}
</style>
