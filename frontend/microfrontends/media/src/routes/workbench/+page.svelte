<script lang="ts" module>
	// Kicked off at MODULE EVALUATION — while the route chunk is still hydrating — rather than
	// inside onMount. The ~100 KB dock chunk then downloads in PARALLEL with hydration instead of
	// after it, which is the other half of removing the blink. One promise per module, so a second
	// visit in the same session reuses it instead of re-importing.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * The media workbench — Atlas, Treemap and topic results in one arrangeable dock.
	 *
	 * Why this zone gets one: the corpus views are already a workbench pretending to be five routes.
	 * `/tree` nests a treemap and a results pane inside a fixed `ResizableSplit`, `/atlas` and `/graph`
	 * are full-page siblings you can only look at one at a time, and comparing an embedding map against
	 * the topic hierarchy — the actual research task — means alt-tabbing between two URLs. A dock lets
	 * the user put them side by side, and remembers how they did it.
	 *
	 * The 1175-line `/` search page is deliberately NOT folded in here. It carries the search store,
	 * the player and the transcript in one component, and breaking it up is its own change with its own
	 * browser drive — the same reasoning `OPEN-WORK.md` C1 records for `TableDetail.svelte`.
	 */
	import { onMount, type Component } from 'svelte';
	import type { PanelRegistry } from '@rask/dockview';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import { FolderTree, ListTree, Map } from '@lucide/svelte';
	import { MediaWorkbench } from '$lib/dock/workbench.svelte';
	import { setMediaWorkbench } from '$lib/dock/context';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { dockLayout } from '$lib/dock/layout.remote';
	import { base } from '$app/paths';
	import AtlasPanel from '$lib/dock/panels/AtlasPanel.svelte';
	import TreemapPanel from '$lib/dock/panels/TreemapPanel.svelte';
	import TopicsPanel from '$lib/dock/panels/TopicsPanel.svelte';

	const workbench = new MediaWorkbench();

	/** Keys are the contract between a SAVED layout and this code — see MissingPanelRenderer. The
	 *  VALUES carry label/icon/keywords for the `+` picker; `label` is the default tab TITLE at add
	 *  time, not a live binding on layouts already saved. */
	const panels: PanelRegistry = {
		atlas: {
			component: AtlasPanel,
			label: 'Atlas',
			icon: Map,
			keywords: ['embedding', 'umap', 'scatter', 'projection'],
		},
		treemap: {
			component: TreemapPanel,
			label: 'Treemap',
			icon: FolderTree,
			keywords: ['topics', 'hierarchy', 'tree', 'clusters'],
		},
		topics: {
			component: TopicsPanel,
			label: 'Topic results',
			icon: ListTree,
			keywords: ['hits', 'documents', 'search', 'results'],
		},
	};

	// Ordinary Svelte context. `<Dock>` forwards this component's whole context tree into every panel
	// mount (getAllContexts), so panels reach it with the matching getter and no key is involved.
	setMediaWorkbench(workbench);
	// The workbench id namespaces this zone's layout inside the ONE per-subject dock-layout document,
	// so it cannot collide with the lakehouse's.
	const WORKBENCH_ID = 'media';
	/** ONE implementation for the whole estate — see `@rask/api/dock-layout`. */
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout`,
	});
	/** Read on the SERVER, so the saved layout is the FIRST paint rather than a replacement. */
	const initial = $derived(await dockLayout(WORKBENCH_ID));

	/** Deferred: dockview is ~100 KB gzipped in-bundle and this is one route out of seven in the zone. */
	let Dock = $state<Component | null>(null);
	onMount(async () => {
		const mod = await dockModule;
		Dock = mod.Dock as unknown as Component;
	});

	/** Seed the default arrangement — ONLY when the user had no saved layout of their own. */
	function seed(api: DockviewApi, restored: boolean): void {
		if (restored) return;
		api.addPanel({ id: 'treemap', component: 'treemap', title: 'Treemap' });
		api.addPanel({
			id: 'atlas',
			component: 'atlas',
			title: 'Atlas',
			position: { referencePanel: 'treemap', direction: 'right' },
		});
		api.addPanel({
			id: 'topics',
			component: 'topics',
			title: 'Topic results',
			position: { referencePanel: 'atlas', direction: 'below' },
		});
	}
</script>

<svelte:head><title>Workbench · media</title></svelte:head>

<div class="workbench">
	{#if Dock}
		<Dock
			{panels}
			{initial}
			store={layoutStore}
			onready={seed}
			chromeOptions={{ popoutUrl: `${base}/popout.html` }}
		/>
	{/if}
</div>

<style>
	/* A dock measures its container and lays the grid out in pixels, so it needs a DEFINITE height.
	   `min-height: 0` is load-bearing — without it the flex item refuses to shrink below its content
	   and the dock grows without bound. */
	.workbench {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
		width: 100%;
	}
</style>
