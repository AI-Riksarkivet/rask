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
	import { GatedAction } from '@rask/ui/gated-action';
	import { Select } from '@rask/ui/select';
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

	const GRANTABLE = ['reader', 'writer', 'validator', 'owner'];

	let nodes = $state.raw<AccessNodeType[]>([]);
	let edges = $state.raw<Edge[]>([]);
	let graph = $state<AccessGraph | null>(null);
	let selectedId = $state<string | null>(null);
	let fitKey = $state(0);
	let status = $state<'loading' | 'ok' | 'denied' | 'offline'>('loading');
	let mgUser = $state('');
	let mgRelation = $state('');
	let mgBusy = $state(false);
	let mgResult = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	// #143, same treatment as `@rask/ui`'s GrantsPanel — this component carries a SECOND copy of the
	// grant/revoke UI, which is how the wrong denial message survived in two places. Keyed by dataset,
	// so one object's verdicts never gate another's buttons after a navigation. Declared AFTER the
	// `mg*` state it reads: `$derived` is lazy, but keeping the reading order honest is cheaper than
	// relying on that.
	let perms = $state<{ for: string; map: Record<string, boolean> } | null>(null);
	const permMap = $derived(perms?.for === dataset ? perms.map : null);
	// Unknown (read failed, or not loaded yet) renders LIVE: this gate exists to explain a refusal, not
	// to invent one from a missing read.
	const mayGrant = $derived(
		permMap === null || !mgRelation ? true : permMap[`can_grant_${mgRelation}`] === true,
	);
	const mayRevoke = $derived(permMap === null ? true : permMap.can_revoke_grant === true);

	// Same advisory as `@rask/ui`'s GrantsPanel — the placeholder here taught the same wrong shape.
	// A tuple is written for the id EXACTLY as typed while a store keys on the OIDC `sub`, so a display
	// name grants to nobody AND still answers 200. The graph's own nodes are the directory: they carry
	// the real subject ids this object already grants to.
	const knownSubjects = $derived(new Set((graph?.nodes ?? []).map((n) => n.id)));
	const looksUnresolvable = $derived.by(() => {
		const u = mgUser.trim();
		if (!u || u.includes(':') || u.includes('#')) return false;
		if (knownSubjects.has(u) || knownSubjects.has(`user:${u}`)) return false;
		return /^[a-z][a-z0-9._-]{0,30}$/i.test(u);
	});

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

	async function runManage(grant: boolean): Promise<void> {
		const user = mgUser.trim();
		if (mgBusy || !user || !mgRelation) return;
		mgBusy = true;
		mgResult = null;
		const current = dataset;
		try {
			const args = { kind: 'table', id: current, user, relation: mgRelation } as const;
			const res = grant ? await grantAccess(args) : await revokeAccess(args);
			if (dataset !== current) return;
			if (res.ok) {
				mgResult = {
					tone: 'ok',
					text: `${mgRelation} ${grant ? 'granted to' : 'revoked from'} ${res.data.user}.`,
				};
				await load(); // the graph now reflects the change
			} else if (res.status === 401 || res.status === 403) {
				// Same correction as `@rask/ui`'s GrantsPanel, which carried a second copy of this exact
				// sentence: grant/revoke stopped being owner-tier when the grant axis was separated from
				// ownership. `_authorize_grant` reads the rung from the body — grant needs
				// `can_grant_<rung>`, revoke needs `can_revoke_grant` (`manage_grants` alone). Under
				// `managed_access` an owner may hold the owner tier and still be refused, because that
				// flag withdraws precisely `manage_grants` and the grant option beneath it.
				mgResult = {
					tone: 'fail',
					text: grant
						? `Granting ${mgRelation} here needs can_grant_${mgRelation} on this table — held by a grant-manager, or by someone holding ${mgRelation} plus the grant option. Owning the table is not sufficient if access is centrally managed.`
						: 'Revoking here needs can_revoke_grant on this table — grant-manager only, deliberately stricter than granting so a delegate cannot strip the owner who delegated to them.',
				};
			} else {
				mgResult = { tone: 'fail', text: `Failed (HTTP ${res.status}).` };
			}
		} finally {
			mgBusy = false;
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

		<div class="manage">
			<input
				class="mono"
				placeholder="OIDC sub, or role:…#assignee / team:…#member"
				bind:value={mgUser}
			/>
			<Select
				bind:value={mgRelation}
				ariaLabel="Rung"
				placeholder="rung…"
				options={GRANTABLE.map((r) => ({ value: r, label: r }))}
			/>
			<!-- #143: refused stays visible and says why. `disabled` is conditional on the verdict because
			     GatedAction deliberately avoids the native attribute (it would kill the tooltip and drop
			     the control from the tab order), so a natively-disabled child defeats the mechanism. -->
			<GatedAction
				allowed={mayGrant}
				action={`Grant ${mgRelation || 'a rung'}`}
				reason={`Granting ${mgRelation || 'a rung'} here needs can_grant_${mgRelation || '<rung>'} on this table — held by a grant-manager, or by someone holding ${mgRelation || 'that rung'} plus the grant option. Owning the table is not sufficient if access is centrally managed.`}
			>
				<button
					class="btn"
					disabled={mayGrant && (mgBusy || !mgUser.trim() || !mgRelation)}
					onclick={() => runManage(true)}
				>
					{mgBusy ? '…' : 'Grant'}
				</button>
			</GatedAction>
			<GatedAction
				allowed={mayRevoke}
				action="Revoke"
				reason="Revoking here needs can_revoke_grant on this table — grant-manager only, deliberately stricter than granting so a delegate cannot strip the owner who delegated to them."
			>
				<button
					class="btn ghost"
					disabled={mayRevoke && (mgBusy || !mgUser.trim() || !mgRelation)}
					onclick={() => runManage(false)}
				>
					Revoke
				</button>
			</GatedAction>
			{#if looksUnresolvable}
				<p class="advice">
					Granted exactly as typed. A signed-in user's subject is their OIDC <code>sub</code> — a long
					opaque id, not a display name — so <code>{mgUser.trim()}</code> matches nobody unless that is
					literally the subject id (a service account or a userset).
				</p>
			{/if}
			{#if mgResult}
				<span class="res" class:ok={mgResult.tone === 'ok'} class:fail={mgResult.tone === 'fail'}
					>{mgResult.text}</span
				>
			{/if}
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
	.manage {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
	}
	.manage input {
		flex: 1 1 220px;
		min-width: 160px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.res {
		font-size: 12px;
	}
	.res.ok {
		color: var(--ok);
	}
	.res.fail {
		color: var(--fail);
	}
	.advice {
		margin-top: 6px;
		font-size: 11px;
		line-height: 1.5;
		color: var(--color-muted-foreground);
	}
</style>
