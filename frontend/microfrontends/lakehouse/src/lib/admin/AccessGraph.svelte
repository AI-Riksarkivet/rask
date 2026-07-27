<script lang="ts" module>
	// The FGA workbench's Graph tab — the #81 SvelteFlow relationship explorer generalized from the
	// table-scoped data-zone version: seed it with ANY object id and it renders one hop of the live
	// tuple graph around it, straight off GET /v1/access/tuples (estate-admin gated by the catalog).
	// Inbound tuples (object == seed: subjects + containment pointing in) sit left; outbound tuples
	// (user == seed: everything the seed itself holds) sit right. Click any neighbor to re-seed —
	// the graph is the navigation.
	import AccessNode, { type AccessNodeType } from '$lib/admin/AccessNode.svelte';
	import type { NodeTypes } from '@xyflow/svelte';

	// svelte-flow rule 5: register node components ONCE at module scope, not inline.
	const nodeTypes: NodeTypes = { access: AccessNode };
</script>

<script lang="ts">
	import { enter } from '@rask/ui/motion';
	import { Network, ShieldAlert } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { Background, BackgroundVariant, Controls, type Edge, SvelteFlow } from '@xyflow/svelte';
	import {
		AccessModelSchema,
		fetchAccessModel,
		fetchTuples,
		parseModelTypes,
		type Tuple,
		TuplesPageSchema,
	} from './access';
	import FlowAutoFit from './FlowAutoFit.svelte';

	let { object, onseed }: { object: string; onseed: (id: string) => void } = $props();

	// The whole one-hop neighborhood in one window per side — a hub past this renders truncated
	// (the +N chip), which beats silently dropping edges.
	const PAGE = 100;

	let nodes = $state.raw<AccessNodeType[]>([]);
	let edges = $state.raw<Edge[]>([]);
	let fitKey = $state(0);
	let status = $state<'loading' | 'ok' | 'denied' | 'offline'>('loading');
	let truncated = $state(false);
	let empty = $state(false);
	// Outbound legs that failed (or 'model' when the type list itself was unreadable) — the graph
	// still renders what arrived, flagged partial, instead of going dark on one bad leg.
	let failedLegs = $state<string[]>([]);

	const idType = (id: string): string => id.split(':')[0] || 'unknown';
	const idLabel = (id: string): string => (id.includes(':') ? id.slice(id.indexOf(':') + 1) : id);

	function rebuild(seed: string, inbound: Tuple[], outbound: Tuple[]): void {
		const mk = (id: string, x: number, y: number): AccessNodeType => ({
			id,
			type: 'access',
			position: { x, y },
			data: { fgaId: id, fgaType: idType(id), label: idLabel(id), focus: id === seed },
		});
		// Dedupe per side (a subject can hold several relations — one node, several edges).
		const left = [...new Set(inbound.map((t) => t.user))].filter((id) => id !== seed);
		const right = [...new Set(outbound.map((t) => t.object))].filter((id) => id !== seed);
		const next: AccessNodeType[] = [];
		left.forEach((id, i) => next.push(mk(id, 20, 20 + i * 82)));
		next.push(mk(seed, 330, 20 + (Math.max(1, Math.max(left.length, right.length)) - 1) * 41));
		right.forEach((id, i) => next.push(mk(id, 640, 20 + i * 82)));
		nodes = next;
		// Every edge points user → object (the tuple's own direction); containment stays static.
		edges = [...inbound, ...outbound].map((t, i) => ({
			id: `e${i}`,
			source: t.user,
			target: t.object,
			label: t.relation,
			animated: t.relation !== 'parent' && t.relation !== 'project',
		}));
		empty = left.length === 0 && right.length === 0;
		fitKey++;
	}

	async function load(): Promise<void> {
		const seed = object;
		// The hop: tuples ON the seed (inbound, one {object} read) + tuples BY the seed (outbound).
		// The API rejects a bare `user` filter by design (an OpenFGA Read needs an object type), so
		// the outbound side fans out one {user, objectType} read per model type — bounded by the
		// model DSL, read off the same /v1/access/model the Model tab shows.
		const [inRes, modelRes] = await Promise.all([
			fetchTuples({ object: seed, pageSize: PAGE }),
			fetchAccessModel(),
		]);
		if (object !== seed) return; // re-seeded mid-flight — drop stale
		if (!inRes.ok) {
			status = inRes.status === 401 || inRes.status === 403 ? 'denied' : 'offline';
			return;
		}
		if (!modelRes.ok && (modelRes.status === 401 || modelRes.status === 403)) {
			status = 'denied';
			return;
		}
		let types: string[] = [];
		let modelFailed = !modelRes.ok;
		if (modelRes.ok) {
			try {
				types = parseModelTypes(parse(AccessModelSchema, modelRes.data).dsl);
			} catch (err) {
				console.error(`access model parse failure: ${String(err)}`);
				modelFailed = true;
			}
		}
		const outLegs = await Promise.all(
			types.map(async (t) => ({
				type: t,
				res: await fetchTuples({ user: seed, objectType: t, pageSize: PAGE }),
			})),
		);
		if (object !== seed) return; // re-seeded mid-flight — drop stale
		// Partial results beat none: a failed type-leg is named in the header, not fatal.
		const outbound: Tuple[] = [];
		const failed: string[] = modelFailed ? ['model'] : [];
		let outTruncated = false;
		for (const leg of outLegs) {
			if (!leg.res.ok) {
				failed.push(leg.type);
				continue;
			}
			try {
				const pageData = parse(TuplesPageSchema, leg.res.data);
				outbound.push(...pageData.tuples);
				if (pageData.continuation !== null) outTruncated = true;
			} catch (err) {
				console.error(`access graph parse failure (${leg.type} leg): ${String(err)}`);
				failed.push(leg.type);
			}
		}
		try {
			const inbound = parse(TuplesPageSchema, inRes.data);
			truncated = inbound.continuation !== null || outTruncated;
			failedLegs = failed;
			rebuild(seed, inbound.tuples, outbound);
			status = 'ok';
		} catch (err) {
			console.error(`access graph parse failure: ${String(err)}`);
			status = 'offline';
		}
	}

	$effect(() => {
		void object;
		status = 'loading';
		truncated = false;
		failedLegs = [];
		load();
	});

	function selectNode(e: { node: { id: string } }): void {
		// Clicking a neighbor re-seeds the graph on it — clicking the focus itself is a no-op.
		if (e.node.id !== object) onseed(e.node.id);
	}
