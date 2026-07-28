<script lang="ts">
	// `/admin/access` — the FGA workbench (goal cond 4): the estate's authorization system on one
	// surface, against the catalog's frozen /v1/access API (estate-admin gated BY THE CATALOG, this
	// zone only bearer-forwards the session). Four tabs:
	//   Graph  — the SvelteFlow relationship explorer generalized to seed from ANY object id
	//   Tuples — the raw store on the shared DataTable: filter / grant (dialog) / revoke (confirm)
	//   Check  — user × relation × object → one live Check verdict, rendered big
	//   Model  — the checked-in DSL, read-only ("model changes are code migrations")
	import { ShieldCheck } from '@lucide/svelte';
	import AccessCheck from '$lib/admin/AccessCheck.svelte';
	import AccessGraph from '$lib/admin/AccessGraph.svelte';
	import AccessModel from '$lib/admin/AccessModel.svelte';
	import AccessTuples from '$lib/admin/AccessTuples.svelte';
	import { fetchTables } from '$lib/admin/access';

	const TABS = ['Graph', 'Tuples', 'Check', 'Model'] as const;
	type Tab = (typeof TABS)[number];
	let tab = $state<Tab>('Graph');

	// The graph seed — any FGA object id. The search box sets it; a node click re-seeds it; the
	// registry chips below offer the estate's tables (and their derived namespaces) as one-click
	// entry points so the first seed never needs typing.
	let seedInput = $state('');
	let seed = $state<string | null>(null);

	let tables = $state<string[] | null>(null);
	let registryStatus = $state(0);

	async function loadRegistry(): Promise<void> {
		const res = await fetchTables();
		if (res.ok) {
			tables = [...res.data.tables].sort();
			registryStatus = 200;
		} else {
			tables = null;
			registryStatus = res.status;
		}
	}

	$effect(() => {
		loadRegistry();
	});

	// Namespaces derived from the registry ids (the segment before the first `$`).
	const namespaces = $derived(
		[
			...new Set((tables ?? []).map((t) => (t.includes('$') ? t.slice(0, t.indexOf('$')) : t))),
		].sort(),
	);

	function load(): void {
		const id = seedInput.trim();
		if (!id) return;
		seed = id;
	}

	function reseed(id: string): void {
		seedInput = id;
		seed = id;
	}
</script>

<svelte:head><title>Access · lance</title></svelte:head>

<div class="page">
	<header>
		<ShieldCheck size={16} />
		<h1>Access</h1>
		<span class="sub mono">FGA workbench · graph / tuples / check / model (#51 #68 #72 #81)</span>
	</header>

	<div class="tabs" role="tablist" aria-label="Access workbench tabs">
		{#each TABS as t (t)}
			<button
				class="tab"
				role="tab"
				aria-selected={tab === t}
				class:active={tab === t}
				onclick={() => (tab = t)}>{t}</button
			>
		{/each}
	</div>

	{#if tab === 'Graph'}
		<form
			class="picker"
			onsubmit={(e) => {
	e.preventDefault();
	load();
}}
		>
			<input
				class="mono"
				bind:value={seedInput}
				placeholder="seed object id (e.g. table:db1$t, warehouse:acme-wh, user:alice)"
				aria-label="Graph seed object"
			/>
			<button class="btn" type="submit" disabled={!seedInput.trim()}>Seed</button>
		</form>

		{#if seed}
			{#key seed}
				<section class="panel">
					<h2 class="mono">{seed}</h2>
					<AccessGraph object={seed} onseed={reseed} />
				</section>
			{/key}
		{/if}

		<section>
			<h2>Registry entries</h2>
			{#if tables === null}
				<p class="mut">
					{registryStatus === 0
						? 'Loading the registry…'
						: `Registry unavailable (HTTP ${registryStatus}) — seed any object id above.`}
				</p>
			{:else if tables.length === 0}
				<p class="mut">No tables registered yet — seed any object id above.</p>
			{:else}
				<div class="entries">
					{#each namespaces as ns (ns)}
						<button class="chip mono" onclick={() => reseed(`namespace:${ns}`)}>ns · {ns}</button>
					{/each}
					{#each tables as t (t)}
						<button class="chip mono" onclick={() => reseed(`table:${t}`)}>{t}</button>
					{/each}
				</div>
			{/if}
		</section>
	{:else if tab === 'Tuples'}
		<AccessTuples object={seed ?? ''} />
	{:else if tab === 'Check'}
		<AccessCheck />
	{:else}
		<AccessModel />
	{/if}
</div>

<style>
	.page {
		max-width: 980px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 10px;
		margin-bottom: 14px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.tabs {
		display: flex;
		gap: 4px;
		border-bottom: 1px solid var(--line);
		margin-bottom: 16px;
	}
	.tab {
		background: none;
		border: none;
		border-bottom: 2px solid transparent;
		color: var(--mut);
		font-size: 13px;
		padding: 6px 12px;
		cursor: pointer;
	}
	.tab.active {
		color: var(--ink);
		border-bottom-color: var(--accent, #6aa9ff);
	}
	.picker {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 18px;
	}
	.picker input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
		flex: 1 1 320px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 14px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.panel {
		margin-bottom: 22px;
	}
	section h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.panel h2 {
		text-transform: none;
		letter-spacing: 0;
		color: var(--ink);
		font-size: 14px;
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.entries {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		color: var(--mut);
		font-size: 12px;
		padding: 1px 10px;
		cursor: pointer;
	}
	.chip:hover {
		color: var(--ink);
		border-color: var(--mut);
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
