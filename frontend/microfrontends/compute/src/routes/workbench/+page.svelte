<script lang="ts" module>
	// Kicked off at MODULE EVALUATION — while the route chunk is still hydrating — rather than inside
	// onMount. The ~100 KB dock chunk then downloads in PARALLEL with hydration instead of after it,
	// which is half of removing the blink; the SSR-read layout below is the other half.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * The compute workbench — jobs, cluster capacity and live actors in one arrangeable dock.
	 *
	 * Operating Ray means watching three things at once: what was submitted, what capacity is left, and
	 * which actors are alive. Today those are three routes, so answering "is this job starved or stuck?"
	 * costs two navigations. Docked, it is one glance — and the arrangement follows the operator to
	 * whatever machine they next open.
	 *
	 * Panels take no context here, unlike the other zones': every read is already a remote `query()`,
	 * which is per-request server state a panel can call for itself.
	 */
	import { onMount, type Component } from 'svelte';
	import { Boxes, ListTree, Server } from '@lucide/svelte';
	import type { PanelRegistry } from '@rask/dockview';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { dockLayout } from '$lib/dock/layout.remote';
	import { base } from '$app/paths';
	import JobsPanel from '$lib/dock/panels/JobsPanel.svelte';
	import ClusterPanel from '$lib/dock/panels/ClusterPanel.svelte';
	import ActorsPanel from '$lib/dock/panels/ActorsPanel.svelte';

	/** Keys are the contract between a SAVED layout and this code — see MissingPanelRenderer. The
	 *  VALUES carry label/icon/keywords for the `+` picker; `label` is the default tab TITLE at add
	 *  time, not a live binding on layouts already saved. Annotated deliberately: without the
	 *  annotation an entry missing its required `label` type-checks clean at the `<Dock>` boundary. */
	const panels: PanelRegistry = {
		jobs: {
			component: JobsPanel,
			label: 'Jobs',
			icon: ListTree,
			keywords: ['ray', 'submitted', 'queue', 'lifecycle', 'raysubmit'],
		},
		cluster: {
			component: ClusterPanel,
			label: 'Cluster',
			icon: Server,
			keywords: ['nodes', 'capacity', 'resources', 'gpu', 'load'],
		},
		actors: {
			component: ActorsPanel,
			label: 'Actors',
			icon: Boxes,
			keywords: ['live', 'workers', 'replicas', 'serve'],
		},
	};

	/** Namespaces this zone's layout inside the ONE per-subject document, so zones cannot collide. */
	const WORKBENCH_ID = 'compute';
	/** ONE implementation for the whole estate — see `@rask/api/dock-layout`. */
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout`,
	});
	/** Read on the SERVER, so the saved layout is the FIRST paint rather than a replacement. */
	const initial = $derived(await dockLayout(WORKBENCH_ID));

	let Dock = $state<Component | null>(null);
	onMount(async () => {
		const mod = await dockModule;
		Dock = mod.Dock as unknown as Component;
	});

	/** Seed the default arrangement — ONLY when the user had no saved layout of their own. */
	function seed(api: DockviewApi, restored: boolean): void {
		if (restored) return;
		api.addPanel({ id: 'jobs', component: 'jobs', title: 'Jobs' });
		api.addPanel({
			id: 'cluster',
			component: 'cluster',
			title: 'Cluster',
			position: { referencePanel: 'jobs', direction: 'right' },
		});
		api.addPanel({
			id: 'actors',
			component: 'actors',
			title: 'Actors',
			position: { referencePanel: 'cluster', direction: 'below' },
		});
	}
</script>

<svelte:head><title>Workbench · compute</title></svelte:head>

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
