<script lang="ts">
	// `/namespaces/<id>` — the namespace detail page (sweep group 3): the catalog's namespace-scoped
	// governance surfaces (access list/check/grant/revoke + maintenance policy set/describe/delete)
	// have been live since #50/#51/#72 with no UI — this page closes that asymmetry. Header (id +
	// table count from the registry the /namespaces page already derives from), the kind-generalized
	// GrantsPanel, and a maintenance-policy card mirroring the table policy form. Same stack-mode
	// states as the registry — governed without a session ⇒ sign-in, unreachable ⇒ retrying.
	import { GatedAction } from '@rask/ui/gated-action';
	import { GrantsPanel, type GrantsClient } from '@rask/ui/grants-panel';
	import { subjectDisplay } from '@rask/ui/identity';
	import { Boxes, Network, RefreshCw, ShieldAlert, Trash2 } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { fetchTables } from '$lib/data/remote/catalog.remote';
	import DetailTabs from '$lib/data/DetailTabs.svelte';
	import StageBadge from '$lib/data/StageBadge.svelte';
	import { namespaceOfTable, stageOf } from '$lib/data/stage';
	import { policyRequestFrom } from '$lib/data/namespace';
	import type { AccessGraph, NamespacePolicy } from '$lib/data/namespace';
	import {
		checkAccess,
		fetchAccess,
		fetchAccessGraph,
		fetchManagedAccess,
		fetchMyPermissions,
		grantAccess,
		revokeAccess,
	} from '$lib/data/remote/access-objects.remote';
	import {
		deleteNamespacePolicy,
		fetchNamespacePolicy,
		setNamespacePolicy,
	} from '$lib/data/remote/namespace.remote';

	const ns = $derived(page.params.id ?? '');

	// Goal cond 3: the FGA view lives on an Access TAB (overview stays the default); the medallion
	// stage badge is derived from the namespace name.
	let tab = $state('overview');
	const stageInfo = $derived(stageOf(ns));

	// The zone-owned catalog seam the shared @rask/ui GrantsPanel calls (the lib never owns an API
	// client). The panel's positional signature is bound to the remote functions' single argument here.
	const grantsClient: GrantsClient = {
		fetchAccess: (kind, id) => fetchAccess({ kind, id }),
		checkAccess: (kind, id, user, relation) => checkAccess({ kind, id, user, relation }),
		grantAccess: (kind, id, user, relation) => grantAccess({ kind, id, user, relation }),
		revokeAccess: (kind, id, user, relation) => revokeAccess({ kind, id, user, relation }),
		// #143: the panel renders a refused Grant/Revoke DISABLED WITH ITS REASON rather than letting
		// the user discover the denial from a 403. It needs the caller's own verdicts to do that, and
		// the zone owns the transport — so the seam gains a fifth member like the four above.
		fetchMyPermissions: (kind, id) => fetchMyPermissions({ kind, id }),
	};

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let tables = $state<string[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	// #50 policy card state — 'none' (404: honest "global defaults apply"), 'set', 'denied' (401),
	// 'unavailable' (5xx/offline/contract drift: never an affirmative empty state that would invite
	// an overwriting write — the same stance as TableDetail's policyUnavailable).
	let policy = $state<NamespacePolicy | null>(null);
	let policyPart = $state<'loading' | 'none' | 'set' | 'denied' | 'unavailable'>('loading');
	let policyDenied = $state<string | null>(null);
	let policyError = $state<string | null>(null);
	let editingPolicy = $state(false);
	let busy = $state(false);
	// number | null, matching what bind:value on a type="number" input delivers (Svelte 5 coerces to
	// a number, or null for an empty field) — same shape TableDetail's audit-hardened draft uses.
	let draft = $state<{
		retention_days: number | null;
		retain_versions: number | null;
		interval: number | null;
		target: number | null;
		enabled: boolean;
	}>({ retention_days: null, retain_versions: null, interval: null, target: null, enabled: true });

	// #81 one hop of the authorization graph around the namespace, as a compact LIST card (TableDetail's
	// lazy-toggle pattern without the SvelteFlow canvas — the namespace page has no inline grant form to
	// prefill, so a list states the same edges more cheaply). Owner-gated by the catalog (can_delete).
	let showGraph = $state(false);
	let graph = $state<AccessGraph | null>(null);
	let graphStatus = $state<'loading' | 'ok' | 'denied' | 'offline'>('loading');

	// What the SIGNED-IN caller may do here, from the catalog's self-view. This page used to render
	// every action unconditionally and report the denial only after the click, which was survivable
	// while a reader could not reach the page at all — the namespace 403'd first. Upward visibility
	// ended that: a single-table grantee now navigates here legitimately, so an ungated `Set policy`
	// would be a button that exists solely to fail. `null` means "not answered yet", which is NOT the
	// same as "denied": treating it as denied would hide an owner's own controls on every first paint.
	let perms = $state<Record<string, boolean> | null>(null);
	// Whether granting here is CENTRALIZED. Distinct from `perms`: that says what the caller may do,
	// this says WHY — an owner whose grant controls vanished is otherwise looking at a bug. `null`
	// means unanswered, which is not the same as "not managed": claiming a policy before the read
	// lands would be as wrong as hiding one.
	let managed = $state<boolean | null>(null);
	// Namespace policy set/delete is gated by the catalog on `can_delete` (fga_deps' suffix map), so
	// that is the rung the button must agree with — not a rung this page invents.
	const mayEditPolicy = $derived(perms?.can_delete === true);
	// ONE string, shared by the three gated policy controls and the sentence below them — three copies
	// of a denial reason is how the grant message came to be wrong in two places at once.
	const REASON_EDIT = $derived(`Changing this policy needs the owner rung (can_delete) on ${ns}.`);
	const permsSettled = $derived(perms !== null);
	// The one case worth naming separately: the caller owns this namespace, and granting is still
	// denied. That is managed access doing its job, and it is the only reading under which an owner
	// should NOT read a missing grant control as broken.
	const grantsCentralized = $derived(managed === true && perms?.can_delete === true);

	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && settled && lastStatus !== 401);

	// The namespace's member tables, grouped the way the registry groups them — through the SAME helper,
	// not a local re-implementation of it. This carried its own copy that split on the FIRST delimiter,
	// the identical bug fixed in `namespaceOfTable` earlier today: for a nested namespace it compared
	// the top-level ancestor against `ns`, so `acme$bronze` listed none of its own tables and `acme`
	// listed all of them. Two copies of one rule is how they came to disagree.
	const members = $derived((tables ?? []).filter((t) => namespaceOfTable(t) === ns).sort());

	async function loadTables(): Promise<void> {
		const current = ns;
		const res = await fetchTables();
		if (ns !== current) return; // latest-wins across a namespace navigation
		settled = true;
		if (res.ok) {
			tables = [...res.data.tables];
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	async function loadPerms(): Promise<void> {
		const current = ns;
		const res = await fetchMyPermissions({ kind: 'namespace', id: current });
		if (ns !== current) return; // latest-wins across a namespace navigation
		// A failure leaves `perms` null — "unanswered", so actions stay hidden rather than being shown
		// on a guess. The catalog is the authority either way; this only decides what to RENDER.
		if (res.ok) perms = res.data.permissions;
	}

	async function loadManaged(): Promise<void> {
		const current = ns;
		const res = await fetchManagedAccess({ kind: 'namespace', id: current });
		if (ns !== current) return; // latest-wins across a namespace navigation
		if (res.ok) managed = res.data.managed_access;
	}

	async function loadGraph(): Promise<void> {
		const current = ns;
		graphStatus = 'loading';
		const res = await fetchAccessGraph({ kind: 'namespace', id: current });
		if (ns !== current) return; // latest-wins across a namespace navigation
		if (res.ok) {
			graph = res.data;
			graphStatus = 'ok';
		} else if (res.status === 401 || res.status === 403) {
			graphStatus = 'denied';
		} else {
			graphStatus = 'offline';
		}
	}

	function toggleGraph(): void {
		showGraph = !showGraph;
		if (showGraph) loadGraph();
	}

	// Split the one-hop edges for the card: inbound = grants (subject holds a rung ON the namespace),
	// outbound = the container edge (namespace → parent/project). Labels come from the graph's nodes.
	// #68: node labels carry raw OIDC subs — render the display form, keep the full value for title.
	const graphNode = (id: string): { label: string; title: string } =>
		subjectDisplay(graph?.nodes.find((n) => n.id === id)?.label ?? id);
	const grantEdges = $derived.by(() => {
		const g = graph;
		return g === null ? [] : g.edges.filter((e) => e.target === g.object);
	});
	const containerEdges = $derived.by(() => {
		const g = graph;
		return g === null ? [] : g.edges.filter((e) => e.source === g.object);
	});

	async function loadPolicy(): Promise<void> {
		const current = ns;
		try {
			const res = await fetchNamespacePolicy({ namespace: current });
			if (ns !== current) return;
			if (res.ok) {
				policy = res.data;
				policyPart = 'set';
			} else if (res.status === 404) {
				policy = null;
				policyPart = 'none';
			} else if (res.status === 401) {
				policyDenied = 'Sign in to view the maintenance policy.';
				policyPart = 'denied';
			} else if (res.status === 403) {
				policyDenied = 'Denied: viewing this policy needs a reader rung on the namespace.';
				policyPart = 'denied';
			} else {
				policyPart = 'unavailable';
			}
		} catch (err) {
			// the parse boundary throws on a wire-contract drift — surface it, never render from a lie
			if (ns !== current) return;
			policyError = `policy response drifted from the contract: ${String(err)}`;
			policyPart = 'unavailable';
		}
	}

	$effect(() => {
		// Reset every piece of state on a namespace change — including the edit form, or an editor
		// opened on A would survive into B and Save would write A's draft to B (the TableDetail audit).
		void ns;
		tab = 'overview';
		tables = null;
		lastStatus = 0;
		settled = false;
		policy = null;
		policyPart = 'loading';
		policyDenied = null;
		policyError = null;
		editingPolicy = false;
		busy = false;
		// #81 graph card — reset too, or namespace A's grantees would flash on B until its fetch lands.
		showGraph = false;
		graph = null;
		graphStatus = 'loading';
		// Reset to "unanswered" too — carrying A's verdicts into B would render B's controls from
		// A's grants, which is the same class of bug as the edit form surviving a namespace change.
		perms = null;
		managed = null;
		loadTables();
		loadPolicy();
		loadPerms();
		loadManaged();
	});

	function startPolicyEdit(): void {
		draft = {
			retention_days: policy?.retention_days ?? null,
			retain_versions: policy?.retain_versions ?? null,
			interval: policy?.compact_interval_hours ?? null,
			target: policy?.target_rows_per_fragment ?? null,
			enabled: policy?.compact_enabled ?? true,
		};
		policyError = null;
		editingPolicy = true;
	}

	function policyFail(status: number, detailText: string): void {
		if (status === 401) policyError = 'Sign in to edit the maintenance policy.';
		else if (status === 403)
			policyError = 'Denied: policy changes need the owner rung (can_delete).';
		else policyError = detailText;
	}

	async function savePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		const current = ns;
		try {
			const body = policyRequestFrom(draft);
			const res = await setNamespacePolicy({ namespace: current, policy: body });
			if (ns !== current) return; // navigated away — drop the stale result
			if (res.ok) {
				policy = res.data;
				policyPart = 'set';
				editingPolicy = false;
			} else {
				policyFail(res.status, res.detail);
			}
		} catch (err) {
			if (ns === current) policyError = `policy response drifted from the contract: ${String(err)}`;
		} finally {
			busy = false;
		}
	}

	async function removePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		const current = ns;
		try {
			const res = await deleteNamespacePolicy({ namespace: current });
			if (ns !== current) return;
			if (res.ok) {
				policy = null;
				policyPart = 'none';
			} else {
				policyFail(res.status, res.detail);
			}
		} catch (err) {
			if (ns === current) policyError = `policy response drifted from the contract: ${String(err)}`;
		} finally {
			busy = false;
		}
	}
