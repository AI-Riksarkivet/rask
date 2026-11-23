<script lang="ts">
	// #71 what just happened in a project: the 10 most recent control events on it and its warehouses,
	// newest first, with the estate-wide, filterable stream one link away. The feed is estate-admin
	// gated at the catalog, so the page reads it only for an estate admin and hands this section the
	// read (or none); anyone else sees a quiet denial, not an error.
	import { resolve } from '$app/paths';
	import ControlEventLine from '#lib/admin/ControlEventLine.svelte';
	import type { projectEvents } from '#lib/remote/warehouses.api.ts';

	let { read }: { read: ReturnType<typeof projectEvents> | null } = $props();

	const answer = $derived(read?.current);
	const events = $derived(answer?.ok ? answer.data : null);
	const denied = $derived(
		read === null || (answer?.ok === false && (answer.status === 401 || answer.status === 403)),
	);
</script>

<section>
	<h2>Recent activity</h2>
	{#if denied}
		<p class="mut">
			The event feed is estate-admin gated — ask an estate admin, or check
			<a href={resolve('/admin/events')}>the estate stream</a>.
		</p>
	{:else if events === null}
		<p class="mut">Events unavailable right now.</p>
	{:else if events.length === 0}
		<p class="mut">
			No governance changes on this project since the catalog started: its feed holds only those.
		</p>
	{:else}
		<ul class="evlist">
			{#each events as e (e.event_id)}
				<ControlEventLine event={e} />
			{/each}
		</ul>
		<p class="mut">
			<a href={resolve('/admin/events')}>All events, live and filterable →</a>
		</p>
	{/if}
</section>

<style>
	section {
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 4px 0;
	}
	.evlist {
		list-style: none;
		margin: 0;
		padding: 0;
		font-size: 12px;
	}
</style>
