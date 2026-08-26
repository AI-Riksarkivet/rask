<script lang="ts" module>
	// Kicked off at MODULE EVALUATION so the ~100 KB dock chunk downloads in parallel with hydration.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE LAKEHOUSE WORKBENCH — a dock INSIDE the lakehouse zone, at ZONE level.
	 *
	 * It briefly lived at `/lakehouse/lineage/workbench`, which buried a zone-level surface inside one
	 * AREA and made it read as a lineage feature. A dock composes the whole zone, so it sits beside
	 * the areas rather than inside one.
	 *
	 * The panels are this zone's own components. Graph/Runs/Events share ONE `LineageState`, polled
	 * once through context, so the DAG, the run board and the event feed can never be a poll apart;
	 * Tables and Storage are the very components `/catalog/tables` and `/catalog/storage` render, own
	 * reads and all. Nothing here is a mirror — that is what an in-zone dock buys.
	 *
	 * Layout + named views ride the SAME per-subject machinery (`dock-layout`,
	 * `dock-layout-library` on this zone's user-state proxy) — the library is zone-agnostic.
	 */
	import { onMount, type Component } from 'svelte';
	import { Activity, Database, HardDrive, List, Network } from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews, ViewSidebar } from '@rask/dockview/views';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { page } from '$app/state';
	import { createBench, setBench } from '$lib/dock/bench.svelte';
	import { userStateFetcher } from '$lib/dock/user-state-fetch';
	import EventsPanel from '$lib/dock/panels/EventsPanel.svelte';
	import GraphPanel from '$lib/dock/panels/GraphPanel.svelte';
	import RunsPanel from '$lib/dock/panels/RunsPanel.svelte';
	import StoragePanel from '$lib/dock/panels/StoragePanel.svelte';
	import TablesPanel from '$lib/dock/panels/TablesPanel.svelte';

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
		tables: {
			component: TablesPanel,
			label: 'Tables',
			icon: Database,
			keywords: ['tables', 'catalog', 'registry', 'namespace', 'schema'],
		},
		storage: {
			component: StoragePanel,
			label: 'Storage',
			icon: HardDrive,
			keywords: ['storage', 'objects', 's3', 'bucket', 'browser', 'blobs'],
		},
	};

	// The subject key is the ZONE's dock, not an area's — it kept the old `lakehouse-lineage` name
	// while the route lived under /lineage. Renaming it abandons any layout saved under the old key,
	// which is the honest trade: the dock it described had three panels and a different route.
	const WORKBENCH_ID = 'lakehouse';
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
		dockApi.addPanel({
			id: 'tables',
			component: 'tables',
			title: 'Tables',
			position: { referencePanel: 'graph', direction: 'below' },
		});
		// Storage joins TABLES as a tab rather than taking a sixth region: five visible panes on a
		// laptop leaves every one of them too small to read, and "what tables exist" / "what objects
		// back them" are two answers to one question — which is exactly what a tab group is for.
		dockApi.addPanel({
			id: 'storage',
			component: 'storage',
			title: 'Storage',
			position: { referencePanel: 'tables', direction: 'within' },
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
			console.warn('[lakehouse workbench] saved view failed to apply — restoring', e);
			try {
				api.fromJSON(previous, { reuseExistingPanels: true });
			} catch {
				// The previous layout came from this dock a moment ago; the seed is the only ground left.
			}
		}
	}
</script>

<svelte:head><title>Lakehouse workbench — rask</title></svelte:head>

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
