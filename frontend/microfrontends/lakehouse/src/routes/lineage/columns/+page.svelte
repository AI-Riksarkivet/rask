<script lang="ts">
	// `/lineage/columns` — column-level lineage as a first-class view (#24): pick a dataset from
	// the governed catalog (or arrive with `?dataset=` from a detail page) and explore its
	// field-to-field subgraph, with the per-field provenance/impact panel on click.
	import { RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { Select } from '@rask/ui/select';
	import ColumnLineage from '$lib/lineage/ColumnLineage.svelte';
	import { listDatasets } from '$lib/api';
	import type { DatasetSummary } from '@rask/api/lineage';
	import { lineageTick, liveRead } from '$lib/live/tick.svelte';

	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(page.url.pathname)}`);

	let datasets = $state<DatasetSummary[] | null>(null);
	let lastStatus = $state(0);
	let settled = $state(false);

	const unauthorized = $derived(datasets === null && lastStatus === 401);
	const offline = $derived(datasets === null && settled && lastStatus !== 401);

	// The chosen dataset arrives via `?dataset=` (detail pages deep-link here) and is kept in the
	// URL as the picker changes, so the view survives reload/share. One-way state → URL: the page
	// itself carries no link that rewrites the param externally.
	let selected = $state(page.url.searchParams.get('dataset') ?? '');
	$effect(() => {
		const q = selected ? `?dataset=${encodeURIComponent(selected)}` : '';
		goto(`${base}/lineage/columns${q}`, { replaceState: true, keepFocus: true, noScroll: true });
	});

	async function load(): Promise<void> {
		const res = await listDatasets();
		settled = true;
		if (res.ok) {
			datasets = res.data.datasets ?? [];
			lastStatus = 200;
		} else {
			lastStatus = res.status;
		}
	}

	// The dataset picker's options. This page and the canvas below it used to run two independent timers
	// at different periods (15s here, 5s there), so the list and the graph could disagree about which
	// datasets exist; both now hang off the ONE shared lineage cursor and move together.
	liveRead(lineageTick, () => load());
</script>

<svelte:head><title>Columns · lineage · lance</title></svelte:head>

<div class="app">
	<header>
		<h1>Columns <span class="sub">field-to-field lineage</span></h1>
		<p class="explain">
			How each column was produced — transformation kinds on every hop, masking derivations in red.
		</p>
		<div class="picker">
			<Select
				bind:value={selected}
				ariaLabel="Dataset"
				placeholder="pick a dataset…"
				options={(datasets ?? []).map((d) => ({ value: d.name, label: d.name }))}
			/>
		</div>
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a href={loginHref} data-sveltekit-reload>sign in</a> to browse lineage.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Lineage service unreachable (HTTP {lastStatus}) — retrying.</p>
		</div>
	{:else if !selected}
		<div class="empty pad">
			<p>
				Pick a dataset above (or open one from <a href="{base}/lineage/datasets">Datasets</a> and follow “column
				lineage”) to see how its fields were derived.
			</p>
		</div>
	{:else}
		{#key selected}
			<ColumnLineage dataset={selected} />
		{/key}
	{/if}
</div>

<style>
	.app {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
	}
	header {
		display: flex;
		align-items: center;
		gap: 16px;
		flex-wrap: wrap;
		padding: 11px 18px;
		border-bottom: 1px solid var(--line);
		background: linear-gradient(180deg, var(--panel-2), transparent);
	}
	h1 {
		font-size: 16px;
		margin: 0;
		font-weight: 600;
	}
	.sub {
		color: var(--mut);
		font-size: 12px;
		font-weight: 400;
	}
	.explain {
		margin: 0;
		font-size: 12px;
		color: var(--mut);
	}
	.picker {
		margin-left: auto;
		min-width: 220px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		padding: 32px 20px;
		font-size: 13px;
	}
	.empty a {
		color: var(--ink);
	}
	.empty.pad p {
		margin: 0;
	}
</style>
