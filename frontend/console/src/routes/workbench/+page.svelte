<script lang="ts" module>
	// Kicked off at MODULE EVALUATION so the ~100 KB dock chunk downloads in parallel with the page's mount.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE LAKEHOUSE WORKBENCH — a dock beside the catalog's areas rather than inside one, because it
	 * composes them.
	 *
	 * The panels are the console's own components. Events reads the lineage event feed through
	 * context; Tables is the very component `/catalog/tables` renders, own reads and all. Nothing here
	 * is a mirror.
	 *
	 * Layout + named views ride the per-subject user-state machinery (`dock-layout`,
	 * `dock-layout-library`) — the library is app-agnostic.
	 */
	import { onMount, type Component } from 'svelte';
	import { Database, List } from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews, ViewSidebar } from '@rask/dockview/views';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { page } from '$app/state';
	import { createBench, setBench } from '#lib/dock/bench.svelte.ts';
	import { userStateFetcher } from '#lib/dock/user-state-fetch.ts';
	import EventsPanel from '#lib/dock/panels/EventsPanel.svelte';
	import TablesPanel from '#lib/dock/panels/TablesPanel.svelte';

	const store = createBench();
	setBench(store);
	onMount(() => void store.poll());

	const panels: PanelRegistry = {
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
	};

	// The subject key names this dock inside the one per-subject document.
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
		dockApi.addPanel({ id: 'tables', component: 'tables', title: 'Tables' });
		dockApi.addPanel({
			id: 'events',
			component: 'events',
			title: 'Events',
			position: { referencePanel: 'tables', direction: 'right' },
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
			console.warn('[workbench] saved view failed to apply — restoring', e);
			try {
				api.fromJSON(previous, { reuseExistingPanels: true });
			} catch {
				// The previous layout came from this dock a moment ago; the seed is the only ground left.
			}
		}
	}
</script>

<svelte:head><title>Workbench · rask</title></svelte:head>

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
