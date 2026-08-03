<script lang="ts" module>
	// Kicked off at MODULE EVALUATION — while the route chunk is still hydrating — so the ~100 KB dock
	// chunk downloads in PARALLEL with hydration rather than after it.
	const dockModule = import('@rask/dockview');
</script>

<script lang="ts">
	/**
	 * THE global workbench — one dock composing panels OWNED AND SERVED BY OTHER ZONES, plus a
	 * sidebar of saved views (open_workbench.md).
	 *
	 * This zone holds ZERO panel code. A foreign panel is a custom element (`rask-<zone>-<panel>`)
	 * built by its owning zone's own Vite into that zone's deployment; `ForeignPanel` loads it by
	 * URL and mounts the tag. The zone keeps its code, its deploys, its fetchers — redeploying
	 * compute updates the jobs panel HERE with no rebuild of this zone. Communication crosses the
	 * boundary exactly one way each: properties down, `RASK_SELECT` CustomEvents up, valibot-gated
	 * at the relay below. Native panels (the selection log) may use context; foreign ones never.
	 */
	import { onMount, type Component } from 'svelte';
	import { on } from 'svelte/events';
	import {
		Activity,
		Boxes,
		Database,
		List,
		ListTree,
		ScrollText,
		Server,
		ServerCog,
		Workflow,
	} from '@lucide/svelte';
	import type { DockviewApi, SerializedDockview } from 'dockview';
	import type { PanelRegistry } from '@rask/dockview';
	import { DockViews } from '@rask/dockview/views';
	import { parseSelectDetail, RASK_SELECT } from '@rask/dockview/contract';
	import { makeDockLayoutStore } from '@rask/api/dock-layout';
	import { makeDockViewsStore } from '@rask/api/dock-views';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import ForeignPanel from '$lib/ForeignPanel.svelte';
	import SelectionLog from '$lib/SelectionLog.svelte';
	import { bench } from '$lib/bench.svelte';
	import { Selections, setSelections } from '$lib/selections.svelte';

	const selections = new Selections();
	setSelections(selections);

	/** Keys are the contract between a SAVED layout and this code — see MissingPanelRenderer. */
	const panels: PanelRegistry = {
		'selection-log': {
			component: SelectionLog,
			group: 'Workbench',
			label: 'Selection log',
			icon: List,
			keywords: ['relay', 'events', 'clicked', 'history'],
		},
		'compute-jobs': {
			component: ForeignPanel,
			group: 'Compute',
			label: 'Ray jobs (compute)',
			icon: ListTree,
			keywords: ['ray', 'compute', 'submitted', 'queue', 'raysubmit', 'foreign'],
		},
		'compute-cluster': {
			component: ForeignPanel,
			group: 'Compute',
			label: 'Cluster nodes (compute)',
			icon: Server,
			keywords: ['ray', 'compute', 'nodes', 'capacity', 'gpu', 'foreign'],
		},
		'compute-actors': {
			component: ForeignPanel,
			group: 'Compute',
			label: 'Ray actors (compute)',
			icon: Boxes,
			keywords: ['ray', 'compute', 'actors', 'workers', 'replicas', 'foreign'],
		},
		'compute-serve': {
			component: ForeignPanel,
			group: 'Compute',
			label: 'Serve apps (compute)',
			icon: ServerCog,
			keywords: ['ray', 'compute', 'serve', 'deployments', 'endpoints', 'foreign'],
		},
		'lakehouse-runs': {
			component: ForeignPanel,
			group: 'Lakehouse',
			label: 'Lineage runs (lakehouse)',
			icon: Activity,
			keywords: ['lineage', 'runs', 'pipelines', 'openlineage', 'foreign'],
		},
		'lakehouse-events': {
			component: ForeignPanel,
			group: 'Lakehouse',
			label: 'Lineage events (lakehouse)',
			icon: List,
			keywords: ['lineage', 'events', 'feed', 'openlineage', 'foreign'],
		},
		'lakehouse-graph': {
			component: ForeignPanel,
			group: 'Lakehouse',
			label: 'Lineage graph (lakehouse)',
			icon: Workflow,
			keywords: ['lineage', 'graph', 'dag', 'medallion', 'provenance', 'foreign'],
		},
		'lakehouse-datasets': {
			component: ForeignPanel,
			group: 'Lakehouse',
			label: 'Datasets (lakehouse)',
			icon: Database,
			keywords: ['catalog', 'datasets', 'tables', 'governed', 'foreign'],
		},
		'lakehouse-audit': {
			component: ForeignPanel,
			group: 'Lakehouse',
			label: 'Audit trail (lakehouse)',
			icon: ScrollText,
			keywords: ['governance', 'audit', 'compliance', 'trail', 'foreign'],
		},
	};

	/** ONE dock, ONE workbench id — VERSIONED. The build-time workbench (reversed, see
	 *  docs/architecture/global-workbench.md) saved layouts under 'global' whose panel keys
	 *  (graph/runs/events/jobs/cluster/actors) no longer exist here; restoring one filled the dock
	 *  with MissingPanelRenderer placeholders. Bumping the id orphans that state wholesale — the
	 *  predicted one-time reset — instead of half-restoring it as tombstones. */
	const WORKBENCH_ID = 'global-ce-v1';
	const layoutStore = makeDockLayoutStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout`,
		// With auth ON a 401 is an expired session, not the auth-off dev case — the stores change
		// their 401 semantics on this flag (review finding: silent this-browser-only downgrades).
		isAuthEnabled: () => page.data.authEnabled === true,
	});
	const viewsStore = makeDockViewsStore<SerializedDockview>({
		workbenchId: WORKBENCH_ID,
		endpoint: `${base}/capi/v1/user-state/dock-layout-library`,
		isAuthEnabled: () => page.data.authEnabled === true,
	});

	let api = $state<DockviewApi | null>(null);
	/** The saved-views model: the list, WHICH ONE IS ACTIVE, and whether the dock has diverged. */
	const views = new DockViews<SerializedDockview>(viewsStore, () => api?.toJSON() ?? null);

	let Dock = $state<Component | null>(null);
	let dockHost = $state<HTMLElement | null>(null);
	// Hand the views model to the layout's shell rail (see $lib/bench.svelte.ts). Its own onMount:
	// an async mount callback cannot return a cleanup, and the dock import below is async.
	onMount(() => {
		bench.views = views;
		bench.apply = applyView;
		return () => {
			bench.views = null;
			bench.apply = null;
		};
	});

	onMount(async () => {
		const mod = await dockModule;
		Dock = mod.Dock as unknown as Component;
		void views.refresh();
	});

	/** The relay: ONE delegated listener on the dock wrapper. Foreign panels' events bubble through
	 *  light DOM to here; a detail that fails the schema is logged and DROPPED — a drifted zone
	 *  degrades, it never corrupts a sibling. Attached via $effect because `rask:select` is not a
	 *  DOM attribute event. */
	$effect(() => {
		const host = dockHost;
		if (host === null) return;
		const relay = (event: Event): void => {
			const detail = parseSelectDetail((event as CustomEvent).detail);
			if (detail === null) {
				console.warn('[workbench] dropped malformed RASK_SELECT', (event as CustomEvent).detail);
				return;
			}
			selections.push(detail);
		};
		return on(host, RASK_SELECT, relay);
	});

	/** Seed the default arrangement — ONLY when the user had no saved layout of their own. */
	function ready(dockApi: DockviewApi, restored: boolean): void {
		api = dockApi;
		dockApi.onDidLayoutChange(() => views.touch());
		if (restored) return;
		dockApi.addPanel({ id: 'compute-jobs', component: 'compute-jobs', title: 'Ray jobs' });
		dockApi.addPanel({
			id: 'lakehouse-runs',
			component: 'lakehouse-runs',
			title: 'Lineage runs',
			position: { referencePanel: 'compute-jobs', direction: 'right' },
		});
		dockApi.addPanel({
			id: 'lakehouse-events',
			component: 'lakehouse-events',
			title: 'Lineage events',
			position: { referencePanel: 'lakehouse-runs', direction: 'below' },
		});
		dockApi.addPanel({
			id: 'selection-log',
			component: 'selection-log',
			title: 'Selections',
			position: { referencePanel: 'compute-jobs', direction: 'below' },
		});
	}

	/** Applying a view replaces the arrangement; the dock stays fully rearrangeable afterwards.
	 *  `reuseExistingPanels` is LOAD-BEARING for foreign panels: without it a view switch recreates
	 *  every panel, and a recreated custom element loses its state and refetches. The ordering is
	 *  the review's: apply FIRST, `activate` only on success (no checked-but-never-applied view),
	 *  and a bad saved view restores the previous arrangement instead of leaving an empty dock for
	 *  autosave to persist. */
	function applyView(id: string): void {
		if (api === null) return;
		const read = views.select(id);
		if (read.status !== 'ok') return;
		const previous = api.toJSON();
		try {
			api.fromJSON(read.layout as SerializedDockview, { reuseExistingPanels: true });
			views.activate(id);
		} catch (e) {
			console.warn('[workbench] saved view failed to apply — restoring the draft', e);
			try {
				api.fromJSON(previous, { reuseExistingPanels: true });
			} catch {
				// The previous layout came from this same dock a moment ago; if even it refuses,
				// the seeded default is the only safe ground left.
			}
		}
	}
</script>

<svelte:head><title>Workbench</title></svelte:head>

<div class="wrap">
	<div class="dock" bind:this={dockHost}>
		{#if Dock}
			<!-- popout: EXPLICITLY off (not just "no popoutUrl configured"): popout re-parents a panel
			     into a second document, where a custom element re-runs its full lifecycle against a
			     window that has neither the token stylesheet nor the element scripts — every foreign
			     panel here is one. The design doc requires this gate (review finding). -->
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
	/* A dock measures its container and lays the grid out in pixels, so it needs a DEFINITE height.
	   `min-height: 0` is load-bearing — without it the flex item refuses to shrink below its content
	   and the dock grows without bound. */
	.dock {
		display: flex;
		flex: 1 1 0;
		min-height: 0;
	}
</style>