</script>

<svelte:head><title>{ns} · namespaces · lance</title></svelte:head>

<div class="page">
	<header>
		<a class="back" href={`${base}/catalog/namespaces`}>Namespaces</a>
		<span class="sep">/</span>
		<Boxes size={15} />
		<h1 class="mono">{ns}</h1>
		{#if stageInfo}<StageBadge info={stageInfo} />{/if}
		{#if tables !== null}
			<span class="count">{members.length} table{members.length === 1 ? '' : 's'}</span>
		{/if}
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view this namespace.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if tables === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<!-- Goal cond 3: overview (default) | access — the FGA view moved onto its own tab. -->
		<DetailTabs tabs={['overview', 'access']} bind:active={tab} />
		{#if tab === 'overview'}
			<section>
				<h2>Tables</h2>
				{#if members.length === 0}
					<p class="mut">No tables in this namespace.</p>
				{:else}
					<ul class="list">
						{#each members as t (t)}
							<li>
								<a class="row mono" href={`${base}/catalog/tables/${encodeURIComponent(t)}`}>{t}</a>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section>
				<h2>Maintenance policy</h2>
				<p class="mut">
					A namespace policy governs every dataset under <span class="mono">{ns}</span> unless a table
					policy overrides it; tag-pinned versions (e.g. blessed) are never cleaned up.
				</p>
				{#if editingPolicy}
					<div class="policy-edit">
						<label
							>retention days <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.retention_days}
								placeholder="global default"
							/></label
						>
						<label
							>retain versions <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.retain_versions}
								placeholder="—"
							/></label
						>
						<label
							>compact every (h) <input
								class="mono"
								type="number"
								min="1"
								bind:value={draft.interval}
								placeholder="every sweep"
							/></label
						>
						<label
							>target rows/fragment <input
								class="mono"
								type="number"
								min="1024"
								bind:value={draft.target}
								placeholder="Lance default"
							/></label
						>
						<label class="check"
							><input type="checkbox" bind:checked={draft.enabled} /> maintenance enabled</label
						>
						<div class="btnrow">
							<button class="btn" disabled={busy} onclick={savePolicy}>Save policy</button>
							<button class="btn ghost" onclick={() => (editingPolicy = false)}>Cancel</button>
						</div>
					</div>
				{:else if policyPart === 'loading'}
					<p class="mut">Loading policy…</p>
				{:else if policyPart === 'denied'}
					<p class="mut">{policyDenied}</p>
				{:else if policyPart === 'unavailable'}
					<p class="mut">
						Policy unavailable right now — not shown to avoid an overwriting edit against a stale
						read.
					</p>
				{:else if policy}
					<div class="refs">
						{#if policy.retention_days}<span class="chip mono">retention {policy.retention_days}d</span>{/if}
						{#if policy.retain_versions}<span class="chip mono">keep last {policy.retain_versions}</span>{/if}
						{#if policy.compact_interval_hours}<span class="chip mono">every {policy.compact_interval_hours}h</span>{/if}
						{#if policy.target_rows_per_fragment}<span class="chip mono">target {policy.target_rows_per_fragment}
						rows/frag</span>{/if}
						{#if !policy.compact_enabled}<span class="chip off mono">maintenance off</span>{/if}
						<!-- #143 (owner ruling, 2026-08-16): a gated action stays VISIBLE and disabled with its
						     reason. These were `{#if mayEditPolicy}` — absent, with the reason in a separate
						     sentence below. The sentence STAYS: it is visible without hovering, which the
						     tooltip is not, and it names the rung to ask for. What changes is that the
						     operations themselves are now discoverable rather than invisible.
						     `disabled` is conditional on the verdict because GatedAction deliberately avoids
						     the native attribute — it would kill the tooltip and drop the control from the
						     tab order. Until the self-view answers (`permsSettled` false) they render live,
						     since "not answered yet" is not "denied". -->
						<GatedAction
							allowed={!permsSettled || mayEditPolicy}
							action="Edit policy"
							reason={REASON_EDIT}
						>
							<button class="btn ghost" onclick={startPolicyEdit}>Edit</button>
						</GatedAction>
						<GatedAction
							allowed={!permsSettled || mayEditPolicy}
							action="Remove policy"
							reason={REASON_EDIT}
						>
							<button
								class="btn ghost danger"
								disabled={(!permsSettled || mayEditPolicy) && busy}
								onclick={removePolicy}
							>
								<Trash2 size={12} /> Remove
							</button>
						</GatedAction>
					</div>
				{:else}
					<p class="mut">
						No policy — the sweep applies the global defaults.
						<GatedAction
							allowed={!permsSettled || mayEditPolicy}
							action="Set policy"
							reason={REASON_EDIT}
						>
							<button class="btn ghost" onclick={startPolicyEdit}>Set policy</button>
						</GatedAction>
					</p>
				{/if}
				<!-- Say WHY the controls are absent, once the self-view has actually answered. Silence
				     would read as a broken page to someone who expected to be able to edit, and the
				     rung is the useful part of the answer — it names what to ask for. -->
				{#if permsSettled && !mayEditPolicy}
					<p class="mut">{REASON_EDIT}</p>
				{/if}
				{#if policyError}<p class="error">{policyError}</p>{/if}
			</section>
		{:else}
			<section>
				<h2>Access</h2>
				<!-- Managed access is a POLICY, and an unexplained absence is indistinguishable from a
				     bug. Shown only once the read has landed and only when it actually explains
				     something the caller can see: they own this namespace and granting is still denied.
				     Anyone without the owner rung already gets the rung message and does not need a
				     second reason. -->
				{#if grantsCentralized}
					<p class="mut managed">
						Granting here is <strong>centralized</strong>. Owners of this namespace keep every other
						power and cannot hand out access; a grant-manager on the warehouse above does that.
						Clearing it is theirs too — a policy you can switch off from inside is not a policy.
					</p>
				{/if}
				<GrantsPanel dataset={ns} kind="namespace" client={grantsClient} />
				<!-- #81 one hop of the authorization graph, lazy-loaded as a compact list card. -->
				<button class="btn ghost graphtoggle" onclick={toggleGraph}>
					{showGraph ? 'Hide' : 'Show'} authorization graph
				</button>
				{#if showGraph}
					<div class="graphcard">
						<header class="graphhead">
							<Network size={14} />
							<h3>Authorization graph</h3>
							<span class="mut mono">who holds which rung · one hop around {ns}</span>
						</header>
						{#if graphStatus === 'denied'}
							<p class="mut">
								<ShieldAlert size={13} /> Owner access is required to view the graph.
							</p>
						{:else if graphStatus === 'offline'}
							<p class="mut">Graph unavailable right now — reopen to retry.</p>
						{:else if graphStatus === 'loading'}
							<p class="mut">Loading the authorization graph…</p>
						{:else if graph}
							{#if grantEdges.length === 0}
								<p class="mut">No direct grants on this namespace.</p>
							{:else}
								<ul class="edges">
									{#each grantEdges as e (`${e.source}:${e.relation}`)}
										<li class="mono">
											<span class="subject" title={graphNode(e.source).title}
												>{graphNode(e.source).label}</span
											>
											<span class="chip rel">{e.relation}</span>
											<span class="mut">on {graphNode(e.target).label}</span>
										</li>
									{/each}
								</ul>
							{/if}
							{#if containerEdges.length > 0}
								<ul class="edges">
									{#each containerEdges as e (`${e.relation}:${e.target}`)}
										<li class="mono">
											<span class="mut">{e.relation} →</span>
											<span class="subject" title={graphNode(e.target).title}
												>{graphNode(e.target).label}</span
											>
										</li>
									{/each}
								</ul>
							{/if}
						{/if}
					</div>
				{/if}
			</section>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 18px;
		color: var(--mut);
	}
	h1 {
		font-size: 20px;
		margin: 0;
		color: var(--ink);
	}
	.back {
		color: var(--mut);
		font-size: 13px;
		text-decoration: none;
	}
	.back:hover {
		color: var(--ink);
	}
	.sep {
		color: var(--faint);
	}
	.count {
		font-size: 12px;
		color: var(--faint);
	}
	section {
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
		margin: 4px 0;
	}

	/* Real tokens, not the legacy `--ink/--line/--faint` bridge this file otherwise uses: those names
	   were undefined for a long time and fell back to `currentColor`, which is the estate's
	   long-standing "why does this look weird". Migrating the whole page is its own change; new rules
	   should not add to the debt. */
	.managed {
		border-left: 2px solid var(--border);
		padding-left: 8px;
		color: var(--muted-foreground);
	}
	.error {
		color: var(--fail);
		font-size: 12px;
		margin: 6px 0 0;
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.row {
		display: block;
		padding: 6px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
		color: var(--ink);
		text-decoration: none;
		font-size: 13px;
	}
	.row:hover {
		background: var(--panel-2);
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
	.refs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--mut);
		font-size: 12px;
		padding: 1px 8px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber) 55%, var(--line));
	}
	.policy-edit {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
		align-items: end;
	}
	.policy-edit label {
		display: flex;
		flex-direction: column;
		gap: 3px;
		color: var(--faint);
		font-size: 11px;
	}
	.policy-edit label.check {
		flex-direction: row;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--mut);
	}
	.policy-edit input[type='number'] {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 7px;
		width: 130px;
	}
	.btnrow {
		display: flex;
		gap: 6px;
	}
	.btn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 12px;
		cursor: pointer;
	}
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.btn.danger {
		color: var(--fail);
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.graphtoggle {
		margin: 10px 0 8px;
	}
	.graphcard {
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		background: var(--panel-2);
		padding: 10px 12px;
	}
	.graphhead {
		display: flex;
		align-items: baseline;
		gap: 8px;
		margin-bottom: 6px;
	}
	.graphhead h3 {
		font-size: 13px;
		margin: 0;
	}
	.edges {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 12px;
	}
	.edges li {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 3px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
	}
	.edges li:last-child {
		border-bottom: none;
	}
	.subject {
		color: var(--ink);
	}
	.chip.rel {
		border-color: color-mix(in srgb, var(--ok) 45%, var(--line));
	}
</style>
