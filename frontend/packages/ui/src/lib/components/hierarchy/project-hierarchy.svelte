<script lang="ts" module>
	import type { HierarchyTier } from './hierarchy-node.svelte';

	/** Mirrors the zones' status-aware `ApiResult` (`@rask/api`'s client.ts): 0 = fetch-level
	 *  failure/offline. Declared STRUCTURALLY rather than imported, so the library owns no API
	 *  client (the GrantsPanel precedent) and a zone can hand its catalog remotes straight in. */
	export type HierarchyResult<T> =
		| { ok: true; data: T }
		| { ok: false; status: number; detail: string };

	/** The project→warehouse rung, as both zones' catalog reads already spell it. Structural: a
	 *  richer record (with `status`, `serving`, …) passes without an adapter. */
	export type HierarchyWarehouse = { id: string; bucket: string };

	/** The warehouse→namespace rung. The catalog's `GET /v1/warehouses/{id}/namespaces` shape. */
	export type FetchHierarchyNamespaces = (
		warehouseId: string,
	) => Promise<HierarchyResult<{ namespaces: string[] }>>;

	/** The namespace→table rung. The catalog's `GET /v1/namespace/{id}/table/list` shape. */
	export type FetchHierarchyTables = (
		namespaceId: string,
	) => Promise<HierarchyResult<{ tables: string[] }>>;

	/** One rung, as `hrefFor` receives it. `id` is the OBJECT id and differs from `label` where the
	 *  two diverge — a table's label is its bare name (`features`) while its id is the qualified
	 *  `<namespace>$<table>` the catalog routes on. Passing the label would build a URL to nothing. */
	export type HierarchyRung = { tier: HierarchyTier; id: string; label: string };

	/** Where a rung navigates, or `null` to leave it a plain box.
	 *
	 *  The CALLER builds the URL, on the same reasoning that keeps the two rung reads as props: this
	 *  component is mounted by home's `/projects/<id>` and the lakehouse's Overview, whose route
	 *  namespaces differ, and zones may not import each other. A library that hardcoded
	 *  `/lakehouse/catalog/...` would be asserting one zone's routing from inside a shared package. */
	export type HierarchyHref = (rung: HierarchyRung) => string | null;

	export type ProjectHierarchyProps = {
		/** The project at the top of the tree — the root rung's label. */
		project: string;
		/** Its warehouses. An empty list is a real state, not a loading one. */
		warehouses: HierarchyWarehouse[];
		fetchNamespaces: FetchHierarchyNamespaces;
		fetchTables: FetchHierarchyTables;
		/** Tables drawn per namespace before the view says "+k more" (default 8). */
		maxTables?: number;
		/** Makes the graph a MAP rather than a picture. Omit and every rung stays a plain box, which is
		 *  what it was until 2026-08-16: the one view drawing project › warehouse › namespace › table
		 *  could not take you to any of them, and sat beside a separate text drill-down with nothing
		 *  linking the two. */
		hrefFor?: HierarchyHref;
	};
</script>

