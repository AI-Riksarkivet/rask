<script lang="ts" module>
	// #81 Authorization graph — an interactive relationship explorer for a table's OpenFGA grants, the
	// visual upgrade of the #68 who-can-do-what form. One hop: the focus object, every subject directly
	// granted a rung (edges subject→object labelled owner/writer/reader), and the parent/project
	// container edge. Click a subject to prefill the grant form; grant/revoke re-fetches so the graph
	// reflects the change. SvelteFlow (reusing the LineageExplorer pattern) + the design system's Select + GSAP.
	import AccessNode, { type AccessNodeType } from '#lib/data/AccessNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { access: AccessNode };
</script>

<script lang="ts">
	import { GrantForm } from '#lib/access/grant-form/index.ts';
	import { enter } from '@riksarkivet/ui/motion';
	import { Network, ShieldAlert } from '@lucide/svelte';
	import type { Edge } from '@xyflow/svelte';
	import type { AccessGraph } from './namespace';
	import {
		fetchAccessGraph,
		fetchMyPermissions,
		grantAccess,
		revokeAccess,
	} from './remote/access-objects.api';
	import { GraphCanvas } from '#lib/flow/index.ts';

	let { dataset }: { dataset: string } = $props();

	const target = $derived({ kind: 'table' as const, id: dataset });
	const graphRead = $derived(fetchAccessGraph(target));
	// The caller's own verdicts, for the #143 gate on Grant/Revoke below, read beside the graph: a failed
	// permission read must not blank the graph, it just leaves the verdicts unknown, which renders the
	// controls live. Each read is the object's own, so one object's verdicts never gate another's.
	const permsRead = $derived(fetchMyPermissions(target));
	const permMap = $derived(permsRead.current?.ok ? permsRead.current.data.permissions : null);
	const result = $derived(graphRead.current);
	const graph = $derived(result?.ok ? result.data : null);
	const status = $derived<'loading' | 'ok' | 'denied' | 'offline'>(
		result === undefined
			? 'loading'
			: result.ok
				? 'ok'
				: result.status === 401 || result.status === 403
					? 'denied'
					: 'offline',
	);

	// The node clicked last, keyed by the object it was clicked on.
	let selection = $state<{ for: string; id: string } | null>(null);
	const selectedId = $derived(selection?.for === dataset ? selection.id : null);
	let mgUser = $state('');

	function build(g: AccessGraph, selected: string | null): AccessNodeType[] {
		const obj = g.object;
		const inbound = g.edges.filter((e) => e.target === obj); // grants: subject → obj
		const outbound = g.edges.filter((e) => e.source === obj); // container: obj → parent
		const byId = new Map(g.nodes.map((n) => [n.id, n]));
		const mk = (id: string, x: number, y: number): AccessNodeType => {
			const n = byId.get(id) ?? { id, type: id.split(':')[0] || 'unknown', label: id };
			return {
				id,
				type: 'access',
				position: { x, y },
				data: {
					fgaId: id,
					fgaType: n.type,
					label: n.label,
					focus: id === obj,
					selected: id === selected,
				},
			};
		};
		const next: AccessNodeType[] = [];
		inbound.forEach((e, i) => next.push(mk(e.source, 20, 20 + i * 82)));
		next.push(mk(obj, 330, 20 + (Math.max(1, inbound.length) - 1) * 41));
		outbound.forEach((e, i) => next.push(mk(e.target, 640, 20 + i * 82)));
		return next;
	}

	// Writable deriveds: Svelte Flow writes a drag back through the binding, and the next graph or
	// selection replaces it.
	let nodes = $derived(graph ? build(graph, selectedId) : []);
	let edges = $derived<Edge[]>(
		(graph?.edges ?? []).map((e, i) => ({
			id: `e${i}`,
			source: e.source,
			target: e.target,
			label: e.relation,
			animated: e.relation !== 'parent' && e.relation !== 'project',
		})),
	);
	// The canvas re-frames when the object, its graph's shape or the selection changes.
	const fitTrigger = $derived(
		`${dataset}:${graph?.nodes.length ?? 0}:${graph?.edges.length ?? 0}:${selectedId ?? ''}`,
	);

	/** After a grant or revoke: read the graph and the verdicts again (the write refreshes the graph
	 *  itself, so that read is joined rather than repeated). */
	function reread(): void {
		fetchAccessGraph(target);
		fetchMyPermissions(target);
	}

	function selectNode(e: { node: { id: string; data: unknown } }): void {
		selection = { for: dataset, id: e.node.id };
		const t = (e.node.data as { fgaType?: string }).fgaType ?? '';
		// clicking a subject prefills the grant form with its node id: a user's is the OpenFGA user the store holds,
		// which a grant names as written (the catalog reads a `user:` grantee as that stored id)
		if (['user', 'role'].includes(t)) {
			mgUser = e.node.id;
		}
	}
</script>

<div class="ag" {@attach enter({ y: 6 })}>
	<header>
		<Network size={15} />
		<h3>Authorization graph</h3>
		<span class="sub mono">who holds which rung · click a subject to grant/revoke</span>
	</header>

	{#if status === 'denied'}
		<div class="empty"><ShieldAlert size={15} /> Owner access is required to view the graph.</div>
	{:else if status === 'offline'}
		<div class="empty">Graph unavailable right now — reopen to retry.</div>
	{:else if status === 'loading'}
		<div class="empty">Loading the authorization graph…</div>
	{:else}
		<div class="canvas">
			<GraphCanvas bind:nodes bind:edges {nodeTypes} {fitTrigger} onnodeclick={selectNode} />
		</div>

		<!-- The grant form the grants panel renders too, so both offer the same rungs behind the same
		     gates. `bind:subject` is what keeps clicking a subject node prefilling the form. -->
		<GrantForm
			kind="table"
			bind:subject={mgUser}
			permissions={permMap}
			knownSubjects={(graph?.nodes ?? []).map((n) => n.id)}
			grant={(user, relation) => grantAccess({ kind: 'table', id: dataset, user, relation })}
			revoke={(user, relation) => revokeAccess({ kind: 'table', id: dataset, user, relation })}
			onmutated={reread}
		/>
	{/if}
</div>

<style>
	.ag {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 9px;
	}
	h3 {
		font-size: 14px;
		margin: 0;
	}
	.sub {
		color: var(--muted-foreground);
		font-size: 11px;
	}
	.canvas {
		height: 340px;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		overflow: hidden;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--muted-foreground);
		font-size: 12px;
		padding: 24px 0;
	}
</style>
