<script lang="ts">
	// The VERTICAL hierarchy view (#104, the 2026-08-05 ruling): the project page shows the
	// relationship the estate is BUILT on — project › warehouse › namespace › table — as a graph,
	// immediately, not behind a tab. Mechanism from @rask/flow (StaticFlow: a non-interactive
	// viewer); the DOMAIN graph stays in this zone, per the estate's panels rule.
	//
	// The tree is fetched rung by rung through this zone's own remotes and drawn HONESTLY: each
	// namespace draws at most MAX_TABLES tables and then says "+k more" as its own node — a capped
	// view that looks complete is how a hierarchy lies. A rung whose read failed renders an
	// "unreadable" node rather than disappearing (a binding you cannot read is a namespace you
	// cannot see — the estate's own listing rule).
	import type { Edge, Node } from '@xyflow/svelte';
	import { StaticFlow } from '@rask/flow';
	import type { ProjectSummary } from '$lib/catalog';
	import { namespaceTables, warehouseNamespaces } from '$lib/remote/warehouses.remote';

	let { project, warehouses }: { project: string; warehouses: ProjectSummary['warehouses'] } =
		$props();

	const MAX_TABLES = 8;
	const TIER_H = 120;
	const SLOT_W = 190;

	type Tree = {
		// warehouse id -> namespaces (null = the read failed)
		namespaces: Map<string, string[] | null>;
		// namespace id -> { tables, more } (null = the read failed)
		tables: Map<string, { tables: string[]; more: number } | null>;
	};
	let tree = $state<Tree | null>(null);

	$effect(() => {
		void project;
		tree = null;
		load();
	});

	async function load(): Promise<void> {
		const current = project;
		const namespaces = new Map<string, string[] | null>();
		await Promise.all(
			warehouses.map(async (w) => {
				const res = await warehouseNamespaces(w.id);
				namespaces.set(w.id, res.ok ? res.data.namespaces : null);
			}),
		);
		const allNs = [...new Set([...namespaces.values()].flatMap((n) => n ?? []))];
		const tables = new Map<string, { tables: string[]; more: number } | null>();
		await Promise.all(
			allNs.map(async (ns) => {
				const res = await namespaceTables(ns);
				tables.set(
					ns,
					res.ok
						? {
								tables: res.data.tables.slice(0, MAX_TABLES),
								more: Math.max(0, res.data.tables.length - MAX_TABLES),
							}
						: null,
				);
			}),
		);
		if (project !== current) return; // latest-wins across navigation
		tree = { namespaces, tables };
	}

	/** A tidy vertical tree: leaves take sequential slots, every parent centres over its children. */
	function build(t: Tree): { nodes: Node[]; edges: Edge[]; width: number } {
		const nodes: Node[] = [];
		const edges: Edge[] = [];
		let slot = 0;
		const node = (id: string, label: string, tier: number, x: number, cls = ''): number => {
			nodes.push({
				id,
				position: { x, y: tier * TIER_H },
				data: { label },
				class: cls,
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
					tableXs.push(node(`${nid}:err`, 'tables unreadable', 3, slot++ * SLOT_W, 'err'));
					edges.push({ id: `${nid}-err`, source: nid, target: `${nid}:err` });
				} else if (entry) {
					for (const tb of entry.tables) {
						tableXs.push(node(`t:${ns}$${tb}`, tb, 3, slot++ * SLOT_W));
						edges.push({ id: `${nid}-${tb}`, source: nid, target: `t:${ns}$${tb}` });
					}
					if (entry.more > 0) {
						tableXs.push(node(`${nid}:more`, `+${entry.more} more`, 3, slot++ * SLOT_W, 'more'));
						edges.push({ id: `${nid}-more`, source: nid, target: `${nid}:more` });
					}
				}
				nsXs.push(node(nid, ns, 2, centre(tableXs)));
				edges.push({ id: `${wid}-${ns}`, source: wid, target: nid });
			}
			if (namespaces === null) {
				const eid = `${wid}:err`;
				nsXs.push(node(eid, 'namespaces unreadable', 2, slot++ * SLOT_W, 'err'));
				edges.push({ id: `${wid}-err`, source: wid, target: eid });
			}
			warehouseXs.push(node(wid, `${w.id} · ${w.bucket}`, 1, centre(nsXs)));
			edges.push({ id: `p-${w.id}`, source: 'p', target: wid });
		}
		node('p', project, 0, centre(warehouseXs), 'root');
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
	<div class="hierarchy h-105 w-full min-w-0 overflow-hidden rounded-lg border">
		<StaticFlow nodes={graph.nodes} edges={graph.edges} />
	</div>
{/if}

<style>
	/* Tier styling rides node classes; everything else is the shared @rask/flow theme. */
	.hierarchy :global(.svelte-flow__node.root) {
		font-weight: 600;
	}
	.hierarchy :global(.svelte-flow__node.err) {
		color: var(--destructive);
		border-color: var(--destructive);
	}
	.hierarchy :global(.svelte-flow__node.more) {
		color: var(--muted-foreground);
		border-style: dashed;
	}
</style>