<script lang="ts">
	import { untrack } from 'svelte';
	// The VERTICAL hierarchy view (#104, the 2026-08-05 ruling): the relationship the estate is BUILT
	// on — project › warehouse › namespace › table — drawn as a graph, immediately, not behind a tab.
	// Mechanism from @rask/flow (StaticFlow: a non-interactive viewer).
	//
	// It lives in @rask/ui because TWO zones render it — the home zone's `/projects/<id>` and the
	// lakehouse's Overview (#109) — and zones may not import each other. TRANSPORT-AGNOSTIC on the
	// GrantsPanel precedent (rask-styling rule 6): the library never owns an API client, so the two
	// rung reads arrive as async props and each zone supplies its OWN remote functions.
	//
	// The one thing a consumer must know: this is the design system's single @rask/flow-backed
	// component, so a zone mounting it needs `@rask/flow` + `@xyflow/svelte` declared AND their two
	// stylesheets imported `layer(base)` in its app.css (vendor sheet first, then @rask/flow's token
	// mapping) — otherwise the canvas paints in the vendor's light-mode hex.
	//
	// The tree is fetched rung by rung and drawn HONESTLY: each namespace draws at most `maxTables`
	// tables and then says "+k more" as its own node — a capped view that looks complete is how a
	// hierarchy lies. A rung whose read failed renders an "unreadable" node rather than disappearing
	// (a binding you cannot read is a namespace you cannot see — the estate's own listing rule).
	import type { Edge, Node, NodeTypes } from '@xyflow/svelte';
	import { StaticFlow } from '@rask/flow';
	import HierarchyNode, { type HierarchyData } from './hierarchy-node.svelte';

	let {
		project,
		warehouses,
		fetchNamespaces,
		fetchTables,
		maxTables = 8,
		hrefFor,
	}: ProjectHierarchyProps = $props();

	const TIER_H = 130;
	const SLOT_W = 210;

	// Registered ONCE at module-ish scope (the five rules): keys match node.type exactly.
	const nodeTypes: NodeTypes = { rung: HierarchyNode };

	type Tree = {
		// warehouse id -> namespaces (null = the read failed)
		namespaces: Map<string, string[] | null>;
		// namespace id -> { tables, more } (null = the read failed)
		tables: Map<string, { tables: string[]; more: number } | null>;
	};
	let tree = $state<Tree | null>(null);

	// The refetch key is the SUBJECT, not the props' identity. A caller whose `warehouses` array is
	// re-derived on every render (the lakehouse's is, off a registry query) would otherwise re-walk
	// the whole tree on each pass; keying on the ids means a re-derive that changed nothing refetches
	// nothing, and it is also what `load()` compares against to stay latest-wins across navigation.
	const key = $derived(`${project}\u0000${warehouses.map((w) => w.id).join('\u0001')}`);

	$effect(() => {
		void key;
		tree = null;
		// untrack: `load()` reads `warehouses` + both fetchers in its synchronous prologue, so
		// without this they are TRACKED and the key above buys nothing — a caller passing a
		// re-derived array or an inline arrow prop (the lakehouse Overview does both) refetched
		// every warehouse and every namespace rung on an identity-only change. The key IS the
		// subject; these reads must not add dependencies.
		untrack(() => void load());
	});

	async function load(): Promise<void> {
		const current = key;
		const namespaces = new Map<string, string[] | null>();
		await Promise.all(
			warehouses.map(async (w) => {
				const res = await fetchNamespaces(w.id);
				namespaces.set(w.id, res.ok ? res.data.namespaces : null);
			}),
		);
		const allNs = [...new Set([...namespaces.values()].flatMap((n) => n ?? []))];
		const tables = new Map<string, { tables: string[]; more: number } | null>();
		await Promise.all(
			allNs.map(async (ns) => {
				const res = await fetchTables(ns);
				tables.set(
					ns,
					res.ok
						? {
								tables: res.data.tables.slice(0, maxTables),
								more: Math.max(0, res.data.tables.length - maxTables),
							}
						: null,
				);
			}),
		);
		if (key !== current) return; // latest-wins across navigation
		tree = { namespaces, tables };
	}

	/** A tidy vertical tree: leaves take sequential slots, every parent centres over its children. */
	function build(t: Tree): { nodes: Node[]; edges: Edge[]; width: number } {
		const nodes: Node[] = [];
		const edges: Edge[] = [];
		let slot = 0;
		const TIERS = ['project', 'warehouse', 'namespace', 'table'] as const;
		// `more` and `err` nodes get NO href on purpose — neither names an object you can open, and a
		// link to nowhere is worse than a plain box.
		const link = (tier: HierarchyTier, id: string, label: string): Partial<HierarchyData> => {
			const href = hrefFor?.({ tier, id, label });
			return href ? { href } : {};
		};
		const node = (
			id: string,
			label: string,
			tier: 0 | 1 | 2 | 3,
			x: number,
			extra: Partial<HierarchyData> = {},
		): number => {
			nodes.push({
				id,
				type: 'rung',
				position: { x, y: tier * TIER_H },
				data: { label, tier: TIERS[tier], ...extra } satisfies HierarchyData,
				// StaticFlow is a VIEWER: connections/dragging are off, these just silence handles.
				draggable: false,
				connectable: false,
			});
			return x;
		};
		const centre = (xs: number[]): number =>
			xs.length ? (Math.min(...xs) + Math.max(...xs)) / 2 : slot++ * SLOT_W;

		const warehouseXs: number[] = [];
		for (const w of warehouses) {
			const wid = `w:${w.id}`;
			const namespaces = t.namespaces.get(w.id);
			const nsXs: number[] = [];
			for (const ns of namespaces ?? []) {
				const nid = `n:${ns}`;
				const entry = t.tables.get(ns);
				const tableXs: number[] = [];
				if (entry === null) {
					tableXs.push(node(`${nid}:err`, 'tables unreadable', 3, slot++ * SLOT_W, { err: true }));
					edges.push({ id: `${nid}-err`, source: nid, target: `${nid}:err` });
				} else if (entry) {
					for (const tb of entry.tables) {
						// The table's OBJECT id is qualified (`<ns>$<table>`); its label is the bare name.
						tableXs.push(
							node(`t:${ns}$${tb}`, tb, 3, slot++ * SLOT_W, link('table', `${ns}$${tb}`, tb)),
						);
						edges.push({ id: `${nid}-${tb}`, source: nid, target: `t:${ns}$${tb}` });
					}
					if (entry.more > 0) {
						tableXs.push(
							node(`${nid}:more`, `+${entry.more} more`, 3, slot++ * SLOT_W, { more: true }),
						);
						edges.push({ id: `${nid}-more`, source: nid, target: `${nid}:more` });
					}
				}
				nsXs.push(node(nid, ns, 2, centre(tableXs), link('namespace', ns, ns)));
				edges.push({ id: `${wid}-${ns}`, source: wid, target: nid });
			}
			if (namespaces === null) {
				const eid = `${wid}:err`;
				nsXs.push(node(eid, 'namespaces unreadable', 2, slot++ * SLOT_W, { err: true }));
				edges.push({ id: `${wid}-err`, source: wid, target: eid });
			}
			warehouseXs.push(
				node(wid, w.id, 1, centre(nsXs), {
					detail: `s3://${w.bucket}`,
					...link('warehouse', w.id, w.id),
				}),
			);
			edges.push({ id: `p-${w.id}`, source: 'p', target: wid });
		}
		node('p', project, 0, centre(warehouseXs), link('project', project, project));
		return { nodes, edges, width: Math.max(slot, 1) * SLOT_W };
	}

	const graph = $derived(tree ? build(tree) : null);
</script>

{#if warehouses.length === 0}
	<p class="text-muted-foreground text-sm">
		No warehouses yet — the hierarchy starts when the first one is created.
	</p>
{:else if graph === null}
	<p class="text-muted-foreground text-sm">Loading the hierarchy…</p>
{:else}
	<div data-slot="project-hierarchy" class="h-105 w-full min-w-0 overflow-hidden rounded-lg border">
		<StaticFlow nodes={graph.nodes} edges={graph.edges} {nodeTypes} />
	</div>
{/if}
