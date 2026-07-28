<script lang="ts">
	// `/warehouses/<id>` — the warehouse rung of the hierarchy (goal cond 3): the registry record
	// (project, bucket, status, root) + the namespaces that live in it. There is no bindings read
	// API, so the namespace list is DERIVED from the table registry by the `<project>-<stage>`
	// naming convention (#84) — labeled as derived, never presented as a registry fact.
	import { RefreshCw, ShieldAlert, Warehouse as WarehouseIcon } from '@lucide/svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { fetchTables, fetchWarehouse, type Warehouse } from '$lib/data/catalog';
	import { namespaceOfTable, stageOf } from '$lib/data/stage';
	import StageBadge from '$lib/data/StageBadge.svelte';

	const id = $derived(page.params.id ?? '');

	// Return here after the OIDC round-trip (the shell's ?redirect= contract, nav-user.svelte).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let wh = $state<Warehouse | null>(null);
	let tables = $state<string[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(wh === null && settled && lastStatus === 401);
	const denied = $derived(wh === null && settled && lastStatus === 403);
	const missing = $derived(wh === null && settled && lastStatus === 404);
	const offline = $derived(wh === null && settled && ![200, 401, 403, 404].includes(lastStatus));

	async function load(): Promise<void> {
		const current = id;
		const [whRes, tRes] = await Promise.all([fetchWarehouse(current), fetchTables()]);
		if (id !== current) return; // latest-wins across navigation
		settled = true;
		if (whRes.ok) {
			wh = whRes.data;
			lastStatus = 200;
		} else {
			wh = null;
			lastStatus = whRes.status;
		}
		tables = tRes.ok ? [...tRes.data.tables].sort() : null;
	}

	$effect(() => {
		void id;
		wh = null;
		tables = null;
		lastStatus = 0;
		settled = false;
		load();
	});

	// The warehouse's namespaces, derived: group the registry by namespace, keep those whose
	// stage-name project prefix matches this warehouse's project (bare stage names belong to the
	// projectless default path and are only shown when the project has no prefixed zones).
	const namespaces = $derived.by(() => {
		const project = wh?.project;
		if (!project || tables === null) return [];
		const counts = new Map<string, number>();
		for (const t of tables) {
			const ns = namespaceOfTable(t);
			counts.set(ns, (counts.get(ns) ?? 0) + 1);
		}
		return [...counts.entries()]
			.map(([ns, count]) => ({ ns, count, info: stageOf(ns) }))
			.filter(({ info }) => info?.project === project)
			.sort((a, b) => a.ns.localeCompare(b.ns));
	});

	function statusOf(w: Warehouse): string {
		return w.status ?? 'active';
	}
</script>

<svelte:head><title>{id} · warehouses · lance</title></svelte:head>

<div class="page">
	<header>
		<a class="back" href={`${base}/data/warehouses`}>Warehouses</a>
		<span class="sep">/</span>
		<WarehouseIcon size={15} />
		<h1 class="mono">{id}</h1>
		{#if wh}
			<span class="chip mono" class:off={statusOf(wh) !== 'active'}>{statusOf(wh)}</span>
		{/if}
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to view this warehouse.
			</p>
		</div>
	{:else if denied}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>You don't have metadata access to this warehouse.</p>
		</div>
	{:else if missing}
		<div class="empty"><p>No such warehouse in the registry.</p></div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if wh === null}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<section>
			<h2>Registry record</h2>
			<div class="facts mono">
				<span
					>project <a class="plink" href={`${base}/data/projects/${encodeURIComponent(wh.project)}`}
						>{wh.project}</a
					></span
				>
				<span>bucket {wh.bucket}</span>
				<span class="loc">{wh.root_uri}</span>
			</div>
		</section>

		<section>
			<h2>Namespaces</h2>
			<p class="mut">
				Derived from the table registry by the <span class="mono">&lt;project&gt;-&lt;stage&gt;</span> naming
				convention — the registry has no bindings read API.
			</p>
			{#if tables === null}
				<p class="mut">Table registry unavailable right now — namespaces can't be derived.</p>
			{:else if namespaces.length === 0}
				<p class="mut">No project-prefixed namespaces in the registry for {wh.project}.</p>
			{:else}
				<ul class="list">
					{#each namespaces as { ns, count, info } (ns)}
						<li>
							<a class="row" href={`${base}/data/namespaces/${encodeURIComponent(ns)}`}>
								<span class="mono">{ns}</span>
								{#if info}<StageBadge {info} />{/if}
								<span class="count">{count} table{count === 1 ? '' : 's'}</span>
							</a>
						</li>
					{/each}
				</ul>
			{/if}
		</section>
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
	.facts {
		display: flex;
		flex-wrap: wrap;
		gap: 14px;
		font-size: 12px;
		color: var(--mut);
	}
	.loc {
		color: var(--faint);
	}
	.plink {
		color: var(--ink);
		text-decoration: none;
	}
	.plink:hover {
		text-decoration: underline;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid color-mix(in srgb, var(--ok) 45%, var(--line));
		border-radius: var(--radius-sm);
		font-size: 11px;
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber, #d18b28) 55%, var(--line));
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 7px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
		color: var(--ink);
		text-decoration: none;
		font-size: 13px;
	}
	.row:hover {
		background: var(--panel-2);
	}
	.count {
		margin-left: auto;
		color: var(--faint);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 0;
	}
</style>
