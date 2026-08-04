<script lang="ts">
	import { controlEvents } from '$lib/admin/remote/admin.remote';
	import type { ControlEvent } from '$lib/admin/control';
	import { Activity, ShieldAlert } from '@lucide/svelte';

	// A live query (query.live): the server generator owns the cursor + accumulation + event_id dedup and
	// yields the recent window on change; SvelteKit streams it here and owns reconnect. No arg → the
	// estate-wide feed (the catalog gates it on the estate-admin relation, so a non-admin gets a terminal 403).
	const feed = controlEvents();

	const label = (e: ControlEvent) => e.action.replaceAll('_', ' ');
	// ISO slice, not toLocaleTimeString, so the SSR first-frame and the hydrated client agree (no mismatch).
	const at = (iso: string) => iso.slice(11, 19);

	// #70 — filter the RENDERED window, client-side. The live query's server generator owns the
	// cursor and the window; filtering must never re-query per keystroke or the tail dies with the
	// eviction rule. So the filter is a pure function over the already-streamed array: typing narrows
	// what is shown, while the stream keeps appending underneath — clearing the filter shows
	// everything that arrived meanwhile.
	let q = $state('');
	let typeFilter = $state('');

	function matches(e: ControlEvent): boolean {
		if (typeFilter && e.object_type !== typeFilter) return false;
		const needle = q.trim().toLowerCase();
		if (!needle) return true;
		return [e.action, e.object_id, e.actor ?? 'system'].some((f) =>
			f.toLowerCase().includes(needle),
		);
	}

	// The type dropdown offers what the WINDOW actually contains — no hardcoded list to drift.
	const typesIn = (events: ControlEvent[]): string[] =>
		[...new Set(events.map((e) => e.object_type))].sort();
</script>

<section class="page">
	<header>
		<Activity size={16} />
		<h1>Control-plane activity</h1>
		<span class="sub">live &middot; governance changes across the estate</span>
	</header>

	<svelte:boundary>
		{@const events = await feed}
		{@const shown = events.filter(matches)}

		{#if events.length === 0}
			<p class="empty">No recent changes.</p>
		{:else}
			<div class="filters">
				<input
					class="mono"
					type="search"
					placeholder="filter by action, object or actor…"
					aria-label="Filter events"
					bind:value={q}
				/>
				<select aria-label="Filter by object type" bind:value={typeFilter}>
					<option value="">all types</option>
					{#each typesIn(events) as t (t)}
						<option value={t}>{t}</option>
					{/each}
				</select>
				{#if q || typeFilter}
					<span class="count">{shown.length} of {events.length}</span>
				{/if}
			</div>
			{#if shown.length === 0}
				<p class="empty">Nothing in the window matches — the feed is still live underneath.</p>
			{:else}
				<ul class="events">
					{#each shown as e (e.event_id)}
						<li>
							<span class="action">{label(e)}</span>
							<span class="obj mono">{e.object_id}</span>
							<span class="actor">{e.actor ?? 'system'}</span>
							<span class="ts mono">{at(e.occurred_at)}</span>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}

		{#snippet pending()}
			<p class="empty">Connecting to the live feed&hellip;</p>
		{/snippet}

		{#snippet failed(err)}
			<p class="empty">
				<ShieldAlert size={15} /> Feed unavailable: {err instanceof Error ? err.message : String(err)}
			</p>
		{/snippet}
	</svelte:boundary>
</section>

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
		margin-bottom: 16px;
	}
	h1 {
		font-size: 20px;
		margin: 0;
	}
	.sub {
		color: var(--faint);
		font-size: 12px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--mut);
		font-size: 13px;
		padding: 30px 0;
	}
	.filters {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 12px;
	}
	.filters input {
		flex: 1;
		max-width: 340px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: inherit;
		font-size: 12px;
		padding: 5px 8px;
	}
	.filters select {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: inherit;
		font-size: 12px;
		padding: 5px 6px;
	}
	.count {
		color: var(--faint);
		font-size: 12px;
	}
	.events {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.events li {
		display: grid;
		grid-template-columns: 170px 1fr auto auto;
		gap: 12px;
		align-items: baseline;
		padding: 6px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
		font-size: 12px;
	}
	.action {
		color: var(--ink);
		font-weight: 500;
	}
	.obj {
		color: var(--mut);
	}
	.actor,
	.ts {
		color: var(--faint);
	}
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