</script>

<div class="ag" {@attach enter({ y: 6 })}>
	<header>
		<Network size={15} />
		<h3>Relationship graph</h3>
		<span class="sub mono">one hop of live tuples around the seed · click a neighbor to re-seed</span>
		{#if truncated}
			<span class="trunc mono" title="More tuples exist than one page — the hop is truncated">
				truncated at {PAGE}/side
			</span>
		{/if}
		{#if failedLegs.length}
			<span
				class="trunc mono"
				title="Part of the outbound hop failed to load — the graph shows what arrived"
			>
				partial · {failedLegs.join(', ')} failed
			</span>
		{/if}
	</header>

	{#if status === 'denied'}
		<div class="empty">
			<ShieldAlert size={15} /> The tuple graph is estate-admin only.
		</div>
	{:else if status === 'offline'}
		<div class="empty">Tuple store unreachable — re-seed to retry.</div>
	{:else if status === 'loading'}
		<div class="empty">Loading the relationship graph…</div>
	{:else}
		{#if empty}
			<div class="empty">
				No tuples touch <span class="mono">{object}</span> — nothing granted on it and it holds nothing.
			</div>
		{/if}
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
				<FlowAutoFit trigger={`${object}:${fitKey}`} />
			</SvelteFlow>
		</div>
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
	.trunc {
		margin-left: auto;
		color: var(--warn, #d18b28);
		font-size: 11px;
	}
	.canvas {
		height: 380px;
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
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
