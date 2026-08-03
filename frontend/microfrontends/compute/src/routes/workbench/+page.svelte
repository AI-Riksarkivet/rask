<script lang="ts" module>
	// Kicked off at MODULE EVALUATION so the ~100 KB dock chunk downloads in parallel with hydration.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE COMPUTE WORKBENCH — a dock INSIDE the compute zone.
	 *
	 * Each panel reads the zone's OWN remote functions (`getRayJobs`, `getRayCluster`, `getActors`)
	 * on the zone's own poll clock — the thing a cross-zone element could never do, because a
	 * remote function's endpoint is per-app.
	 *
	 * Layout + named views ride the SAME per-subject machinery (`dock-layout`,
	 * `dock-layout-library` on this zone's user-state proxy) — the library is zone-agnostic.
	 */
	import { onMount, type Component } from 'svelte';
	import { Boxes, ListTree, Server } from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews, ViewSidebar } from '@rask/dockview/views';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { userStateFetcher } from '$lib/dock/user-state-fetch';
	import ActorsPanel from '$lib/dock/panels/ActorsPanel.svelte';
	import ClusterPanel from '$lib/dock/panels/ClusterPanel.svelte';
	import JobsPanel from '$lib/dock/panels/JobsPanel.svelte';

	const panels: PanelRegistry = {
		jobs: {
			component: JobsPanel,
			label: 'Ray jobs',
			icon: ListTree,
			keywords: ['ray', 'jobs', 'submitted', 'queue', 'raysubmit'],
		},
		cluster: {
			component: ClusterPanel,
			label: 'Cluster nodes',
			icon: Server,
			keywords: ['ray', 'nodes', 'capacity', 'gpu', 'resources'],
		},
		actors: {
			component: ActorsPanel,
			label: 'Ray actors',
			icon: Boxes,
			keywords: ['ray', 'actors', 'workers', 'replicas', 'serve'],
		},
	};

	const WORKBENCH_ID = 'compute-ray';
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
		dockApi.addPanel({ id: 'jobs', component: 'jobs', title: 'Ray jobs' });
		dockApi.addPanel({
			id: 'cluster',
			component: 'cluster',
			title: 'Cluster',
			position: { referencePanel: 'jobs', direction: 'right' },
		});
		dockApi.addPanel({
			id: 'actors',
			component: 'actors',
			title: 'Actors',
			position: { referencePanel: 'cluster', direction: 'below' },
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
			console.warn('[compute workbench] saved view failed to apply — restoring', e);
			try {
				api.fromJSON(previous, { reuseExistingPanels: true });
			} catch {
				// The previous layout came from this dock a moment ago; the seed is the only ground left.
			}
		}
	}
</script>

<svelte:head><title>Compute workbench — RASK</title></svelte:head>

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
