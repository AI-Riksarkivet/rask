<script lang="ts" module>
	// Kicked off at MODULE EVALUATION so the ~100 KB dock chunk downloads in parallel with hydration.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE SEARCH WORKBENCH — a dock INSIDE the media zone.
	 *
	 * The panels are this zone's own components (`search-bar`, `hit-list`, `AtlasMap`,
	 * `player-pane`) sharing ONE `Bench` store through context, which is the whole argument for a
	 * per-zone dock: a result picked in the list is the hit the atlas highlights and the player
	 * loads, with no transport between them. The global `/workbench` zone can compose panels ACROSS
	 * zones, but pays bundles, proxies and mirroring for it and still cannot share a store. Cutting
	 * and re-cutting ONE corpus is the workflow that actually wanted a dock.
	 *
	 * Layout + named views ride the SAME per-subject machinery (`dock-layout`,
	 * `dock-layout-library` on this zone's user-state proxy) — the library is zone-agnostic.
	 */
	import { onMount, type Component } from 'svelte';
	import { ListTree, Map, Play } from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews, ViewSidebar } from '@rask/dockview/views';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { Bench, setBench } from '$lib/dock/bench.svelte';
	import AtlasPanel from '$lib/dock/panels/AtlasPanel.svelte';
	import PlayerPanel from '$lib/dock/panels/PlayerPanel.svelte';
	import SearchPanel from '$lib/dock/panels/SearchPanel.svelte';

	const bench = new Bench();
	setBench(bench);

	const panels: PanelRegistry = {
		results: {
			component: SearchPanel,
			label: 'Search results',
			icon: ListTree,
			keywords: ['search', 'hits', 'results', 'query', 'fts', 'vector'],
		},
		atlas: {
			component: AtlasPanel,
			label: 'Atlas',
			icon: Map,
			keywords: ['embedding', 'map', 'atlas', 'cluster', 'lasso'],
		},
		player: {
			component: PlayerPanel,
			label: 'Player',
			icon: Play,
			keywords: ['media', 'transcript', 'audio', 'video', 'play'],
		},
	};

	const WORKBENCH_ID = 'media-search';
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout`,
		isAuthEnabled: () => page.data.authEnabled === true,
	});
	const viewsStore = makeDockViewsStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout-library`,
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
		dockApi.addPanel({ id: 'results', component: 'results', title: 'Results' });
		dockApi.addPanel({
			id: 'atlas',
			component: 'atlas',
			title: 'Atlas',
			position: { referencePanel: 'results', direction: 'right' },
		});
		dockApi.addPanel({
			id: 'player',
			component: 'player',
			title: 'Player',
			position: { referencePanel: 'atlas', direction: 'below' },
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
			console.warn('[media workbench] saved view failed to apply — restoring', e);
			try {
				api.fromJSON(previous, { reuseExistingPanels: true });
			} catch {
				// The previous layout came from this dock a moment ago; the seed is the only ground left.
			}
		}
	}
</script>

<svelte:head><title>Search workbench — RASK</title></svelte:head>

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
