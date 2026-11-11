<script lang="ts">
	import { controlEvents } from '#lib/admin/remote/admin.api.ts';
	import { grantDetail, objectLabel, type ControlEvent } from '#lib/admin/control.ts';
	import { Activity, ShieldAlert } from '@lucide/svelte';
	import ControlEventLine from './ControlEventLine.svelte';

	// A live cursor (query.live): the generator in admin.api.ts polls the catalog from this browser, owns
	// the cursor, the window and event_id dedup, and yields the window when it changes. No argument: the
	// estate-wide feed, which the catalog gates on the estate-admin relation, so a non-admin's first poll
	// ends the feed with a 403 that renders below and is not retried.
	const feed = controlEvents();

	const failure = (err: unknown) => (err instanceof Error ? err.message : String(err));

	// #70 — filter the RENDERED window, client-side. The live cursor's generator owns the
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
		const grant = grantDetail(e);
		return [e.action, objectLabel(e), e.object_id, e.actor ?? 'system', grant?.subject ?? '']
			.filter((f) => f !== '')
			.some((f) => f.toLowerCase().includes(needle));
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

	{#if feed.error !== undefined}
		<p class="empty">
			<ShieldAlert size={15} /> Feed unavailable: {failure(feed.error)}
		</p>
	{:else if feed.current === undefined}
		<p class="empty">Connecting to the live feed&hellip;</p>
	{:else}
		{@const events = feed.current}
		{@const shown = events.filter(matches)}

		{#if events.length === 0}
			<!-- The catalog keeps this feed in memory, per replica (`GET /management/v1/events`): a restart
			     empties it, so an empty feed says what it holds rather than that nothing changed. -->
			<p class="empty">
				No changes since this catalog started. The feed holds only the governance changes the
				catalog has seen since its last start; earlier ones are in the audit trail.
			</p>
		{:else}
			<div class="filters">
				<input
					class="mono"
					type="search"
					placeholder="filter by action, object, subject or actor…"
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
						<ControlEventLine event={e} />
					{/each}
				</ul>
			{/if}
		{/if}
	{/if}
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
	.mono {
		font-family: ui-monospace, monospace;
	}
</style>
