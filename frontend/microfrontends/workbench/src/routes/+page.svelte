<script lang="ts" module>
	// Kicked off at MODULE EVALUATION — while the route chunk is still hydrating — so the ~100 KB dock
	// chunk downloads in PARALLEL with hydration rather than after it.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE global workbench — one dock that holds panels from any zone, and a sidebar of saved views.
	 *
	 * It is its own ZONE, not a route inside lakehouse or compute, and that is the whole point: a Svelte
	 * component only renders if it is in the bundle, and a zone cannot import from another zone. The
	 * panels therefore live in `@rask/panels` and are imported here — ordinary build-time sharing, the
	 * same mechanism `@rask/ui` already uses in all seven zones. No module federation, no iframes.
	 *
	 * Being a zone is also being a PROCESS, which is what makes the media plane's module-level API base
	 * safe here: it is set once, to this zone's base, exactly as media and annotator each set their own.
	 */
	import { onMount, type Component } from 'svelte';
	import { Activity, List, Workflow } from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews } from '@rask/dockview';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { createLineageClient } from '@rask/api/lineage';
	import { createBffClient } from '@rask/api/client';
	import {
		EventsPanel,
		GraphPanel,
		LineageState,
		RunsPanel,
		setLineageState,
	} from '@rask/panels/lineage';
	import { base } from '$app/paths';
	import ViewSidebar from '$lib/ViewSidebar.svelte';

	/** This zone's own BFF binding — the ONE zone-shaped value the panels' transport needs. */
	const bff = createBffClient(base);
	/** ONE LineageState, polled once, shared by graph/runs/events. The thing an iframe design could
	 *  not have: `<Dock>` hands its context tree to every panel, so they can never be a poll apart. */
	const lineage = new LineageState(createLineageClient(bff));
	setLineageState(lineage);

	/** Keys are the contract between a SAVED layout and this code — see MissingPanelRenderer. */
	const panels: PanelRegistry = {
		graph: {
			component: GraphPanel,
			label: 'Lineage graph',
			icon: Workflow,
			keywords: ['dag', 'lineage', 'provenance', 'medallion', 'openlineage'],
		},
		runs: {
			component: RunsPanel,
			label: 'Runs',
			icon: Activity,
			keywords: ['jobs', 'executions', 'failures', 'state', 'lineage'],
		},
		events: {
			component: EventsPanel,
			label: 'Events',
			icon: List,
			keywords: ['feed', 'log', 'stream', 'emissions', 'lineage'],
		},
	};

	/** ONE dock now, so ONE workbench id — the three per-zone ids are retired with their routes. */
	const WORKBENCH_ID = 'global';
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout`,
	});
	const viewsStore = makeDockViewsStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout-library`,
	});

	let api = $state<DockviewApi | null>(null);
	/** The saved-views model: the list, WHICH ONE IS ACTIVE, and whether the dock has diverged. */
	const views = new DockViews<SerializedDockview>(viewsStore, () => api?.toJSON() ?? null);

	let Dock = $state<Component | null>(null);
	onMount(async () => {
		const mod = await dockModule;
		Dock = mod.Dock as unknown as Component;
		void views.refresh();
		void lineage.poll();
	});

	/** Seed the default arrangement — ONLY when the user had no saved layout of their own. */
	function ready(dockApi: DockviewApi, restored: boolean): void {
		api = dockApi;
		// Every layout change bumps the divergence marker. Not debounced, unlike autosave: this writes
		// no bytes, and a "modified" dot that appears a second after the drag reads as a bug.
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

	/** Applying a view replaces the arrangement; the dock stays fully rearrangeable afterwards. */
	function applyView(id: string): void {
		const read = views.select(id);
		if (read.status === 'ok' && api !== null) api.fromJSON(read.layout as SerializedDockview);
	}
</script>

<svelte:head><title>Workbench</title></svelte:head>

<div class="wrap">
	<ViewSidebar {views} onselect={applyView} />
	<div class="dock">
		{#if Dock}
			<Dock
				{panels}
				store={layoutStore}
				onready={ready}
				chromeOptions={{ popoutUrl: `${base}/popout.html` }}
			/>
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
	/* A dock measures its container and lays the grid out in pixels, so it needs a DEFINITE height.
	   `min-height: 0` is load-bearing — without it the flex item refuses to shrink below its content
	   and the dock grows without bound. */
	.dock {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
	}
</style>
