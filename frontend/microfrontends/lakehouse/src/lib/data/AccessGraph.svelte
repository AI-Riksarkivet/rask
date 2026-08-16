<script lang="ts" module>
	// #81 Authorization graph — an interactive relationship explorer for a table's OpenFGA grants, the
	// visual upgrade of the #68 who-can-do-what form. One hop: the focus object, every subject directly
	// granted a rung (edges subject→object labelled owner/writer/reader/validator), and the parent/project
	// container edge. Click a subject to prefill the grant form; grant/revoke re-fetches so the graph
	// reflects the change. SvelteFlow (reusing the LineageExplorer pattern) + @rask/ui Select + GSAP.
	import AccessNode, { type AccessNodeType } from '$lib/data/AccessNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { access: AccessNode };
</script>

<script lang="ts">
	import { GrantForm } from '@rask/ui/grant-form';
	import { enter } from '@rask/ui/motion';
	import { Network, ShieldAlert } from '@lucide/svelte';
	import { Background, BackgroundVariant, Controls, type Edge, SvelteFlow } from '@xyflow/svelte';
	import type { AccessGraph } from './namespace';
	import {
		fetchAccessGraph,
		fetchMyPermissions,
		grantAccess,
		revokeAccess,
	} from './remote/access-objects.remote';
	import { FlowAutoFit } from '@rask/flow';

	let { dataset }: { dataset: string } = $props();



	let nodes = $state.raw<AccessNodeType[]>([]);
	let edges = $state.raw<Edge[]>([]);
	let graph = $state<AccessGraph | null>(null);
	let selectedId = $state<string | null>(null);
	let fitKey = $state(0);
	let status = $state<'loading' | 'ok' | 'denied' | 'offline'>('loading');
	let mgUser = $state('');




	// #143, same treatment as `@rask/ui`'s GrantsPanel — this component carries a SECOND copy of the
	// grant/revoke UI, which is how the wrong denial message survived in two places. Keyed by dataset,
	// so one object's verdicts never gate another's buttons after a navigation. Declared AFTER the
	// `mg*` state it reads: `$derived` is lazy, but keeping the reading order honest is cheaper than
	// relying on that.
	let perms = $state<{ for: string; map: Record<string, boolean> } | null>(null);
	const permMap = $derived(perms?.for === dataset ? perms.map : null);




	function rebuild(g: AccessGraph): void {
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
					selected: id === selectedId,
				},
			};
		};
		const next: AccessNodeType[] = [];
		inbound.forEach((e, i) => next.push(mk(e.source, 20, 20 + i * 82)));
		next.push(mk(obj, 330, 20 + (Math.max(1, inbound.length) - 1) * 41));
		outbound.forEach((e, i) => next.push(mk(e.target, 640, 20 + i * 82)));
		nodes = next;
		edges = g.edges.map((e, i) => ({
			id: `e${i}`,
			source: e.source,
			target: e.target,
			label: e.relation,
			animated: e.relation !== 'parent' && e.relation !== 'project',
		}));
		fitKey++;
	}

	async function load(): Promise<void> {
		const current = dataset;
		const res = await fetchAccessGraph({ kind: 'table', id: current });
		// The caller's own verdicts, for the #143 gate on Grant/Revoke below. Fire-and-forget with the
		// same latest-wins guard: a failed permission read must not blank the graph, it just leaves the
		// verdicts unknown, which renders the controls live.
		void fetchMyPermissions({ kind: 'table', id: current }).then((r) => {
			if (dataset !== current) return;
			perms = r.ok ? { for: current, map: r.data.permissions } : null;
		});
		if (dataset !== current) return; // navigated away — drop stale
		if (res.ok) {
			graph = res.data;
			status = 'ok';
			rebuild(res.data);
		} else if (res.status === 401 || res.status === 403) {
			status = 'denied';
		} else {
			status = 'offline';
		}
	}

	$effect(() => {
		void dataset;
		status = 'loading';
		graph = null;
		selectedId = null;
		load();
	});

	function selectNode(e: { node: { id: string; data: unknown } }): void {
		selectedId = e.node.id;
		const t = (e.node.data as { fgaType?: string }).fgaType ?? '';
		// clicking a subject prefills the grant form (drop the bare user: prefix for a plain user)
		if (['user', 'role', 'team'].includes(t)) {
			mgUser = e.node.id.startsWith('user:') ? e.node.id.slice('user:'.length) : e.node.id;
		}
		if (graph) rebuild(graph);
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
			<SvelteFlow
				bind:nodes
				bind:edges
				{nodeTypes}
				colorMode="dark"
				fitView
				onnodeclick={selectNode}
			>
				<Background variant={BackgroundVariant.Dots} gap={16} />
				<Controls />
				<FlowAutoFit trigger={`${dataset}:${fitKey}`} />
			</SvelteFlow>
		</div>

		<!-- ONE implementation of this form, shared with `@rask/ui`'s GrantsPanel. It used to live here
		     too, and that duplication was the actual defect behind three bugs fixed on 2026-08-16 — each
		     paid for twice — while the copies had already drifted: this one offered four rungs to the
		     panel's six, so `pass_grants` and `manage_grants` were ungrantable from the graph and
		     grantable from the panel, with no decision behind the difference.
		     `bind:subject` is what keeps clicking a subject node prefilling the form. -->
		<GrantForm
			kind="table"
			bind:subject={mgUser}
			permissions={permMap}
			knownSubjects={(graph?.nodes ?? []).map((n) => n.id)}
			grant={(user, relation) => grantAccess({ kind: 'table', id: dataset, user, relation })}
			revoke={(user, relation) => revokeAccess({ kind: 'table', id: dataset, user, relation })}
			onmutated={load}
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
		color: var(--faint);
		font-size: 11px;
	}
	.canvas {
		height: 340px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		overflow: hidden;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 12px;
		padding: 24px 0;
	}
</style>
