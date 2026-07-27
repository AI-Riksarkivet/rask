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
</script>

<section class="page">
	<header>
		<Activity size={16} />
		<h1>Control-plane activity</h1>
		<span class="sub">live &middot; governance changes across the estate</span>
	</header>

	<svelte:boundary>
		{@const events = await feed}

		{#if events.length === 0}
			<p class="empty">No recent changes.</p>
		{:else}
			<ul class="events">
				{#each events as e (e.event_id)}
					<li>
						<span class="action">{label(e)}</span>
						<span class="obj mono">{e.object_id}</span>
						<span class="actor">{e.actor ?? 'system'}</span>
						<span class="ts mono">{at(e.occurred_at)}</span>
					</li>
				{/each}
			</ul>
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
